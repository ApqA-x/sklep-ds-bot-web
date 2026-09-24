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
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

COLL_OPERATIONS = "operations"

# O05/O07: рабочий обязан завершить попытку быстрее lease; истёкший lease ≠ «эффекта не было»
LEASE_SECONDS = 90
# O12 (T08.12): срок жизни ключей идемпотентности должен переживать окно повторов UI/очередей
RETENTION_DAYS = 30

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
}

TERMINAL = ("succeeded", "failed", "unknown")


def new_id() -> str:
    return uuid.uuid4().hex


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
) -> tuple[dict[str, Any], str]:
    """Устойчиво сохранить намерение (T08.4). Возвращает (документ, created|existing).

    Ошибка записи журнала (кроме duplicate по области ключа) — HTTP 503: ни один
    внешний эффект не вызван. Один ключ с другим payload → 409 (T08.3).
    Секретов в аргументах нет по построению — каталог вызывающих передаёт только ID/тексты.
    """
    if kind not in KINDS:
        raise HTTPException(status_code=422, detail=f"unknown operation kind: {kind}")
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
        "requestHash": canonical_hash(kind, arguments),
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
    update: dict[str, Any] = {
        "$set": {
            "state": state,
            "result": result,
            "error": error,
            "leaseExpiresAt": None,
            "updatedAt": now,
            "finishedAt": now,
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
        {"_id": operation_id},
        {"$set": {"auditError": True, "updatedAt": _now()}},
    )


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
        "audited": not doc.get("auditError", False),
    }


def get_operation(db: Any, guild_id: str, op_id: str) -> dict[str, Any] | None:
    """Guild-scope (T04): чужая операция не различима от несуществующей."""
    doc = db[COLL_OPERATIONS].find_one({"_id": op_id})
    if doc is None or doc.get("guildId") != guild_id:
        return None
    return doc


def list_batch(db: Any, guild_id: str, batch_id: str) -> list[dict[str, Any]]:
    docs = list(db[COLL_OPERATIONS].find({"guildId": guild_id, "batchId": batch_id}))
    docs.sort(key=lambda d: d.get("createdAt") or _now())
    return docs
