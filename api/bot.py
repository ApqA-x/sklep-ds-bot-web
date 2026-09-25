from __future__ import annotations

import asyncio
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

from . import discord_api, mutations, operations
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


def _op_key(request: Request) -> str:
    """Ключ идемпотентности присылает клиент (T08.9): повтор доставки — прежний key,
    новая попытка — новый. Без заголовка journal ведёт per-attempt (dedup невозможен)."""
    key = request.headers.get("idempotency-key", "").strip()
    return key or operations.new_id()


def _audit(
    db: Any,
    guild: str,
    actor: dict[str, str],
    action: str,
    arguments: dict[str, Any],
    *,
    status: int | None,
    detail: Any,
    ok: bool,
    op_id: str,
) -> None:
    """O08: факт операции устойчив уже к этому моменту; сбой audit-проекции остаётся
    видимым (auditError на документе), а не маскируется под полный успех."""
    try:
        mutations.record_audit(
            db,
            guild_id=guild,
            actor=actor,
            action=f"bot.{action}",
            after={**arguments, "discordStatus": status, "detail": detail if not ok else None},
            ok=ok,
            origin="web",  # инициатор — веб-интерфейс; Discord здесь только транспорт исполнения
            operation_id=op_id,
        )
    except Exception:  # noqa: BLE001 — сбой проекции не должен превращать выполненный эффект в 500
        operations.mark_audit_error(db, op_id)


async def _action(
    request: Request,
    kind: str,
    action: str,
    arguments: dict[str, Any],
    run: Any,
) -> dict:
    """T08: устойчивый intent → атомарный claim → внешний эффект → финал.

    Ни один вызов Discord не происходит до успешной записи намерения (O01).
    Повтор терминальной операции возвращает сохранённый факт без нового эффекта (O04);
    unknown не переигрывается вслепую (O05); lease/fencing — в operations.claim/finish.
    """
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    guild = _guild(request)
    actor = actor_of(request)
    # T16 п.3: journal (operations/mutations) — синхронный pymongo; все записи
    # уходят в thread, чтобы медленная БД не вешала event loop под нагрузкой (L06).
    doc, _created = await asyncio.to_thread(
        operations.create_intent,
        db,
        guild_id=guild,
        actor=actor,
        kind=kind,
        arguments=arguments,
        idempotency_key=_op_key(request),
        batch_id=request.headers.get("x-batch-id") or None,
    )
    op_id = doc["_id"]
    if doc.get("state") in operations.TERMINAL:
        view = operations.public_view(doc)
        replayed = {"ok": doc["state"] == "succeeded", "operationId": op_id, "replayed": True,
                    "state": doc["state"], "result": view["result"], "error": view["error"]}
        if doc["state"] == "succeeded":
            replayed["discordStatus"] = (doc.get("result") or {}).get("discordStatus")
            return replayed
        if doc["state"] == "failed":
            saved = doc.get("error") or {}
            raise HTTPException(status_code=502, detail={**saved, "operationId": op_id, "replayed": True})
        raise HTTPException(status_code=504, detail={"error": "outcome_unproven", **replayed})
    claimed_pair = await asyncio.to_thread(operations.claim, db, op_id)
    if claimed_pair is None:
        raise HTTPException(status_code=409, detail={"error": "operation_in_progress", "operationId": op_id})
    claimed, mode = claimed_pair
    if mode == "takeover" and not operations.is_idempotent(kind):
        # O07: переживший lease не доказывает ни эффект, ни его отсутствие;
        # для неидемпотентного kind слепой повтор запрещён — остаёмся unknown.
        await asyncio.to_thread(
            operations.finish, db, op_id, claimed, "unknown", error={"classification": "lease_expired_unproven"}
        )
        await asyncio.to_thread(
            _audit, db, guild, actor, action, arguments, status=None,
            detail={"classification": "lease_expired_unproven"}, ok=False, op_id=op_id,
        )
        raise HTTPException(status_code=504, detail={"error": "outcome_unproven", "operationId": op_id, "state": "unknown"})
    try:
        status, payload = await run()
    except HTTPException:
        # эффект не отправлялся (лимит/конфиг) — попытка честная, но без внешнего вызова
        await asyncio.to_thread(
            operations.finish, db, op_id, claimed, "failed", error={"classification": "not_dispatched"}
        )
        raise
    except Exception as exc:  # noqa: BLE001 — транспорт: distinguish «не ушло» vs «неизвестно» (O05/O06)
        classification, definitely_no_effect = operations.classify_transport(exc)
        state = "failed" if definitely_no_effect else "unknown"
        await asyncio.to_thread(
            operations.finish, db, op_id, claimed, state, error={"classification": classification}
        )
        await asyncio.to_thread(
            _audit, db, guild, actor, action, arguments, status=None,
            detail={"classification": classification}, ok=False, op_id=op_id,
        )
        raise HTTPException(
            status_code=504, detail={"error": classification, "operationId": op_id, "state": state}
        ) from exc
    ok = 200 <= status < 300
    detail = payload if isinstance(payload, dict) else {"raw": str(payload)[:300]}
    if ok:
        result: dict[str, Any] = {"discordStatus": status}
        resource_id = detail.get("id") if isinstance(detail, dict) else None
        if resource_id:
            result["discordResourceId"] = str(resource_id)  # T08.7: зацепка для ручной сверки
        await asyncio.to_thread(operations.finish, db, op_id, claimed, "succeeded", result=result)
    else:
        await asyncio.to_thread(
            operations.finish, db, op_id, claimed, "failed", error={"discordStatus": status, "body": detail}
        )
    await asyncio.to_thread(
        _audit, db, guild, actor, action, arguments, status=status, detail=detail, ok=ok, op_id=op_id,
    )
    if not ok:
        raise HTTPException(status_code=502, detail={"discordStatus": status, "body": detail, "operationId": op_id})
    return {"ok": True, "discordStatus": status, "operationId": op_id, "state": "succeeded"}


