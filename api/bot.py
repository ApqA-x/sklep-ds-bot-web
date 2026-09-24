from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from starlette.datastructures import UploadFile  # парсер создаёт базовый класс, fastapi лишь оборачивает его

try:  # в starlette 0.40 исключение живёт в formparsers, в более новых переехало в exceptions
    from starlette.formparsers import MultiPartException
except ImportError:
    from starlette.exceptions import MultiPartException

from . import discord_api, mutations
from .auth import actor_of, perms_for, require_guild_admin
from .config import WebConfig
from .models import (
    ChannelMessageAction,
    InviteCreateAction,
    KickMemberAction,
    MemberRoleAction,
    MoveMemberAction,
    TimeoutMemberAction,
)

router = APIRouter(
    prefix="/api/guild/{guildId}/bot",
    tags=["bot-actions"],
    dependencies=[Depends(require_guild_admin)],
)

# capacity=5, refill 2/s per guild — Discord global limit is 50/s
_BUCKET_CAPACITY = 5.0
_BUCKET_RATE = 2.0
_buckets: dict[str, tuple[float, float]] = {}

# вложения сообщений: лимиты Discord на файл — 25 МБ, держаем разумный потолок пачки
MAX_FILES = 10
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024

KICK_MEMBERS = 1 << 2
SNOWFLAKE_RE = re.compile(r"^\d{5,25}$")
INVITE_CODE_RE = re.compile(r"^[A-Za-z0-9-]{2,32}$")


def _snowflake(value: str, field: str) -> str:
    if not SNOWFLAKE_RE.match(value):
        raise HTTPException(status_code=422, detail=f"invalid {field}")
    return value


def _reset_rate_buckets() -> None:
    _buckets.clear()


def _allow(guild_id: str) -> bool:
    now = time.monotonic()
    tokens, last = _buckets.get(guild_id, (_BUCKET_CAPACITY, now))
    tokens = min(_BUCKET_CAPACITY, tokens + (now - last) * _BUCKET_RATE)
    if tokens < 1:
        _buckets[guild_id] = (tokens, now)
        return False
    _buckets[guild_id] = (tokens - 1, now)
    return True


def _guild(request: Request) -> str:
    return request.path_params["guildId"]


def _cfg(request: Request) -> WebConfig:
    return request.app.state.config


async def _call(
    request: Request,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    reason: str | None = None,
    files: list[tuple[str, bytes, str]] | None = None,
):
    cfg = _cfg(request)
    if not cfg.discord_token:
        raise HTTPException(status_code=503, detail="bot token not configured")
    if not _allow(_guild(request)):
        raise HTTPException(status_code=429, detail="too many bot actions, slow down")
    return await discord_api.bot_request(cfg, method, path, json_body=json_body, reason=reason, files=files)


def _finish(request: Request, action: str, arguments: dict[str, Any], status: int, payload: Any) -> dict:
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    guild = _guild(request)
    ok = 200 <= status < 300
    detail = payload if isinstance(payload, dict) else {"raw": str(payload)[:300]}
    mutations.record_audit(
        db,
        guild_id=guild,
        actor=actor_of(request),
        action=f"bot.{action}",
        after={**arguments, "discordStatus": status, "detail": detail if not ok else None},
        ok=ok,
        origin="web",  # инициатор — веб-интерфейс; Discord здесь только транспорт исполнения
    )
    if not ok:
        raise HTTPException(status_code=502, detail={"discordStatus": status, "body": detail})
    return {"ok": True, "discordStatus": status}


