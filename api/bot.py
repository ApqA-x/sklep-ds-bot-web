from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
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
    """Принадлежность invite проверяется по официальному Invite object (R26-05):

    Discord GET /invites/{code} возвращает гильдию во вложенном поле ``guild`` —
    верхнеуровневого ``guild_id`` у этого ответа нет (его чтение отклоняло легитимное
    удаление своего invite как 403). Fail-closed: любая неоднозначность — отказ.
    """
    got = await _meta_get(request, f"/invites/{code}")
    if got is None:
        return
    status, payload = got
    if status == 404:
        raise HTTPException(status_code=404, detail="invite not found")
    # 401/403/429/5xx и неразбираемый ответ — отказ, никогда не пропускаем молча
    if status >= 400 or not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=f"invite lookup failed: {status}")
    guild = payload.get("guild")
    if not isinstance(guild, dict):
        # group DM (guild нет) или неожиданная форма — принадлежность недоказуема
        raise HTTPException(status_code=403, detail="invite guild cannot be verified")
    gid = str(guild.get("id") or "")
    if not gid or gid != guild_id:
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
    новая попытка — новый. Без заголовка journal ведёт per-attempt (dedup невозможен).
    R26-03.9: формат и длина пришедшего ключа проверяются (клиентский ключ — uuid или
    «batchId:channelId»; произвольная строка в журнал/URL статусов не попадёт)."""
    key = request.headers.get("idempotency-key", "").strip()
    if not key:
        return operations.new_id()
    return operations.validate_key(key, "idempotency-key")


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


async def _finish(
    db: Any, op_id: str, claimed: dict[str, Any], state: str, **kw: Any
) -> dict[str, Any]:
    """Финал с проверкой fence (R26-03.1).

    `finish()` возвращает False при потере fence — прежний «слепой» вызов позволял
    отдать HTTP succeeded, пока журнал остаётся executing, и старый worker писал
    успешный audit поверх чужой попытки. Здесь: False или сбой БД превращаются в
    честное 504 с фактическим состоянием журнала; audit не пишется без права финала.
    """
    try:
        won = await asyncio.to_thread(operations.finish, db, op_id, claimed, state, **kw)
    except Exception as exc:  # noqa: BLE001 — внешний эффект уже мог случиться, журнал недоступен
        raise HTTPException(
            status_code=504,
            detail={"error": "journal_unavailable_after_effect", "operationId": op_id},
        ) from exc
    if not won:
        current = await asyncio.to_thread(operations.read_current, db, op_id)
        raise HTTPException(
            status_code=504,
            detail={
                "error": "ownership_lost",
                "operationId": op_id,
                "state": current.get("state") if current else None,
            },
        )
    return kw.get("result") or {}


async def _action(
    request: Request,
    kind: str,
    action: str,
    arguments: dict[str, Any],
    run: Any,
    *,
    identity: dict[str, Any] | None = None,
    preflight: Callable[[], Awaitable[None]] | None = None,
) -> dict:
    """T08: устойчивый intent → атомарный claim → внешний эффект → финал.

    Ни один вызов Discord не происходит до успешной записи намерения (O01).
    Повтор терминальной операции возвращает сохранённый факт без нового эффекта (O04);
    unknown не переигрывается вслепую (O05); lease/fencing — в operations.claim/finish.

    R26-03: `run(args)` исполняется на аргументах из durable-документа, а не из
    локальной копии вызова — retry/takeover повторяют исходный намеренный payload
    (в том числе исходный абсолютный дедлайн timeout.set). `identity` — канонический
    запрос для requestHash, когда часть args выводима из момента записи.

    R26-05: `preflight` — scope-проверка (GET метаданных), выполняемая только для
    не-терминальных документов: после переиспользования намерения и до claim.
    Replay уже завершённой операции не должен дёргать Discord — там «ресурс уже
    исчез» является ожидаемым следствием прошлого успеха.
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
        identity=identity,
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
    if preflight is not None:
        # R26-05: scope-проверка после устойчивого intent и только для не-терминального
        # документа. Отказ preflight не требует правки журнала: документ остаётся в
        # state=requested — внешнего эффекта не было, намерение уже устойчиво; повтор
        # тем же Idempotency-Key переиспользует его (claim принимает requested), а
        # новый ключ даст новый intent.
        await preflight()
    claimed_pair = await asyncio.to_thread(operations.claim, db, op_id)
    if claimed_pair is None:
        raise HTTPException(status_code=409, detail={"error": "operation_in_progress", "operationId": op_id})
    claimed, mode = claimed_pair
    # единый источник аргументов для effect и audit — документ журнала
    args = doc.get("arguments") or arguments
    if mode == "takeover" and not operations.is_idempotent(kind):
        # O07: переживший lease не доказывает ни эффект, ни его отсутствие;
        # для неидемпотентного kind слепой повтор запрещён — остаёмся unknown.
        await _finish(db, op_id, claimed, "unknown", error={"classification": "lease_expired_unproven"})
        await asyncio.to_thread(
            _audit, db, guild, actor, action, args, status=None,
            detail={"classification": "lease_expired_unproven"}, ok=False, op_id=op_id,
        )
        raise HTTPException(status_code=504, detail={"error": "outcome_unproven", "operationId": op_id, "state": "unknown"})
    try:
        status, payload = await run(args)
    except HTTPException:
        # эффект не отправлялся (лимит/конфиг) — попытка честная, но без внешнего вызова
        await _finish(db, op_id, claimed, "failed", error={"classification": "not_dispatched"})
        raise
    except Exception as exc:  # noqa: BLE001 — транспорт: distinguish «не ушло» vs «неизвестно» (O05/O06)
        classification, definitely_no_effect = operations.classify_transport(exc)
        state = "failed" if definitely_no_effect else "unknown"
        await _finish(db, op_id, claimed, state, error={"classification": classification})
        await asyncio.to_thread(
            _audit, db, guild, actor, action, args, status=None,
            detail={"classification": classification}, ok=False, op_id=op_id,
        )
        raise HTTPException(
            status_code=504, detail={"error": classification, "operationId": op_id, "state": state}
        ) from exc
    ok = 200 <= status < 300
    detail = payload if isinstance(payload, dict) else {"raw": str(payload)[:300]}
    if ok:
        result = operations.extract_result(kind, status, detail)  # R26-03.4: id/code в durable-результат
        await _finish(db, op_id, claimed, "succeeded", result=result)
    elif status >= 500:
        # R26-03.2: 5xx не доказывает отсутствие эффекта — запрос дошёл до Discord
        await _finish(
            db, op_id, claimed, "unknown",
            error={"classification": "discord_server_error", "discordStatus": status, "body": detail},
        )
        await asyncio.to_thread(
            _audit, db, guild, actor, action, args, status=status, detail=detail, ok=False, op_id=op_id,
        )
        raise HTTPException(
            status_code=504,
            detail={"error": "outcome_unproven", "discordStatus": status, "operationId": op_id, "state": "unknown"},
        )
    else:
        await _finish(
            db, op_id, claimed, "failed", error={"discordStatus": status, "body": detail}
        )
    await asyncio.to_thread(
        _audit, db, guild, actor, action, args, status=status, detail=detail, ok=ok, op_id=op_id,
    )
    if not ok:
        raise HTTPException(status_code=502, detail={"discordStatus": status, "body": detail, "operationId": op_id})
    return {"ok": True, "discordStatus": status, "operationId": op_id, "state": "succeeded", "result": result}


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
        lambda _args: _call(request, method, f"/guilds/{guildId}/members/{userId}/roles/{body.roleId}"),
    )


