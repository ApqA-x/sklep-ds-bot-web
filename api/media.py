"""T05: авторизованная выдача сохранённых вложений вместо публичного StaticFiles.

Достроено по решению владельца D02: панель = owner/Administrator своей гильдии.
Файл отдаётся только когда доказана связь метадания вложения с авторизованной
гильдией (запись chat_messages), путь канонический и лежит внутри MEDIA_DIR.
Знание sha256-пути доступа не даёт (M01): без сессии/прав — 404/401.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import Response

from .auth import perms_for, require_guild_read

log = logging.getLogger("api.media")

ADMINISTRATOR = 1 << 3

# <guildId>/<YYYY-MM>/<sha256>.<ext> — ровно та схема, что пишет bot media.store_attachments
REL_RE = re.compile(r"^(\d{5,25})/(\d{4}-\d{2})/([0-9a-f]{64})\.([A-Za-z0-9]{1,8})$")

MEDIA_MAX_BYTES = 20 * 1024 * 1024

# магические заголовки растеров, которые можно отдавать inline
_RASTER = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _sniff_inline(blob: bytes) -> str | None:
    """Только проверенные растровые форматы получают inline; всё прочее — attachment."""
    for magic, ctype in _RASTER:
        if blob.startswith(magic):
            return ctype
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


def _disposition(meta: dict[str, Any]) -> str:
    name = os.path.basename(str(meta.get("filename") or "file"))[:120] or "file"
    # имя не должно ломать заголовок: управляющие символы и кавычки вычищены
    safe = "".join(ch for ch in name if ch >= " " and ch not in '"\\')
    try:
        name.encode("ascii")
        return f'attachment; filename="{safe}"'
    except UnicodeEncodeError:
        from urllib.parse import quote

        return f"attachment; filename=\"{safe.encode('ascii', 'replace').decode()[:60]}\"; filename*=UTF-8''{quote(name)}"


def _media_root(request: Request) -> Path:
    from .config import _clean

    raw = _clean(getattr(request.app.state.config, "media_dir", ""))
    if not raw:
        raise HTTPException(status_code=503, detail="media storage not configured")
    root = Path(raw)
    if not root.is_dir():
        raise HTTPException(status_code=503, detail="media storage not available")
    return root


def _find_attachment(request: Request, guild_id: str, *, attachment_id: str = "", rel_path: str = "") -> dict[str, Any] | None:
    """Доказательство принадлежности: метаданные вложения есть в записи чата этой гильдии."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    if attachment_id:
        flt: dict[str, Any] = {"guildId": guild_id, "attachments.id": attachment_id}
    else:
        flt = {"guildId": guild_id, "attachments.path": rel_path}
    doc = db["chat_messages"].find_one(flt)
    if not doc:
        return None
    for item in doc.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        if attachment_id and str(item.get("id") or "") != attachment_id:
            continue
        if rel_path and str(item.get("path") or "") != rel_path:
            continue
        return item
    return None


def _serve(request: Request, guild_id: str, meta: dict[str, Any]) -> Response:
    rel = str(meta.get("path") or "")
    if not meta.get("stored") or not REL_RE.match(rel):
        # нет метаданных о хранимом файле или путь не канонический — как будто файла нет
        raise HTTPException(status_code=404, detail="media not found")
    if rel.split("/", 1)[0] != guild_id:
        raise HTTPException(status_code=404, detail="media not found")
    root = _media_root(request)
    root_real = os.path.realpath(root)
    target = os.path.realpath(os.path.join(root_real, rel))
    if not (target == root_real or target.startswith(root_real + os.sep)):
        raise HTTPException(status_code=404, detail="media not found")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="media not found")
    try:
        with open(target, "rb") as fh:
            blob = fh.read(MEDIA_MAX_BYTES + 1)
    except OSError as err:
        log.warning("media read failed: %s", type(err).__name__)
        raise HTTPException(status_code=404, detail="media not found") from None
    if len(blob) > MEDIA_MAX_BYTES:
        raise HTTPException(status_code=413, detail="media too large")
    inline = _sniff_inline(blob)
    headers = {
        "X-Content-Type-Options": "nosniff",
        # приватный ответ не кэшируется публично; после отзыва прав кэш не спасёт (M09)
        "Cache-Control": "private, no-store",
    }
    if inline is None:
        # SVG/HTML/неизвестное содержимое не исполняется с origin панели
        return Response(
            content=blob,
            media_type="application/octet-stream",
            headers={**headers, "Content-Disposition": _disposition(meta)},
        )
    return Response(content=blob, media_type=inline, headers=headers)


router = APIRouter(tags=["media"])

guild_router = APIRouter(
    prefix="/api/guild/{guildId}",
    tags=["media"],
    dependencies=[Depends(require_guild_read)],
)


@guild_router.get("/media/attachment/{attachmentId}")
async def guild_media(request: Request, guildId: str, attachmentId: str) -> Response:
    meta = _find_attachment(request, guildId, attachment_id=attachmentId)
    if meta is None:
        raise HTTPException(status_code=404, detail="media not found")
    return _serve(request, guildId, meta)


@router.get("/media/{guildId}/{rest:path}")
async def legacy_media(request: Request, guildId: str, rest: str) -> Response:
    """Совместимость со старыми /media/<path> ссылками (T05.3): тот же файл,
    но доступ проверяется так же, как у канонического маршрута."""
    perms = await perms_for(request, guildId)
    if not perms & ADMINISTRATOR:
        raise HTTPException(status_code=403, detail="administrator permission required")
    rel = f"{guildId}/{rest}"
    meta = _find_attachment(request, guildId, rel_path=rel)
    if meta is None:
        raise HTTPException(status_code=404, detail="media not found")
    return _serve(request, guildId, meta)
