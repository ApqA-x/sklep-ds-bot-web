from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo.errors import DuplicateKeyError

from . import queries
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
        # T08.10: audit — проекция фактов operations; старые записи без поля совместимы
        entry["operationId"] = operation_id
    db[COLL_AUDIT].insert_one(entry)


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
    """
    fields = patch.mongo_set()
    expected = patch.expectedRevision
    coll = db[queries.COLL_GUILD_SETTINGS]
    doc = coll.find_one({"_id": guild_id}) or {}
    now = _utc_now()
    before = {key: doc.get(key) for key in fields}
    if not doc:
        if expected != 0:
            # клиент читалrevision N, документа нет — это конфликт, не тихое создание
            return "conflict", queries.settings_document(db, guild_id)
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
            return "conflict", queries.settings_document(db, guild_id)
    else:
        result = coll.update_one(
            {"_id": guild_id, "revision": expected},
            {"$set": {**fields, "updatedAt": now}, "$inc": {"revision": 1}},
        )
        if result.matched_count == 0:
            return "conflict", queries.settings_document(db, guild_id)

    record_audit(
        db,
        guild_id=guild_id,
        actor=actor,
        action="settings.patch",
        before=before,
        after=fields,
    )
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
    """
    user_id = body.userId
    operator = "$addToSet" if body.action == "add" else "$pull"
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
    record_audit(
        db,
        guild_id=guild_id,
        actor=actor,
        action=f"{field}.{body.action}",
        before={"ids": before_ids},
        after={"ids": after_ids, "userId": user_id},
    )
    return queries.settings_document(db, guild_id)


def mutate_stalker(db: Any, guild_id: str, body: StalkerAction, actor: dict[str, str]) -> dict[str, Any]:
    sub_id = f"{guild_id}:{body.watcherUserId}:{body.targetUserId}"
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
    record_audit(
        db,
        guild_id=guild_id,
        actor=actor,
        action=f"stalker.{body.action}",
        after={"subscriptionId": sub_id},
    )
    return {"ok": True, "subscriptionId": sub_id}


_SNOWFLAKE = re.compile(r"^\d{5,25}$")


def mutate_chat_preset(
    db: Any, guild_id: str, body: ChatPresetAction, actor: dict[str, str]
) -> dict[str, Any]:
    """add записывает текстовый или embed-пресет в chat_presets, remove удаляет по id гильдии."""
    now = _utc_now()
    if body.action == "add":
        preset_id = uuid.uuid4().hex
        doc: dict[str, Any] = {
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
        db[queries.COLL_CHAT_PRESETS].insert_one(doc)
    else:
        preset_id = (body.presetId or "").strip()
        existing = db[queries.COLL_CHAT_PRESETS].find_one({"_id": preset_id, "guildId": guild_id}) or {}
        db[queries.COLL_CHAT_PRESETS].delete_one({"_id": preset_id, "guildId": guild_id})
    record_audit(
        db,
        guild_id=guild_id,
        actor=actor,
        action=f"chatPreset.{body.action}",
        after={
            "presetId": preset_id,
            "kind": body.kind if body.action == "add" else existing.get("kind", "text"),
            "text": body.text if body.action == "add" else None,
            "name": body.name if body.action == "add" else existing.get("name"),
            "channelIds": body.channelIds if body.action == "add" else existing.get("channelIds"),
        },
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
    total = db[COLL_AUDIT].count_documents(where)
    docs = db[COLL_AUDIT].find(where, sort=[("at", direction)], skip=(page - 1) * size, limit=size)
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
    rows = db[COLL_AUDIT].aggregate(
        [
            {"$match": {"guildId": guild_id}},
            {"$group": {"_id": "$action", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ]
    )
    return [{"action": str(row["_id"]), "count": int(row.get("n") or 0)} for row in rows if row.get("_id")]