@router.post("/member/{userId}/timeout")
async def member_timeout(request: Request, guildId: str, userId: str, body: TimeoutMemberAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    await _check_member(request, guildId, userId)
    # R26-03.4: абсолютный дедлайн вычисляется один раз при записи намерения и
    # хранится в журнале; retry/takeover повторяют его, а не сдвигают на now+seconds.
    # В requestHash входит только identity (seconds) — поздний повтор того же
    # намерения не превращается в 409 из-за свежей отметки времени.
    arguments: dict[str, Any] = {"userId": userId, "mute": body.mute, "seconds": body.seconds if body.mute else None}
    if body.mute:
        arguments["deadlineAt"] = (datetime.now(timezone.utc) + timedelta(seconds=body.seconds)).isoformat()
    return await _action(
        request,
        "bot.timeout.set" if body.mute else "bot.timeout.clear",
        "timeout",
        arguments,
        lambda args: _call(
            request,
            "PATCH",
            f"/guilds/{guildId}/members/{userId}",
            json_body={"communication_disabled_until": args.get("deadlineAt") if args.get("mute") else None},
            reason="voice_tracker web",
        ),
        identity={"userId": userId, "mute": body.mute, "seconds": body.seconds if body.mute else None},
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
        lambda _args: _call(
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
        lambda _args: _call(
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
        lambda _args: _call(request, "DELETE", f"/guilds/{guildId}/members/{userId}", reason=body.reason),
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
    embed_payload: dict | None = None
    if embed is not None:
        try:
            embed_payload = embed.discord_embed({name for name, _, _ in files})
            json_body["embeds"] = [embed_payload]
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
    # R26-03.3: канонический запрос различает содержимое — sha256 байтов каждого файла
    # и полный нормализованный embed (audit_summary сокращён и для дедупликации не годится).
    identity: dict[str, Any] = {"channelId": channelId, "content": content or None, "embed": embed_payload}
    if files:
        # в audit — имена/размеры/digest, не содержимое
        arguments["attachments"] = [
            {"name": name, "size": len(blob), "sha256": operations.file_digest(blob)}
            for name, blob, _ in files
        ]
        identity["attachments"] = [
            {"name": name, "size": len(blob), "contentType": ctype, "sha256": operations.file_digest(blob)}
            for name, blob, ctype in files
        ]
    return await _action(
        request,
        "bot.message",
        "message",
        arguments,
        lambda _args: _call(
            request,
            "POST",
            f"/channels/{channelId}/messages",
            json_body=json_body or None,
            files=files or None,
            billed=True,
        ),
        identity=identity,
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
        lambda _args: _call(
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
    # R26-05: scope-проверка — preflight внутри _action (после intent, до claim), а не
    # до журнала: повтор уже отработавшего удаления не должен падать на 404 в GET /invites.
    # Код invite стабилен между retry: requestHash-проверка create_intent даёт 409 при
    # другом code под тем же ключом, поэтому effect и preflight читают code из URL-пути.
    return await _action(
        request,
        "bot.invite.delete",
        "invite.delete",
        {"code": code},
        lambda _args: _call(request, "DELETE", f"/invites/{code}"),
        preflight=lambda: _check_invite(request, guildId, code),
    )


@router.get("/operations/{operationId}")
async def operation_status(request: Request, guildId: str, operationId: str) -> dict:
    """T08.9: защищённое чтение статуса (тот же require_guild_admin + guild-scope T04).

    R26-03.5: чтение также выполняет bounded-recovery — зависший «executing» с
    истёкшим lease становится наблюдаемым unknown. Внешних эффектов эндпоинт не
    создаёт: одна CAS-правка журнала с fence-инкрементом.

    R26-04: для терминальной операции с auditError та же граница чтения делает
    bounded-попытку повторить идемпотентную audit-проекцию (детерминированный _id
    исключает дубль; повторной мутации нет). Сбой БД при перепроекции не уходит в
    500 — auditError остаётся видимым в ответе.
    """
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    doc = await asyncio.to_thread(operations.get_operation, db, guildId, operationId)
    if doc is None:
        raise HTTPException(status_code=404, detail="operation not found")
    doc = await asyncio.to_thread(operations.reconcile_stale, db, doc)
    if doc.get("state") in operations.TERMINAL and doc.get("auditError"):
        try:
            await asyncio.to_thread(mutations.project_operation_audit, db, doc)
            fresh = await asyncio.to_thread(operations.get_operation, db, guildId, operationId)
            if fresh is not None:
                doc = fresh
        except Exception:  # noqa: BLE001 — recovery наблюдаем, а не ещё одна ошибка запроса
            pass
    return operations.public_view(doc)


@router.get("/operations")
async def operation_batch(request: Request, guildId: str, batchId: str) -> dict:
    """O10: батч из нескольких каналов — явные child-статусы, общий success только когда все терминальны."""
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    operations.validate_key(batchId, "batchId")
    docs = await asyncio.to_thread(operations.list_batch, db, guildId, batchId)
    docs = [await asyncio.to_thread(operations.reconcile_stale, db, doc) for doc in docs]
    views = [operations.public_view(doc) for doc in docs]
    pending = [v for v in views if v["state"] not in operations.TERMINAL]
    failed = [v for v in views if v["state"] != "succeeded"]
    return {
        "batchId": batchId,
        "operations": views,
        "complete": not pending,
        "pending": len(pending),
        # T08/R26-03.7: success батча — только когда все обязательные children succeeded
        "allSucceeded": not pending and not failed,
    }
