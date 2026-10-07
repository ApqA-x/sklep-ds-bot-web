"""T08: журнал операций — устойчивый intent ДО внешнего эффекта, идемпотентность, lease/fencing.

Каталог мутаций (T08.1): kind → свойства идемпотентности и сверки результата.
Естественная идемпотентность — повтор запроса к Discord с теми же аргументами не
создаёт второго эффекта (role/timeout/move/disconnect/invite.delete приводят к тому же
состоянию). Для неидемпотентных (message/kick/invite.create) повтор запрещён без
доказательства результата: state остаётся unknown.

Документ операции (T08.2): _id=operationId, guildId, actor, source, kind,
arguments (канонические, без секретов), requestHash, idempotencyKey, state
(requested → executing → succeeded|failed|unknown), timestamps, attempts,
leaseOwner/leaseExpiresAt/fenceVersion (T08.6), result/error, batchId (T08.9).

Дедупликация (T08.3/O02): _id детерминирован из области ключа
sha256(guildId|actorUserId|kind|idempotencyKey) — уникальность _id гарантирует её
и на unit-фейках, и на реальной Mongo без вторичного индекса. TTL-хранение
ключей (T08.12, RETENTION_DAYS) как индекс — зона T10; окно больше UI-повторов.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

COLL_OPERATIONS = "operations"

# O05/O07: рабочий обязан завершить попытку быстрее lease; истёкший lease ≠ «эффекта не было»
LEASE_SECONDS = 90
# R26-03.9: одно обещание контракта с migration M2 (OPERATIONS_TTL_SECONDS в манифесте
# схемы = 90 суток). Раньше код обещал 30 — два разных TTL для одного журнала.
RETENTION_DAYS = 90

# R26-03.9: границы ключей идемпотентности/батча. Ключи приходят из UI и живут в URL-
# запросах статусов — набор символов и длина сверху ограничены (клиентский ключ —
# uuid или «batchId:channelId»), произвольная строка в журнал/URL статусов не попадёт.
KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# Аргументы попадают в audit-проекции и ответы статусов; содержимое файлов — только дigest'ы.
MAX_ARGUMENTS_BYTES = 64 * 1024

# T08.1 каталог: idempotent — повтор того же запроса безвреден; reconcile — как
# доказывать результат после unknown
KINDS: dict[str, dict[str, str]] = {
    "bot.role.grant": {"idempotent": "state", "reconcile": "member-roles"},
    "bot.role.revoke": {"idempotent": "state", "reconcile": "member-roles"},
    "bot.timeout.set": {"idempotent": "state", "reconcile": "member-timeout"},
    "bot.timeout.clear": {"idempotent": "state", "reconcile": "member-timeout"},
    "bot.move": {"idempotent": "state", "reconcile": "member-voice"},
    "bot.disconnect": {"idempotent": "state", "reconcile": "member-voice"},
    "bot.invite.delete": {"idempotent": "state", "reconcile": "invite-absent"},
    "bot.kick": {"idempotent": "none", "reconcile": "manual"},
    "bot.message": {"idempotent": "none", "reconcile": "manual"},  # nonce/enforce_nonce дедупликацию не доказываем
    "bot.invite.create": {"idempotent": "none", "reconcile": "manual"},
    # R26-04: локальные DB-мутации панели тоже ведут durable-след в этом журнале
    # (intent до эффекта; audit — идемпотентная проекция с детерминированным _id).
    "db.settings.patch": {"idempotent": "revision", "reconcile": "settings-document"},
    "db.list.mutate": {"idempotent": "state", "reconcile": "settings-document"},
    "db.stalker.mutate": {"idempotent": "state", "reconcile": "stalker-collection"},
    "db.preset.mutate": {"idempotent": "none", "reconcile": "preset-collection"},
}

TERMINAL = ("succeeded", "failed", "unknown")


def requires_audit(kind: str, state: str) -> bool:
    """Bot outcomes are auditable; local DB changes only after proven success."""
    return kind.startswith("bot.") or (kind.startswith("db.") and state == "succeeded")


def new_id() -> str:
    return uuid.uuid4().hex


def validate_key(value: str, field: str) -> str:
    """R26-03.9: ключ/батч-ид пришёл из клиента — формат и длина проверяются до записи."""
    if not KEY_RE.match(value):
        raise HTTPException(status_code=422, detail=f"invalid {field}")
    return value


def canonical_hash(kind: str, arguments: dict[str, Any]) -> str:
    blob = json.dumps(
        {"kind": kind, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def operation_id(guild_id: str, actor_user_id: str, kind: str, idempotency_key: str) -> str:
    """T08.3: область ключа guild+actor+kind+key. _id = её хэш — повтор того же
    намерения физически не может создать второй документ."""
    blob = "\x1f".join([guild_id, actor_user_id, kind, idempotency_key])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_idempotent(kind: str) -> bool:
    return KINDS.get(kind, {}).get("idempotent") == "state"


def _now() -> datetime:
    return datetime.now(UTC)


def create_intent(
    db: Any,
    *,
    guild_id: str,
    actor: dict[str, str],
    kind: str,
    arguments: dict[str, Any],
    idempotency_key: str,
    batch_id: str | None = None,
    identity: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Устойчиво сохранить намерение (T08.4). Возвращает (документ, created|existing).

    Ошибка записи журнала (кроме duplicate по области ключа) — HTTP 503: ни один
    внешний эффект не вызван. Один ключ с другим payload → 409 (T08.3).
    Секретов в аргументах нет по построению — каталог вызывающих передаёт только ID/тексты.

    R26-03.4: `identity` — каноническая идентичность намерения для requestHash, когда
    часть аргументов выводима из момента запроса (абсолютный дедлайн timeout). Дедлайн
    хранится в аргументах и повторяется при retry/takeover, но не входит в хэш:
    повтор того же намерения позже — то же намерение, а не 409.
    R26-03.9: размер аргументов ограничен — журнал и audit-проекции не должны расти
    из тела запроса произвольно.
    """
    if kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"unknown operation kind: {kind}")
    validate_key(idempotency_key, "idempotency-key")
    if batch_id is not None:
        validate_key(batch_id, "x-batch-id")
    if len(json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")) > MAX_ARGUMENTS_BYTES:
        raise HTTPException(status_code=422, detail="operation arguments too large")
    now = _now()
    op_id = operation_id(guild_id, actor.get("userId", ""), kind, idempotency_key)
    doc = {
        "_id": op_id,
        "guildId": guild_id,
        "actorUserId": actor.get("userId", ""),
        "actorName": actor.get("userName", ""),
        "source": "web",
        "kind": kind,
        "arguments": arguments,
        "requestHash": canonical_hash(kind, identity if identity is not None else arguments),
        "idempotencyKey": idempotency_key,
        "batchId": batch_id,
        "state": "requested",
        "attempts": 0,
        "fenceVersion": 0,
        "leaseOwner": None,
        "leaseExpiresAt": None,
        "createdAt": now,
        "updatedAt": now,
        "finishedAt": None,
        "result": None,
        "error": None,
        "auditError": False,
        "auditState": "not_started",
    }
    collection = db[COLL_OPERATIONS]
    try:
        collection.insert_one(dict(doc))
        return doc, "created"
    except Exception as exc:  # noqa: BLE001 — перечитываем только duplicate-key
        if not _is_duplicate(exc):
            raise HTTPException(status_code=503, detail="operations journal unavailable") from exc
    existing = collection.find_one({"_id": op_id})
    if existing is None:
        raise HTTPException(status_code=503, detail="operations journal unavailable") from exc
    if existing.get("requestHash") != doc["requestHash"]:
        # T08.3: один ключ — только один payload
        raise HTTPException(
            status_code=409,
            detail={"error": "idempotency_conflict", "operationId": op_id, "state": existing.get("state")},
        )
    return existing, "existing"


def _is_duplicate(exc: Exception) -> bool:
    if getattr(exc, "code", None) == 11000:
        return True
    return "duplicatekey" in type(exc).__name__.lower()


def lease_expired(doc: dict[str, Any]) -> bool:
    expires = doc.get("leaseExpiresAt")
    if expires is None:
        return True
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    elif expires.tzinfo is None:  # BSON-datetime приезжает наивным при tz_aware=False (урок T09)
        expires = expires.replace(tzinfo=UTC)
    return expires <= _now()


def claim(db: Any, operation_id: str, *, owner: str | None = None) -> tuple[dict[str, Any], str] | None:
    """Атомарный claim с lease и fence (T08.6). None — операцию ведёт активный worker.

    Возвращает (документ, режим):
      "fresh"    — первый запуск из requested, внешнего эффекта ещё не было;
      "takeover" — перехват operations-документа с истёкшим lease: эффект НЕ доказан,
                   неидемпотентный kind запрещён к повтору вслепую (O05/O07).
    """
    owner = owner or new_id()
    now = _now()
    collection = db[COLL_OPERATIONS]
    lease = {
        "$set": {
            "state": "executing",
            "leaseOwner": owner,
            "leaseExpiresAt": now + timedelta(seconds=LEASE_SECONDS),
            "updatedAt": now,
        },
        "$inc": {"fenceVersion": 1, "attempts": 1},
    }
    fresh = collection.update_one({"_id": operation_id, "state": "requested"}, lease)
    if fresh.matched_count == 1:
        return collection.find_one({"_id": operation_id}), "fresh"
    stale = collection.update_one(
        {"_id": operation_id, "state": "executing", "leaseExpiresAt": {"$lte": now}}, lease
    )
    if stale.matched_count == 1:
        return collection.find_one({"_id": operation_id}), "takeover"
    return None


def finish(
    db: Any,
    operation_id: str,
    claimed: dict[str, Any],
    state: str,
    *,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> bool:
    """Финализация только с совпадающим fence/lease-owner (O07): просроченный worker
    не может завершить новую попытку другого worker. Возвращает False в этом случае."""
    if state not in TERMINAL:
        raise ValueError(f"not a terminal state: {state}")
    now = _now()
    audit_required = requires_audit(str(claimed.get("kind") or ""), state)
    update: dict[str, Any] = {
        "$set": {
            "state": state,
            "result": result,
            "error": error,
            "leaseExpiresAt": None,
            "updatedAt": now,
            "finishedAt": now,
            # The audit marker and terminal state must be one atomic CAS.
            "auditState": "pending" if audit_required else "not_required",
            "auditError": audit_required,
        }
    }
    update_filter = {
        "_id": operation_id,
        "fenceVersion": claimed.get("fenceVersion"),
        "leaseOwner": claimed.get("leaseOwner"),
        "state": "executing",
    }
    outcome = db[COLL_OPERATIONS].update_one(update_filter, update)
    return outcome.matched_count == 1


def mark_audit_error(db: Any, operation_id: str) -> None:
    """O08: факт операции устойчив; ошибка audit-проекции остаётся видимой меткой."""
    db[COLL_OPERATIONS].update_one(
        {"_id": operation_id, "state": {"$in": list(TERMINAL)}, "auditState": {"$ne": "complete"}},
        {"$set": {"auditError": True, "auditState": "error", "updatedAt": _now()}},
    )


def mark_audit_complete(db: Any, operation_id: str) -> None:
    """Only call after a durable audit insert or its deterministic duplicate."""
    db[COLL_OPERATIONS].update_one(
        {"_id": operation_id, "state": {"$in": list(TERMINAL)}},
        {"$set": {"auditError": False, "auditState": "complete", "updatedAt": _now()}},
    )


def read_current(db: Any, operation_id: str) -> dict[str, Any] | None:
    """Фактическое состояние после отказа финала (R26-03.1): пользователю показываем
    журнал, а не предположение завершившегося внешнего эффекта."""
    return db[COLL_OPERATIONS].find_one({"_id": operation_id})


def classify_transport(exc: Exception) -> tuple[str, bool]:
    """(classification, definitely_no_effect). Соединение не установлено — запрос не ушёл;
    всё остальное (timeout/обрыв после отправки) — unknown, эффект недоказуем (O05)."""
    name = type(exc).__name__
    if name in ("ClientConnectorError", "ClientProxyConnectionError", "InvalidURL"):
        return "connection_not_established", True
    if name in ("ServerDisconnectedError", "ServerTimeoutError", "ClientOSError", "ClientPayloadError"):
        return "connection_lost_after_send", False
    if name == "TimeoutError" or name == "asyncio.TimeoutError":
        return "timeout_after_send", False
    return "transport_error", False


def classify_http_status(status: int) -> tuple[str, bool]:
    """R26-03.2: не всякий >=400 доказывает отсутствие эффекта.

    4xx — отказDiscord до применения (definite rejection): эффект точно не наступил.
    5xx/иной нетерминальный ответ — запрос дошёл до Discord, запись могла произойти
    до сбоя на его стороне: исход недоказуем, операция остаётся unknown.
    """
    if 400 <= status < 500:
        return "discord_rejected", True
    return "discord_server_error", False


def extract_result(kind: str, status: int, payload: Any) -> dict[str, Any]:
    """R26-03.4: детерминированный результат для первого успеха и для replay.

    message.id / invite.code — то, что показывает экран; сохраняем в журнал, повтор
    запроса с тем же ключом возвращает ровно тот же результат без нового вызова.
    """
    result: dict[str, Any] = {"discordStatus": status}
    if isinstance(payload, dict):
        if payload.get("id"):
            result["discordResourceId"] = str(payload["id"])
        if kind == "bot.invite.create" and payload.get("code"):
            result["inviteCode"] = str(payload["code"])
            if payload.get("url"):
                result["inviteUrl"] = str(payload["url"])
    return result


def audit_entry_id(guild_id: str, operation_id: str) -> str:
    """R26-04: детерминированный _id audit-проекции — повторная проекция той же
    операции физически не создаёт дубль (unique по _id держится и на standalone Mongo)."""
    return hashlib.sha256(f"audit\u001f{guild_id}\u001f{operation_id}".encode()).hexdigest()


def file_digest(blob: bytes) -> str:
    """sha256 содержимого — разные файлы одинаковой длины обязаны давать разный
    канонический запрос (R26-03.3)."""
    return hashlib.sha256(blob).hexdigest()


def reconcile_stale(db: Any, doc: dict[str, Any]) -> dict[str, Any]:
    """R26-03.5: наблюдаемое bounded-recovery для «executing с истёкшим lease», если
    владелец так и не вернулся. Чтение статуса не создаёт внешних эффектов — оно лишь
    переводит недоказанную попытку в unknown с fence-инкрементом (прежний рабочий
    теряет право финализации). unknown остаётся видимым пользователю до доказанной
    или ручной сверки; слепой повтор тем же ключом отвергается replay-веткой.
    """
    if doc.get("state") != "executing" or not lease_expired(doc):
        return doc
    now = _now()
    outcome = db[COLL_OPERATIONS].update_one(
        {
            "_id": doc["_id"],
            "state": "executing",
            "fenceVersion": doc.get("fenceVersion"),
            "leaseExpiresAt": {"$lte": now},
        },
        {
            "$set": {
                "state": "unknown",
                "error": {"classification": "lease_expired_unproven"},
                "leaseExpiresAt": None,
                "updatedAt": now,
                "finishedAt": now,
                "auditState": "pending" if requires_audit(str(doc.get("kind") or ""), "unknown") else "not_required",
                "auditError": requires_audit(str(doc.get("kind") or ""), "unknown"),
            },
            "$inc": {"fenceVersion": 1},
        },
    )
    if outcome.matched_count == 1:
        doc = dict(doc)
        doc["state"] = "unknown"
        doc["error"] = {"classification": "lease_expired_unproven"}
        doc["finishedAt"] = now
        doc["auditState"] = "pending" if requires_audit(str(doc.get("kind") or ""), "unknown") else "not_required"
        doc["auditError"] = doc["auditState"] == "pending"
    return doc


def public_view(doc: dict[str, Any]) -> dict[str, Any]:
    """Ответ status-эндпоинта: факты без служебных lease-деталей (T08.9)."""
    return {
        "operationId": doc.get("_id"),
        "kind": doc.get("kind"),
        "state": doc.get("state"),
        "attempts": doc.get("attempts"),
        "batchId": doc.get("batchId"),
        "createdAt": doc.get("createdAt"),
        "finishedAt": doc.get("finishedAt"),
        "result": doc.get("result"),
        "error": doc.get("error"),
        "audited": (
            doc.get("auditState") in {"complete", "not_required"}
            if "auditState" in doc
            else doc.get("state") in TERMINAL
            and not requires_audit(str(doc.get("kind") or ""), str(doc.get("state") or ""))
        ),
    }


def get_operation(db: Any, guild_id: str, op_id: str) -> dict[str, Any] | None:
    """Guild-scope (T04): чужая операция не различима от несуществующей."""
    from .limits import timeout_kwargs

    doc = db[COLL_OPERATIONS].find_one({"_id": op_id}, **timeout_kwargs())
    if doc is None or doc.get("guildId") != guild_id:
        return None
    return doc


def list_batch(db: Any, guild_id: str, batch_id: str) -> list[dict[str, Any]]:
    from .limits import timeout_kwargs

    docs = list(db[COLL_OPERATIONS].find({"guildId": guild_id, "batchId": batch_id}, **timeout_kwargs()))
    docs.sort(key=lambda d: d.get("createdAt") or _now())
    return docs
