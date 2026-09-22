from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import queries
from .models import GuildSettingsPatch, ListMemberAction, StalkerAction

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
        return value.isoformat().replace("+00:00", "Z")
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
) -> None:
    db[COLL_AUDIT].insert_one(
        {
            "guildId": guild_id,
            "actorUserId": actor.get("userId", ""),
            "actorName": actor.get("userName", ""),
            "action": action,
            "before": _jsonable(before),
            "after": _jsonable(after),
            "ok": ok,
            "at": _utc_now(),
            "source": "web",
        }
    )


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def patch_guild_settings(
    db: Any,
    guild_id: str,
    patch: GuildSettingsPatch,
    actor: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    """Returns (status, fresh document). status: updated | conflict."""
    fields = patch.mongo_set()
    doc = db[queries.COLL_GUILD_SETTINGS].find_one({"_id": guild_id}) or {}

    if patch.expectedUpdatedAt is not None:
        current = doc.get("updatedAt")
        expected = _parse_iso(patch.expectedUpdatedAt)
        if current is not None and expected is not None:
            current_dt = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
            if abs((current_dt - expected).total_seconds()) > 1:
                return "conflict", queries.settings_document(db, guild_id)

    now = _utc_now()
    before = {key: doc.get(key) for key in fields}
    if not doc:
        db[queries.COLL_GUILD_SETTINGS].update_one(
            {"_id": guild_id},
            {"$set": {**fields, "updatedAt": now}, "$setOnInsert": {"createdAt": now}},
            upsert=True,
        )
    else:
        db[queries.COLL_GUILD_SETTINGS].update_one({"_id": guild_id}, {"$set": {**fields, "updatedAt": now}})

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
    """Atomic $addToSet/$pull of one snowflake in a guild_settings id list."""
    user_id = body.userId
    operator = "$addToSet" if body.action == "add" else "$pull"
    now = _utc_now()
    doc = db[queries.COLL_GUILD_SETTINGS].find_one({"_id": guild_id}) or {}
    before_ids = list(doc.get(field) or [])
    db[queries.COLL_GUILD_SETTINGS].update_one(
        {"_id": guild_id},
        {operator: {field: user_id}, "$set": {"updatedAt": now}, "$setOnInsert": {"createdAt": now}},
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


def audit_page(db: Any, guild_id: str, page: int, size: int) -> dict[str, Any]:
    where = {"guildId": guild_id}
    total = db[COLL_AUDIT].count_documents(where)
    docs = db[COLL_AUDIT].find(where, sort=[("at", -1)], skip=(page - 1) * size, limit=size)
    return {
        "guildId": guild_id,
        "page": page,
        "size": size,
        "total": int(total),
        "items": [
            {
                "actorUserId": doc.get("actorUserId"),
                "actorName": doc.get("actorName"),
                "action": doc.get("action"),
                "before": doc.get("before"),
                "after": doc.get("after"),
                "ok": doc.get("ok", True),
                "at": queries._iso(doc.get("at")),
            }
            for doc in docs
        ],
    }
