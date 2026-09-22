from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

COLL_SESSIONS = "voice_sessions"
COLL_PARTICIPANTS = "voice_session_participants"
COLL_GUILD_SETTINGS = "guild_settings"
COLL_JOIN_ATTRIBUTIONS = "member_join_attributions"
COLL_JOIN_STATE = "member_join_state"
COLL_ROLE_STATE = "member_role_state"
COLL_NICKNAME_HISTORY = "member_nickname_history"
COLL_INVITE_CATALOG = "invite_catalog"

PERIODS: dict[str, timedelta | None] = {
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}

ACTIVE_MEMBER_SEARCH_WINDOW = timedelta(days=90)
MAX_INVITE_ROWS = 200
MAX_PROFILE_NICKNAMES = 20


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def period_cutoff(period: str) -> datetime | None:
    delta = PERIODS[period]
    return None if delta is None else _utc_now() - delta


class TTLCache:
    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple, tuple[float, Any]] = {}

    def get(self, key: tuple) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > self._ttl:
            del self._entries[key]
            return None
        return value

    def set(self, key: tuple, value: Any) -> None:
        self._entries[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._entries.clear()


_leaderboard_cache = TTLCache()
_invites_cache = TTLCache()


def _iso(value: Any) -> Any:
    return value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else value


# --- pipeline builders (pure, unit-tested without a database) ---


def build_leaderboard_pipeline(guild_id: str, cutoff: datetime | None, limit: int) -> list[dict]:
    match: dict[str, Any] = {"guildId": guild_id}
    if cutoff is not None:
        match["joinedAt"] = {"$gte": cutoff}
    return [
        {"$match": match},
        {"$sort": {"joinedAt": 1}},
        {
            "$group": {
                "_id": "$userId",
                "userName": {"$last": "$userName"},
                "totalMs": {"$sum": "$durationMs"},
                "appearances": {"$sum": 1},
            }
        },
        {"$sort": {"totalMs": -1}},
        {"$limit": limit},
    ]


def build_user_totals_pipeline(guild_id: str, user_id: str, cutoff: datetime | None) -> list[dict]:
    match: dict[str, Any] = {"guildId": guild_id, "userId": user_id}
    if cutoff is not None:
        match["joinedAt"] = {"$gte": cutoff}
    return [
        {"$match": match},
        {"$group": {"_id": None, "totalMs": {"$sum": "$durationMs"}, "appearances": {"$sum": 1}}},
    ]


def build_user_daily_pipeline(guild_id: str, user_id: str, cutoff: datetime | None) -> list[dict]:
    match: dict[str, Any] = {"guildId": guild_id, "userId": user_id}
    if cutoff is not None:
        match["joinedAt"] = {"$gte": cutoff}
    return [
        {"$match": match},
        {
            "$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$joinedAt"}},
                "ms": {"$sum": "$durationMs"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def build_invites_by_inviter_pipeline(guild_id: str, cutoff: datetime | None, limit: int) -> list[dict]:
    match: dict[str, Any] = {"guildId": guild_id, "inviterUserId": {"$type": "string"}}
    if cutoff is not None:
        match["joinedAt"] = {"$gte": cutoff}
    return [
        {"$match": match},
        {"$sort": {"joinedAt": 1}},
        {
            "$group": {
                "_id": "$inviterUserId",
                "userName": {"$last": "$inviterName"},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]


def build_member_search_pipeline(guild_id: str, query: str, cutoff: datetime, limit: int) -> list[dict]:
    return [
        {
            "$match": {
                "guildId": guild_id,
                "joinedAt": {"$gte": cutoff},
                "userName": {"$regex": re.escape(query), "$options": "i"},
            }
        },
        {"$sort": {"joinedAt": 1}},
        {
            "$group": {
                "_id": "$userId",
                "userName": {"$last": "$userName"},
                "lastSeen": {"$max": "$joinedAt"},
            }
        },
        {"$sort": {"lastSeen": -1}},
        {"$limit": limit},
    ]


# --- executors (db is a pymongo Database or a test fake with the same surface) ---


def leaderboard(db: Any, guild_id: str, period: str, limit: int) -> tuple[list[dict], bool]:
    key = (guild_id, period, limit)
    cached = _leaderboard_cache.get(key)
    if cached is not None:
        return cached, True
    rows = db[COLL_PARTICIPANTS].aggregate(
        build_leaderboard_pipeline(guild_id, period_cutoff(period), limit)
    )
    items = [
        {
            "userId": str(row["_id"]),
            "userName": row.get("userName") or "unknown",
            "totalMs": int(row.get("totalMs") or 0),
            "appearances": int(row.get("appearances") or 0),
        }
        for row in rows
    ]
    _leaderboard_cache.set(key, items)
    return items, False


def active_sessions(db: Any, guild_id: str) -> list[dict]:
    sessions = list(
        db[COLL_SESSIONS].find({"guildId": guild_id, "status": "active"}, sort=[("startedAt", 1)])
    )
    if not sessions:
        return []
    session_ids = [session["_id"] for session in sessions]
    participants = db[COLL_PARTICIPANTS].find(
        {"sessionId": {"$in": session_ids}, "active": True}, sort=[("joinedAt", 1)]
    )
    by_session: dict[str, list[dict]] = {}
    for part in participants:
        by_session.setdefault(str(part["sessionId"]), []).append(
            {
                "userId": str(part["userId"]),
                "userName": part.get("userName") or "unknown",
                "joinedAt": _iso(part.get("joinedAt")),
                "durationMs": int(part.get("durationMs") or 0),
            }
        )
    return [
        {
            "id": str(session["_id"]),
            "channelId": str(session.get("channelId") or ""),
            "startedAt": _iso(session.get("startedAt")),
            "participants": by_session.get(str(session["_id"]), []),
        }
        for session in sessions
    ]


def sessions_history(db: Any, guild_id: str, page: int, size: int) -> dict:
    where = {"guildId": guild_id, "status": "closed"}
    total = db[COLL_SESSIONS].count_documents(where)
    cursor = db[COLL_SESSIONS].find(
        where,
        projection={"_id": 1, "channelId": 1, "startedAt": 1, "endedAt": 1, "endedByUserId": 1, "summaryMessage": 1},
        sort=[("endedAt", -1)],
        skip=(page - 1) * size,
        limit=size,
    )
    items = [
        {
            "id": str(doc["_id"]),
            "channelId": str(doc.get("channelId") or ""),
            "startedAt": _iso(doc.get("startedAt")),
            "endedAt": _iso(doc.get("endedAt")),
            "endedByUserId": doc.get("endedByUserId"),
            "hasSummary": bool(doc.get("summaryMessage")),
        }
        for doc in cursor
    ]
    return {"guildId": guild_id, "status": "closed", "page": page, "size": size, "total": int(total), "items": items}


def session_detail(db: Any, guild_id: str, session_id: str) -> dict | None:
    session = db[COLL_SESSIONS].find_one({"_id": session_id})
    if session is None or str(session.get("guildId")) != guild_id:
        return None
    participants = db[COLL_PARTICIPANTS].find({"sessionId": session_id}, sort=[("joinedAt", 1)])
    return {
        "id": str(session["_id"]),
        "guildId": str(session.get("guildId") or ""),
        "channelId": str(session.get("channelId") or ""),
        "status": str(session.get("status") or ""),
        "startedAt": _iso(session.get("startedAt")),
        "endedAt": _iso(session.get("endedAt")),
        "endedByUserId": session.get("endedByUserId"),
        "summaryMessage": session.get("summaryMessage"),
        "summaryGeneratedAt": _iso(session.get("summaryGeneratedAt")),
        "participants": [
            {
                "userId": str(part.get("userId") or ""),
                "userName": part.get("userName") or "unknown",
                "joinedAt": _iso(part.get("joinedAt")),
                "leftAt": _iso(part.get("leftAt")),
                "durationMs": int(part.get("durationMs") or 0),
                "active": bool(part.get("active")),
            }
            for part in participants
        ],
    }


def _first(doc_cursor: Any) -> dict | None:
    for doc in doc_cursor:
        return doc
    return None


def user_profile(db: Any, guild_id: str, user_id: str, period: str) -> dict | None:
    cutoff = period_cutoff(period)
    name_doc = _first(
        db[COLL_PARTICIPANTS].find(
            {"guildId": guild_id, "userId": user_id}, sort=[("joinedAt", -1)], limit=1
        )
    )
    totals_rows = list(db[COLL_PARTICIPANTS].aggregate(build_user_totals_pipeline(guild_id, user_id, cutoff)))
    totals = totals_rows[0] if totals_rows else {"totalMs": 0, "appearances": 0}
    daily = [
        {"date": row["_id"], "ms": int(row.get("ms") or 0)}
        for row in db[COLL_PARTICIPANTS].aggregate(build_user_daily_pipeline(guild_id, user_id, cutoff))
    ]
    role_state = db[COLL_ROLE_STATE].find_one({"guildId": guild_id, "userId": user_id})
    nicknames = [
        {
            "nickname": doc.get("nickname"),
            "previousNickname": doc.get("previousNickname"),
            "changedAt": _iso(doc.get("changedAt")),
            "source": doc.get("source"),
        }
        for doc in db[COLL_NICKNAME_HISTORY].find(
            {"guildId": guild_id, "userId": user_id}, sort=[("changedAt", -1)], limit=MAX_PROFILE_NICKNAMES
        )
    ]
    join_state = db[COLL_JOIN_STATE].find_one({"guildId": guild_id, "userId": user_id})
    known = name_doc is not None or role_state is not None or join_state is not None
    if not known:
        return None
    return {
        "guildId": guild_id,
        "userId": user_id,
        "userName": (name_doc or {}).get("userName") or "unknown",
        "period": period,
        "totalMs": int(totals.get("totalMs") or 0),
        "appearances": int(totals.get("appearances") or 0),
        "daily": daily,
        "roleIds": [str(r) for r in (role_state or {}).get("roleIds") or []],
        "nicknames": nicknames,
        "join": None
        if join_state is None
        else {
            "inviteCode": join_state.get("inviteCode"),
            "inviteType": join_state.get("inviteType"),
            "inviterUserId": join_state.get("inviterUserId"),
            "inviterName": join_state.get("inviterName"),
            "attributionStatus": join_state.get("attributionStatus"),
            "joinedAt": _iso(join_state.get("joinedAt")),
        },
    }


def invites_overview(db: Any, guild_id: str, period: str) -> dict:
    key = (guild_id, period)
    cached = _invites_cache.get(key)
    if cached is not None:
        return cached
    cutoff = period_cutoff(period)
    attr_match: dict[str, Any] = {"guildId": guild_id}
    if cutoff is not None:
        attr_match["joinedAt"] = {"$gte": cutoff}
    attributions = [
        {
            "userId": str(doc.get("userId") or ""),
            "joinedAt": _iso(doc.get("joinedAt")),
            "inviteCode": doc.get("inviteCode"),
            "inviteType": doc.get("inviteType"),
            "inviterUserId": doc.get("inviterUserId"),
            "inviterName": doc.get("inviterName"),
            "attributionStatus": doc.get("attributionStatus"),
            "source": doc.get("source"),
        }
        for doc in db[COLL_JOIN_ATTRIBUTIONS].find(attr_match, sort=[("joinedAt", -1)], limit=MAX_INVITE_ROWS)
    ]
    catalog = [
        {
            "code": doc.get("code"),
            "channelId": str(doc.get("channelId") or ""),
            "inviteType": doc.get("inviteType"),
            "createdByUserId": doc.get("createdByUserId"),
            "createdByName": doc.get("createdByName"),
            "createdAt": _iso(doc.get("createdAt")),
            "deletedAt": _iso(doc.get("deletedAt")),
            "lastSeenAt": _iso(doc.get("lastSeenAt")),
            "source": doc.get("source"),
        }
        for doc in db[COLL_INVITE_CATALOG].find({"guildId": guild_id}, sort=[("lastSeenAt", -1)], limit=MAX_INVITE_ROWS)
    ]
    by_inviter = [
        {"userId": str(row["_id"]), "userName": row.get("userName") or "unknown", "count": int(row.get("count") or 0)}
        for row in db[COLL_JOIN_ATTRIBUTIONS].aggregate(build_invites_by_inviter_pipeline(guild_id, cutoff, 50))
    ]
    payload = {"guildId": guild_id, "period": period, "generatedAt": _iso(_utc_now()),
               "attributions": attributions, "catalog": catalog, "byInviter": by_inviter}
    _invites_cache.set(key, payload)
    return payload


def settings_document(db: Any, guild_id: str) -> dict | None:
    doc = db[COLL_GUILD_SETTINGS].find_one({"_id": guild_id})
    if doc is None:
        return None
    result = {key: _iso(value) for key, value in doc.items() if key != "_id"}
    result["guildId"] = str(doc["_id"])
    return result


def search_members(db: Any, guild_id: str, query: str, limit: int) -> list[dict]:
    cutoff = _utc_now() - ACTIVE_MEMBER_SEARCH_WINDOW
    rows = db[COLL_PARTICIPANTS].aggregate(build_member_search_pipeline(guild_id, query, cutoff, limit))
    return [{"userId": str(row["_id"]), "userName": row.get("userName") or "unknown"} for row in rows]


WEB_INDEXES: list[tuple[str, list[tuple[str, int]], str]] = [
    (COLL_PARTICIPANTS, [("guildId", 1), ("joinedAt", 1)], "web_guildId_joinedAt"),
    (COLL_SESSIONS, [("guildId", 1), ("status", 1), ("endedAt", -1)], "web_guildId_status_endedAt"),
]


def ensure_web_indexes(db: Any) -> dict:
    created: list[str] = []
    errors: list[str] = []
    for coll_name, keys, index_name in WEB_INDEXES:
        try:
            db[coll_name].create_index(keys, name=index_name)
            created.append(f"{coll_name}.{index_name}")
        except Exception as exc:
            errors.append(f"{coll_name}.{index_name}: {type(exc).__name__}")
    return {"created": created, "errors": errors}
