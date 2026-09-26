from __future__ import annotations

import json
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
    EmbedSpec,
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
# L01: потолок всего HTTP-тела (пачка файлов + overhead multipart/embed)
MAX_BODY_BYTES = MAX_TOTAL_BYTES + 4 * 1024 * 1024

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
    billed: bool = False,
):
    cfg = _cfg(request)
    if not cfg.discord_token:
        raise HTTPException(status_code=503, detail="bot token not configured")
    # billed=True: токен rate-limit уже списан до дорогого парсинга тела (L02)
    if not billed and not _allow(_guild(request)):
        raise HTTPException(status_code=429, detail="too many bot actions, slow down")
    return await discord_api.bot_request(cfg, method, path, json_body=json_body, reason=reason, files=files)


async def _meta_get(request: Request, path: str):
    """GET метаданных ресурса для проверки принадлежности гильдии (T04).

    Без бот-токена проверка невозможна: в dev-режиме пропускаем (изменяющие
    вызовы всё равно закроются 503 в _call), в production токен обязателен (C01).
    """
    cfg = _cfg(request)
    if not cfg.discord_token:
        return None
    status, payload = await discord_api.bot_request(cfg, "GET", path)
    return status, payload


async def _check_member(request: Request, guild_id: str, user_id: str) -> None:
    """Target must be a member of the authorized guild BEFORE any mutation."""
    got = await _meta_get(request, f"/guilds/{guild_id}/members/{user_id}")
    if got is None:
        return
    status, payload = got
    if status == 404:
        raise HTTPException(status_code=403, detail="member not found in this guild")
    if status >= 400:
        raise HTTPException(status_code=502, detail=f"member lookup failed: {status}")


async def _check_channel(
    request: Request, guild_id: str, channel_id: str, *, types: tuple[int, ...] | None = None
) -> dict[str, Any]:
    """Channel must belong to the authorized guild; optional channel-type policy.
    Threads carry their guild_id, so the same check covers them."""
    got = await _meta_get(request, f"/channels/{channel_id}")
    if got is None:
        return {}
    status, payload = got
    if status == 404:
        raise HTTPException(status_code=404, detail="channel not found")
    if status >= 400 or not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=f"channel lookup failed: {status}")
    if str(payload.get("guild_id") or "") != guild_id:
        raise HTTPException(status_code=403, detail="channel belongs to another guild")
    if types is not None and int(payload.get("type") or 0) not in types:
        raise HTTPException(status_code=422, detail="wrong channel type for this action")
    return payload


async def _check_role(request: Request, guild_id: str, role_id: str) -> None:
    """Role must belong to the authorized guild; @everyone and managed roles are
    refused explicitly (not left to the UI picker)."""
    _snowflake(role_id, "roleId")
    if role_id == guild_id:
        raise HTTPException(status_code=422, detail="cannot manage @everyone role")
    got = await _meta_get(request, f"/guilds/{guild_id}/roles")
    if got is None:
        return
    status, rows = got
    if status >= 400 or not isinstance(rows, list):
        raise HTTPException(status_code=502, detail=f"role lookup failed: {status}")
    for row in rows:
        if str(row.get("id") or "") != role_id:
            continue
        if row.get("managed") or row.get("tags"):
            raise HTTPException(status_code=422, detail="managed roles are not assignable here")
        return
    raise HTTPException(status_code=403, detail="role belongs to another guild")


async def _check_invite(request: Request, guild_id: str, code: str) -> None:
    """Invite guild_id must match; unknown ownership → deny (T04.2)."""
    got = await _meta_get(request, f"/invites/{code}")
    if got is None:
        return
    status, payload = got
    if status == 404:
        raise HTTPException(status_code=404, detail="invite not found")
    if status >= 400 or not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=f"invite lookup failed: {status}")
    if str(payload.get("guild_id") or "") != guild_id:
        raise HTTPException(status_code=403, detail="invite belongs to another guild")


TEXTISH_CHANNEL_TYPES = (0, 5, 10, 11, 12)  # text/announcement + треды
VOICE_TARGET_TYPES = (2, 13)  # voice + stage


class _BodyTooLarge(Exception):
    """Внутренний сигнал: поток превысил MAX_BODY_BYTES (L01)."""


def _guard_stream(request: Request) -> None:
    """Считать фактические байты тела: работает и при chunked, и при ложном
    Content-Length — лимит применяется по мере чтения, а не после."""
    total = 0
    original_receive = request.receive

    async def receive():
        nonlocal total
        message = await original_receive()
        if message.get("type") == "http.request":
            total += len(message.get("body", b""))
            if total > MAX_BODY_BYTES:
                raise _BodyTooLarge()
        return message

    request._receive = receive


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
    await _check_member(request, guildId, userId)
    await _check_role(request, guildId, body.roleId)
    method = "PUT" if body.action == "grant" else "DELETE"
    status, payload = await _call(request, method, f"/guilds/{guildId}/members/{userId}/roles/{body.roleId}")
    return _finish(request, "role", {"userId": userId, "roleId": body.roleId, "action": body.action}, status, payload)


