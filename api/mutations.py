from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from . import operations, queries
from .models import ChatPresetAction, GuildSettingsPatch, ListMemberAction, StalkerAction

COLL_AUDIT = "web_audit_logs"
COLL_STALKER = "stalker_subscriptions"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return queries._iso(value)
    return value


def record_audit(
    db: Any,
    *,
    guild_id: str,
    actor: dict[str, str],
    action: str,
    before: Any = None,
    after: Any = None,
    ok: bool = True,
    origin: str = "web",
    operation_id: str | None = None,
) -> None:
    entry = {
        "guildId": guild_id,
        "actorUserId": actor.get("userId", ""),
        "actorName": actor.get("userName", ""),
        "action": action,
        "before": _jsonable(before),
        "after": _jsonable(after),
        "ok": ok,
        "at": _utc_now(),
        "source": "web",
        "origin": origin,
    }
    if operation_id:
        # T08.10: audit — проекция фактов operations; старые записи без поля совместимы.
        # R26-04: _id детерминирован (guild|operationId) — повторная проекция той же
        # операции не создаёт дубль даже на standalone Mongo без транзакций.
        entry["_id"] = operations.audit_entry_id(guild_id, operation_id)
        entry["operationId"] = operation_id
    try:
        db[COLL_AUDIT].insert_one(entry)
    except Exception as exc:  # noqa: BLE001 — duplicate по детерминированному _id идемпотентен
        if operations._is_duplicate(exc):
            # проекция уже существует — идемпотентный повтор считается успехом
            return
        raise


# --- R26-04: durable-журнал локальных мутаций --------------------------------
#
# Дефект ревью: CAS-запись проходит, audit-вставка падает — изменение остаётся без
# восстанавливаемого следа. Порядок теперь: intent в operations ДО эффекта (сбой
# журнала = 503 и ноль мутаций), finish ПОСЛЕ (применённые before/after/revision —
# в result), audit — идемпотентная проекция с детерминированным _id. Сбой проекции
# остаётся видимым (auditError на журнале) и исправляется reproject без повторной
# мутации.


def _journal_effect(
    db: Any,
    *,
    guild_id: str,
    actor: dict[str, str],
    kind: str,
    arguments: dict[str, Any],
    effect: Any,
) -> tuple[str, str]:
    """effect() -> ("applied"|"conflict", before, after). Возвращает (status, op_id).

    Ни одна мутация не исполняется, пока намерение не записано устойчиво.
    """
    doc, _created = operations.create_intent(
        db,
        guild_id=guild_id,
        actor=actor,
        kind=kind,
        arguments=arguments,
        idempotency_key=operations.new_id(),
    )
    op_id = doc["_id"]
    claimed_pair = operations.claim(db, op_id)
    if claimed_pair is None:  # документ только что создан — активность чужого workerа невозможна
        raise HTTPException(status_code=503, detail="operations journal unavailable")
    claimed = claimed_pair[0]
    try:
        status, before, after = effect()
    except Exception as exc:  # noqa: BLE001 — эффект мог примениться наполовине: только unknown
        operations.finish(db, op_id, claimed, "unknown", error={"classification": "effect_exception"})
        raise exc
    if status == "conflict":
        operations.finish(db, op_id, claimed, "failed", error={"classification": "revision_conflict"})
        return status, op_id
    operations.finish(
        db,
        op_id,
        claimed,
        "succeeded",
        result={"before": _jsonable(before), "after": _jsonable(after)},
    )
    project_operation_audit(db, db[operations.COLL_OPERATIONS].find_one({"_id": op_id}) or doc)
    return status, op_id


# kind бот-действий → имя audit-действия (совпадает с `action` в api/bot.py::_action)
_BOT_AUDIT_ACTION = {
    "bot.role.grant": "role",
    "bot.role.revoke": "role",
    "bot.timeout.set": "timeout",
    "bot.timeout.clear": "timeout",
    "bot.move": "move",
    "bot.disconnect": "disconnect",
    "bot.kick": "kick",
    "bot.message": "message",
    "bot.invite.create": "invite.create",
    "bot.invite.delete": "invite.delete",
}