@router.post("/member/{userId}/roles")
async def member_role(request: Request, guildId: str, userId: str, body: MemberRoleAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    await _check_member(request, guildId, userId)
    await _check_role(request, guildId, body.roleId)
    method = "PUT" if body.action == "grant" else "DELETE"
    return await _action(
        request,
        f"bot.role.{body.action}",
        "role",
        {"userId": userId, "roleId": body.roleId, "action": body.action},
        lambda: _call(request, method, f"/guilds/{guildId}/members/{userId}/roles/{body.roleId}"),
    )


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
    return await _action(
        request,
        "bot.timeout.set" if body.mute else "bot.timeout.clear",
        "timeout",
        {"userId": userId, "mute": body.mute, "seconds": body.seconds if body.mute else None},
        lambda: _call(
            request, "PATCH", f"/guilds/{guildId}/members/{userId}", json_body=json_body, reason="voice_tracker web"
        ),
    )


@router.post("/member/{userId}/move")
async def member_move(request: Request, guildId: str, userId: str, body: MoveMemberAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    await _check_member(request, guildId, userId)
    await _check_channel(request, guildId, body.channelId, types=VOICE_TARGET_TYPES)
    return await _action(
        request,
        "bot.move",
        "move",
        {"userId": userId, "channelId": body.channelId},
        lambda: _call(
            request,
            "PATCH",
            f"/guilds/{guildId}/members/{userId}",
            json_body={"channel_id": body.channelId},
        ),
    )


@router.post("/member/{userId}/disconnect")
async def member_disconnect(request: Request, guildId: str, userId: str) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    await _check_member(request, guildId, userId)
    return await _action(
        request,
        "bot.disconnect",
        "disconnect",
        {"userId": userId},
        lambda: _call(
            request,
            "PATCH",
            f"/guilds/{guildId}/members/{userId}",
            json_body={"channel_id": None},
        ),
    )


@router.post("/member/{userId}/kick")
async def member_kick(request: Request, guildId: str, userId: str, body: KickMemberAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    await _check_member(request, guildId, userId)
    perms = await perms_for(request, guildId)
    if not perms & (1 << 3 | KICK_MEMBERS):
        raise HTTPException(status_code=403, detail="kick requires administrator or kick members")
    return await _action(
        request,
        "bot.kick",
        "kick",
        {"userId": userId, "reason": body.reason},
        lambda: _call(request, "DELETE", f"/guilds/{guildId}/members/{userId}", reason=body.reason),
    )


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
    return await _action(
        request,
        "bot.message",
        "message",
        arguments,
        lambda: _call(
            request,
            "POST",
            f"/channels/{channelId}/messages",
            json_body=json_body or None,
            files=files or None,
            billed=True,
        ),
    )


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
    return await _action(
        request,
        "bot.invite.create",
        "invite.create",
        {"channelId": body.channelId, "maxAge": body.maxAge, "maxUses": body.maxUses},
        lambda: _call(
            request,
            "POST",
            f"/channels/{body.channelId}/invites",
            json_body={"max_age": body.maxAge, "max_uses": body.maxUses},
        ),
    )


@router.delete("/invite/{code}")
async def delete_invite(request: Request, guildId: str, code: str) -> dict:
    _snowflake(guildId, "guildId")
    if not INVITE_CODE_RE.match(code):
        raise HTTPException(status_code=422, detail="invalid invite code")
    await _check_invite(request, guildId, code)
    return await _action(
        request,
        "bot.invite.delete",
        "invite.delete",
        {"code": code},
        lambda: _call(request, "DELETE", f"/invites/{code}"),
    )


@router.get("/operations/{operationId}")
async def operation_status(request: Request, guildId: str, operationId: str) -> dict:
    """T08.9: защищённое чтение статуса (тот же require_guild_admin + guild-scope T04)."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    doc = await asyncio.to_thread(operations.get_operation, db, guildId, operationId)
    if doc is None:
        raise HTTPException(status_code=404, detail="operation not found")
    return operations.public_view(doc)


@router.get("/operations")
async def operation_batch(request: Request, guildId: str, batchId: str) -> dict:
    """O10: батч из нескольких каналов — явные child-статусы, общий success только когда все терминальны."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    docs = await asyncio.to_thread(operations.list_batch, db, guildId, batchId)
    views = [operations.public_view(doc) for doc in docs]
    pending = [v for v in views if v["state"] not in operations.TERMINAL]
    return {"batchId": batchId, "operations": views, "complete": not pending, "pending": len(pending)}