@router.post("/member/{userId}/timeout")
async def member_timeout(request: Request, guildId: str, userId: str, body: TimeoutMemberAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    await _check_member(request, guildId, userId)
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
    await _check_member(request, guildId, userId)
    await _check_channel(request, guildId, body.channelId, types=VOICE_TARGET_TYPES)
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
    await _check_member(request, guildId, userId)
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
    await _check_member(request, guildId, userId)
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
    # L01/L02: дешёвые границы и rate-limit ДО парсинга дорогого тела
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request body too large")
    if not _allow(guildId):
        raise HTTPException(status_code=429, detail="too many bot actions, slow down")
    _guard_stream(request)
    await _check_channel(request, guildId, channelId, types=TEXTISH_CHANNEL_TYPES)
    try:
        content, embed, files = await _message_body(request)
    except _BodyTooLarge:
        raise HTTPException(status_code=413, detail="request body too large") from None
    json_body: dict[str, Any] = {}
    if content:
        json_body["content"] = content
    if embed is not None:
        try:
            json_body["embeds"] = [embed.discord_embed({name for name, _, _ in files})]
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    arguments: dict[str, Any] = {
        "channelId": channelId,
        "length": len(content),
        # полный текст в журнал сайта: Discord ограничивает сообщение 2000 символами
        "content": content or None,
    }
    if embed is not None:
        arguments["embed"] = embed.audit_summary()
    if files:
        # в audit — только имена и размеры, не содержимое
        arguments["attachments"] = [{"name": name, "size": len(blob)} for name, blob, _ in files]
    status, payload = await _call(
        request,
        "POST",
        f"/channels/{channelId}/messages",
        json_body=json_body or None,
        files=files or None,
        billed=True,
    )
    return _finish(request, "message", arguments, status, payload)


async def _message_body(request: Request) -> tuple[str, EmbedSpec | None, list[tuple[str, bytes, str]]]:
    """Разбор тела: JSON {content, embed}, multipart (content + embed(JSON-строка) + files[]) или form."""
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        try:
            body = ChannelMessageAction.model_validate(await request.json())
        except (ValidationError, ValueError):
            raise HTTPException(status_code=422, detail="content or embed required") from None
        return body.content or "", body.embed, []

    try:
        # max_files на 1 больше: лишний файл — предсказуемый 422, а не 500 от starlette
        form = await request.form(max_files=MAX_FILES + 1, max_fields=32)
    except MultiPartException:
        raise HTTPException(status_code=422, detail="invalid multipart body") from None
    content = str(form.get("content") or "").strip()
    if len(content) > 2000:
        raise HTTPException(status_code=422, detail="content must be at most 2000 characters")
    embed: EmbedSpec | None = None
    raw_embed = form.get("embed")
    if raw_embed:
        try:
            parsed = EmbedSpec.model_validate(json.loads(str(raw_embed)))
        except (ValidationError, ValueError):
            raise HTTPException(status_code=422, detail="invalid embed field") from None
        embed = None if parsed.is_empty() else parsed
    uploads = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
    if len(uploads) > MAX_FILES:
        raise HTTPException(status_code=422, detail="too many files")
    if not content and not uploads and embed is None:
        raise HTTPException(status_code=422, detail="content, embed or files required")
    files: list[tuple[str, bytes, str]] = []
    total = 0
    for up in uploads:
        # читаем с порогом: файл больше лимита не затопит память (L01)
        blob = await up.read(MAX_FILE_BYTES + 1)
        if len(blob) > MAX_FILE_BYTES:
            raise HTTPException(status_code=422, detail=f"file {up.filename!r} exceeds {MAX_FILE_BYTES} bytes")
        total += len(blob)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(status_code=422, detail=f"total upload exceeds {MAX_TOTAL_BYTES} bytes")
        files.append((up.filename or "file", blob, up.content_type or ""))
    return content, embed, files


@router.post("/invite")
async def create_invite(request: Request, guildId: str, body: InviteCreateAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(body.channelId, "channelId")
    await _check_channel(request, guildId, body.channelId)
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
    await _check_invite(request, guildId, code)
    status, payload = await _call(request, "DELETE", f"/invites/{code}")
    return _finish(request, "invite.delete", {"code": code}, status, payload)