@router.post("/member/{userId}/roles")
async def member_role(request: Request, guildId: str, userId: str, body: MemberRoleAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    method = "PUT" if body.action == "grant" else "DELETE"
    status, payload = await _call(request, method, f"/guilds/{guildId}/members/{userId}/roles/{body.roleId}")
    return _finish(request, "role", {"userId": userId, "roleId": body.roleId, "action": body.action}, status, payload)


@router.post("/member/{userId}/timeout")
async def member_timeout(request: Request, guildId: str, userId: str, body: TimeoutMemberAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    if body.mute:
        until = (datetime.now(timezone.utc) + timedelta(seconds=body.seconds)).isoformat()
        json_body: dict[str, Any] | None = {"communication_disabled_until": until}
    else:
        json_body = {"communication_disabled_until": None}
    status, payload = await _call(
        request, "PATCH", f"/guilds/{guildId}/members/{userId}", json_body=json_body, reason="voice_tracker web"
    )
    return _finish(request, "timeout", {"userId": userId, "mute": body.mute, "seconds": body.seconds if body.mute else None}, status, payload)


@router.post("/member/{userId}/move")
async def member_move(request: Request, guildId: str, userId: str, body: MoveMemberAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    status, payload = await _call(
        request,
        "PATCH",
        f"/guilds/{guildId}/members/{userId}",
        json_body={"channel_id": body.channelId},
    )
    return _finish(request, "move", {"userId": userId, "channelId": body.channelId}, status, payload)


@router.post("/member/{userId}/disconnect")
async def member_disconnect(request: Request, guildId: str, userId: str) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    status, payload = await _call(
        request,
        "PATCH",
        f"/guilds/{guildId}/members/{userId}",
        json_body={"channel_id": None},
    )
    return _finish(request, "disconnect", {"userId": userId}, status, payload)


@router.post("/member/{userId}/kick")
async def member_kick(request: Request, guildId: str, userId: str, body: KickMemberAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    perms = await perms_for(request, guildId)
    if not perms & (1 << 3 | KICK_MEMBERS):
        raise HTTPException(status_code=403, detail="kick requires administrator or kick members")
    status, payload = await _call(
        request, "DELETE", f"/guilds/{guildId}/members/{userId}", reason=body.reason
    )
    return _finish(request, "kick", {"userId": userId, "reason": body.reason}, status, payload)


@router.post("/channel/{channelId}/message")
async def channel_message(request: Request, guildId: str, channelId: str) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(channelId, "channelId")
    content, files = await _message_body(request)
    json_body = {"content": content} if content else {}
    status, payload = await _call(
        request, "POST", f"/channels/{channelId}/messages", json_body=json_body or None, files=files or None
    )
    arguments: dict[str, Any] = {
        "channelId": channelId,
        "length": len(content),
        # полный текст в журнал сайта: Discord ограничивает сообщение 2000 символами
        "content": content or None,
    }
    if files:
        # в audit — только имена и размеры, не содержимое
        arguments["attachments"] = [{"name": name, "size": len(blob)} for name, blob, _ in files]
    return _finish(request, "message", arguments, status, payload)


async def _message_body(request: Request) -> tuple[str, list[tuple[str, bytes, str]]]:
    """Разбор тела сообщения: JSON {content}, multipart (content + files[]) или form."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        try:
            body = ChannelMessageAction.model_validate(await request.json())
        except (ValidationError, ValueError):
            raise HTTPException(status_code=422, detail="content must be 1..2000 characters") from None
        return body.content, []

    try:
        form = await request.form(max_files=MAX_FILES + 1)
    except MultiPartException:
        raise HTTPException(status_code=422, detail="invalid multipart body") from None
    content = str(form.get("content") or "").strip()
    if len(content) > 2000:
        raise HTTPException(status_code=422, detail="content must be at most 2000 characters")
    uploads = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
    if len(uploads) > MAX_FILES:
        raise HTTPException(status_code=422, detail="too many files")
    if not content and not uploads:
        raise HTTPException(status_code=422, detail="content or files required")
    files: list[tuple[str, bytes, str]] = []
    total = 0
    for up in uploads:
        blob = await up.read()
        if len(blob) > MAX_FILE_BYTES:
            raise HTTPException(status_code=422, detail=f"file {up.filename!r} exceeds {MAX_FILE_BYTES} bytes")
        total += len(blob)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(status_code=422, detail=f"total upload exceeds {MAX_TOTAL_BYTES} bytes")
        files.append((up.filename or "file", blob, up.content_type or ""))
    return content, files


@router.post("/invite")
async def create_invite(request: Request, guildId: str, body: InviteCreateAction) -> dict:
    _snowflake(guildId, "guildId")
    status, payload = await _call(
        request,
        "POST",
        f"/channels/{body.channelId}/invites",
        json_body={"max_age": body.maxAge, "max_uses": body.maxUses},
    )
    return _finish(request, "invite.create", {"channelId": body.channelId}, status, payload)


@router.delete("/invite/{code}")
async def delete_invite(request: Request, guildId: str, code: str) -> dict:
    _snowflake(guildId, "guildId")
    if not INVITE_CODE_RE.match(code):
        raise HTTPException(status_code=422, detail="invalid invite code")
    status, payload = await _call(request, "DELETE", f"/invites/{code}")
    return _finish(request, "invite.delete", {"code": code}, status, payload)