def rebuild_audit_entry(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    """Аудит-запись из документа журнала — единственный восстановимый источник."""
    if not doc or doc.get("state") not in operations.TERMINAL:
        return None
    kind = doc.get("kind") or ""
    guild = doc.get("guildId") or ""
    op_id = doc.get("_id") or ""
    args = doc.get("arguments") or {}
    base = {
        "_id": operations.audit_entry_id(guild, op_id),
        "guildId": guild,
        "actorUserId": doc.get("actorUserId", ""),
        "actorName": doc.get("actorName", ""),
        "source": doc.get("source", "web"),
        "origin": doc.get("source", "web"),
        "operationId": op_id,
        "at": doc.get("finishedAt") or doc.get("updatedAt") or _utc_now(),
    }
    if kind.startswith("db."):
        if doc.get("state") != "succeeded":  # конфликт/сбой — эффекта не было
            return None
        result = doc.get("result") or {}
        return {
            **base,
            "action": args.get("auditAction") or kind,
            "before": result.get("before"),
            "after": result.get("after"),
            "ok": True,
        }
    if kind in _BOT_AUDIT_ACTION:
        state = doc.get("state")
        ok = state == "succeeded"
        result = doc.get("result") or {}
        error = doc.get("error") or {}
        detail = None
        if ok:
            discord_status = result.get("discordStatus")
        else:
            discord_status = error.get("discordStatus")
            detail = error.get("body") or (
                {"classification": error.get("classification")} if error.get("classification") else None
            )
        return {
            **base,
            "action": f"bot.{_BOT_AUDIT_ACTION[kind]}",
            "before": None,
            "after": {**args, "discordStatus": discord_status, "detail": detail},
            "ok": ok,
        }
    return None


def project_operation_audit(db: Any, doc: dict[str, Any]) -> bool:
    """Идемпотентная проекция журнала в аудит; False — след журнала сохранён,
    но проекция требует повторного reproject (auditError)."""
    entry = rebuild_audit_entry(doc)
    if entry is None:
        return False
    try:
        db[COLL_AUDIT].insert_one(entry)
    except Exception as exc:  # noqa: BLE001
        if not operations._is_duplicate(exc):
            operations.mark_audit_error(db, doc["_id"])
            return False
        # уже спроецировано — дубль по детерминированному _id это успех
    db[operations.COLL_OPERATIONS].update_one(
        {"_id": doc["_id"], "auditError": True},
        {"$set": {"auditError": False, "updatedAt": _utc_now()}},
    )
    return True


def reproject_pending_audits(db: Any, *, guild_id: str | None = None, limit: int = 200) -> dict[str, int]:
    """Bounded-восстановление пропущенных audit-проекций (R26-04). Повтор мутаций
    не выполняет — только перепроецирует уже терминальные записи журнала; дубли
    отсекает детерминированный _id."""
    where: dict[str, Any] = {"auditError": True, "state": {"$in": list(operations.TERMINAL)}}
    if guild_id:
        where["guildId"] = guild_id
    docs = list(db[operations.COLL_OPERATIONS].find(where, limit=limit))
    recovered = sum(1 for doc in docs if project_operation_audit(db, doc))
    return {"considered": len(docs), "recovered": recovered, "remaining": len(docs) - recovered}


def patch_guild_settings(
    db: Any,
    guild_id: str,
    patch: GuildSettingsPatch,
    actor: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    """Returns (status, fresh document). status: updated | conflict.

    T06: одна атомарная CAS-запись с фильтром {_id, revision: expected} и $inc
    revision. Ноль совпадений — конфликт, а не безусловная запись поверх.
    Создание отсутствующего документа — отдельный путь с уникальным _id;
    конкурентный duplicate превращается в конфликт.

    R26-04: намерение (поля+expectedRevision) пишется в журнал до CAS; применённые
    before/after — в result; audit — идемпотентная проекция. `before` соответствует
    применённой revision: снимок читается под тем же CAS-фильтром, а победитель
    конкурентной записи меняется до совпадения фильтра (иначе был бы conflict).
    """
    fields = patch.mongo_set()
    expected = patch.expectedRevision

    def effect() -> tuple[str, Any, Any]:
        coll = db[queries.COLL_GUILD_SETTINGS]
        doc = coll.find_one({"_id": guild_id}) or {}
        now = _utc_now()
        old_values = {key: doc.get(key) for key in fields}
        if not doc:
            if expected != 0:
                # клиент читал revision N, документа нет — это конфликт, не тихое создание
                return "conflict", None, None
            try:
                coll.update_one(
                    {"_id": guild_id, "revision": {"$exists": False}},
                    {
                        "$set": {**fields, "updatedAt": now},
                        "$inc": {"revision": 1},
                        "$setOnInsert": {"createdAt": now},
                    },
                    upsert=True,
                )
            except DuplicateKeyError:
                return "conflict", None, None
            # создание: документ не существовал → применённая revision ровно 0→1
            return "applied", {"revision": 0, **old_values}, {"revision": 1, **fields}
        result = coll.update_one(
            {"_id": guild_id, "revision": expected},
            {"$set": {**fields, "updatedAt": now}, "$inc": {"revision": 1}},
        )
        if result.matched_count == 0:
            return "conflict", None, None
        # CAS-фильтр по revision=expected гарантирует: снимок читался именно той
        # revision, которая применена (иначе это conflict, а не «тихая» запись поверх)
        return "applied", {"revision": expected, **old_values}, {"revision": expected + 1, **fields}

    status, _op_id = _journal_effect(
        db,
        guild_id=guild_id,
        actor=actor,
        kind="db.settings.patch",
        arguments={"fields": fields, "expectedRevision": expected, "auditAction": "settings.patch"},
        effect=effect,
    )
    if status == "conflict":
        return "conflict", queries.settings_document(db, guild_id)
    return "updated", queries.settings_document(db, guild_id)


LIST_FIELDS = {"trusted": "trustedUserIds", "autoUnmute": "autoUnmuteUserIds"}


def mutate_id_list(
    db: Any,
    guild_id: str,
    field: str,
    body: ListMemberAction,
    actor: dict[str, str],
) -> dict[str, Any] | None:
    """Atomic $addToSet/$pull of one snowflake in a guild_settings id list.

    Т06.5: контракт операции — намерение над одним элементом, старая revision не
    требуется; счётчик revision увеличивается атомарно с самим изменением, чтобы
    полный patch на устаревшем снимке списка дал конфликт, а не затирание.

    R26-04: мутация под durable-журналом (db.list.mutate) — сбой audit-проекции
    остаётся восстанавливаемым следом, а не «успех без следа».
    """
    user_id = body.userId
    operator = "$addToSet" if body.action == "add" else "$pull"

    def effect() -> tuple[str, Any, Any]:
        now = _utc_now()
        doc = db[queries.COLL_GUILD_SETTINGS].find_one({"_id": guild_id}) or {}
        before_ids = list(doc.get(field) or [])
        db[queries.COLL_GUILD_SETTINGS].update_one(
            {"_id": guild_id},
            {
                operator: {field: user_id},
                "$set": {"updatedAt": now},
                "$inc": {"revision": 1},
                "$setOnInsert": {"createdAt": now},
            },
            upsert=True,
        )
        fresh = db[queries.COLL_GUILD_SETTINGS].find_one({"_id": guild_id}) or {}
        raw_after = fresh.get(field)
        after_ids = list(raw_after) if isinstance(raw_after, list) else before_ids
        return "applied", {"ids": before_ids}, {"ids": after_ids, "userId": user_id}

    _journal_effect(
        db,
        guild_id=guild_id,
        actor=actor,
        kind="db.list.mutate",
        arguments={
            "field": field,
            "userId": user_id,
            "listAction": body.action,
            "auditAction": f"{field}.{body.action}",
        },
        effect=effect,
    )
    return queries.settings_document(db, guild_id)


def mutate_stalker(db: Any, guild_id: str, body: StalkerAction, actor: dict[str, str]) -> dict[str, Any]:
    sub_id = f"{guild_id}:{body.watcherUserId}:{body.targetUserId}"

    def effect() -> tuple[str, Any, Any]:
        now = _utc_now()
        if body.action == "add":
            db[COLL_STALKER].update_one(
                {"_id": sub_id},
                {
                    "$set": {
                        "guildId": guild_id,
                        "watcherUserId": body.watcherUserId,
                        "targetUserId": body.targetUserId,
                        "updatedAt": now,
                    },
                    "$setOnInsert": {"createdAt": now},
                },
                upsert=True,
            )
        else:
            db[COLL_STALKER].delete_one({"_id": sub_id, "guildId": guild_id})
        return "applied", None, {"subscriptionId": sub_id}

    _journal_effect(
        db,
        guild_id=guild_id,
        actor=actor,
        kind="db.stalker.mutate",
        arguments={
            "subscriptionId": sub_id,
            "watcherUserId": body.watcherUserId,
            "targetUserId": body.targetUserId,
            "stalkerAction": body.action,
            "auditAction": f"stalker.{body.action}",
        },
        effect=effect,
    )
    return {"ok": True, "subscriptionId": sub_id}


_SNOWFLAKE = re.compile(r"^\d{5,25}$")


def mutate_chat_preset(
    db: Any, guild_id: str, body: ChatPresetAction, actor: dict[str, str]
) -> dict[str, Any]:
    """add записывает текстовый или embed-пресет в chat_presets, remove удаляет по id гильдии.

    R26-04: невалидный ввод (текст вне 1..2000) отсекается ДО журнала — неверный
    запрос не оставляет намерения; uuid пресета генерируется тоже до intent, чтобы
    arguments были полными и повтор сверки знал конкретный _id эффекта.
    """
    now = _utc_now()
    doc: dict[str, Any] | None = None
    if body.action == "add":
        preset_id = uuid.uuid4().hex
        doc = {
            "_id": preset_id,
            "guildId": guild_id,
            "kind": body.kind,
            "name": body.name,
            "channelIds": list(body.channelIds or []),
            "createdAt": now,
        }
        if body.kind == "embed":
            assert body.embed is not None
            doc["embed"] = {k: v for k, v in body.embed.model_dump().items() if v is not None}
        else:
            text = (body.text or "").strip()
            if not 1 <= len(text) <= 2000:
                raise ValueError("chat preset text must be 1..2000 chars")
            doc["text"] = text
    else:
        preset_id = (body.presetId or "").strip()

    arguments: dict[str, Any] = {
        "presetId": preset_id,
        "presetAction": body.action,
        "auditAction": f"chatPreset.{body.action}",
    }
    if doc is not None:
        arguments["kind"] = body.kind
        arguments["name"] = body.name

    def effect() -> tuple[str, Any, Any]:
        if doc is not None:
            db[queries.COLL_CHAT_PRESETS].insert_one(doc)
            return "applied", None, {
                "presetId": preset_id,
                "kind": body.kind,
                "text": body.text,
                "name": body.name,
                "channelIds": body.channelIds,
            }
        existing = db[queries.COLL_CHAT_PRESETS].find_one({"_id": preset_id, "guildId": guild_id}) or {}
        db[queries.COLL_CHAT_PRESETS].delete_one({"_id": preset_id, "guildId": guild_id})
        return "applied", None, {
            "presetId": preset_id,
            "kind": existing.get("kind", "text"),
            "text": None,
            "name": existing.get("name"),
            "channelIds": existing.get("channelIds"),
        }

    _journal_effect(
        db,
        guild_id=guild_id,
        actor=actor,
        kind="db.preset.mutate",
        arguments=arguments,
        effect=effect,
    )
    return {"ok": True, "presetId": preset_id}


def _target_filter(user_id: str) -> dict[str, Any]:
    """«Над кем» совершено действие: after.userId либо target из stalker-подписки."""
    return {
        "$or": [
            {"after.userId": user_id},
            {"after.subscriptionId": {"$regex": f":{re.escape(user_id)}$"}},
        ]
    }


def audit_page(
    db: Any,
    guild_id: str,
    page: int,
    size: int,
    origin: str | None = None,
    *,
    target_user: str | None = None,
    action: str | None = None,
    ok: bool | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    sort: str = "desc",
) -> dict[str, Any]:
    where: dict[str, Any] = {"guildId": guild_id}
    if origin:
        where["origin"] = origin
    if action:
        where["action"] = action
    if ok is not None:
        where["ok"] = ok
    if target_user:
        where["$and"] = [_target_filter(target_user)]
    bounds: dict[str, Any] = {}
    if date_from is not None:
        bounds["$gte"] = date_from
    if date_to is not None:
        bounds["$lte"] = date_to
    if bounds:
        where["at"] = bounds
    direction = 1 if sort == "asc" else -1
    from .limits import command_timeout_kwargs, timeout_kwargs

    budget = timeout_kwargs()
    total = db[COLL_AUDIT].count_documents(where, **command_timeout_kwargs())
    docs = db[COLL_AUDIT].find(
        where, sort=[("at", direction)], skip=(page - 1) * size, limit=size, **budget
    )
    return {
        "guildId": guild_id,
        "page": page,
        "size": size,
        "total": int(total),
        "origin": origin,
        "items": [
            {
                "actorUserId": doc.get("actorUserId"),
                "actorName": doc.get("actorName"),
                "action": doc.get("action"),
                "before": doc.get("before"),
                "after": doc.get("after"),
                "ok": doc.get("ok", True),
                "at": queries._iso(doc.get("at")),
                "origin": doc.get("origin", "web"),
            }
            for doc in docs
        ],
    }


def audit_actions(db: Any, guild_id: str) -> list[dict[str, Any]]:
    """Distinct значения action с количеством — наполняет фильтр в UI."""
    from .limits import command_timeout_kwargs

    rows = db[COLL_AUDIT].aggregate(
        [
            {"$match": {"guildId": guild_id}},
            {"$group": {"_id": "$action", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ],
        **command_timeout_kwargs(),
    )
    return [{"action": str(row["_id"]), "count": int(row.get("n") or 0)} for row in rows if row.get("_id")]
