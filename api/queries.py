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
COLL_NICKNAME_STATE = "member_nickname_state"
COLL_INVITE_CATALOG = "invite_catalog"
COLL_CHAT = "chat_messages"

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
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Mongo хранит UTC; pymongo по умолчанию отдаёт naive-datetime
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return value


class InvalidDate(ValueError):
    """строка не парсится как дата/дата-время"""


def parse_date_bound(value: str, *, end: bool) -> datetime | None:
    """Граница периода: 'YYYY-MM-DD' (для end — включая весь день) или полный ISO."""
    value = value.strip()
    if not value:
        return None
    try:
        if len(value) == 10:
            day = datetime.strptime(value, "%Y-%m-%d").date()
            edge = datetime.max.time() if end else datetime.min.time()
            return datetime.combine(day, edge, tzinfo=timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidDate(value) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# --- pipeline builders (pure, unit-tested without a database) ---


def build_leaderboard_pipeline(guild_id: str, cutoff: datetime | None, limit: int, skip: int = 0) -> list[dict]:
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
        {
            "$facet": {
                "page": [{"$skip": skip}, {"$limit": limit}],
                "total": [{"$count": "n"}],
            }
        },
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


def build_member_search_pipeline(guild_id: str, query: str, limit: int) -> list[dict]:
    return [
        {
            "$match": {
                "guildId": guild_id,
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


def leaderboard(db: Any, guild_id: str, period: str, limit: int, page: int = 1) -> tuple[list[dict], int, bool]:
    """Страница лидерборда: (items, total_users, cached)."""
    key = (guild_id, period, limit, page)
    cached = _leaderboard_cache.get(key)
    if cached is not None:
        return cached[0], cached[1], True
    rows = db[COLL_PARTICIPANTS].aggregate(
        build_leaderboard_pipeline(guild_id, period_cutoff(period), limit, (page - 1) * limit)
    )
    facet = next(iter(rows), None) or {}
    items = [
        {
            "userId": str(row["_id"]),
            "userName": row.get("userName") or "unknown",
            "totalMs": int(row.get("totalMs") or 0),
            "appearances": int(row.get("appearances") or 0),
        }
        for row in facet.get("page", [])
    ]
    total_rows = facet.get("total") or []
    total = int(total_rows[0]["n"]) if total_rows else 0
    _leaderboard_cache.set(key, (items, total))
    return items, total, False


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
        "updatedAt": _iso(session.get("updatedAt")),
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
    msg_where: dict[str, Any] = {"guildId": guild_id, "authorUserId": user_id}
    if cutoff is not None:
        msg_where["sentAt"] = {"$gte": cutoff}
    message_count = int(db[COLL_CHAT].count_documents(msg_where))
    invited_count = int(
        db[COLL_JOIN_ATTRIBUTIONS].count_documents({"guildId": guild_id, "inviterUserId": user_id})
    )
    return {
        "guildId": guild_id,
        "userId": user_id,
        "userName": (name_doc or {}).get("userName") or "unknown",
        "period": period,
        "totalMs": int(totals.get("totalMs") or 0),
        "appearances": int(totals.get("appearances") or 0),
        "messageCount": message_count,
        "invitedCount": invited_count,
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


def stalker_subscriptions(db: Any, guild_id: str) -> list[dict]:
    docs = db["stalker_subscriptions"].find({"guildId": guild_id}, sort=[("createdAt", -1)])
    return [
        {
            "id": str(doc.get("_id") or ""),
            "watcherUserId": str(doc.get("watcherUserId") or ""),
            "targetUserId": str(doc.get("targetUserId") or ""),
            "createdAt": _iso(doc.get("createdAt")),
        }
        for doc in docs
    ]


def search_members(db: Any, guild_id: str, query: str, limit: int) -> list[dict]:
    rows = db[COLL_PARTICIPANTS].aggregate(build_member_search_pipeline(guild_id, query, limit))
    return [{"userId": str(row["_id"]), "userName": row.get("userName") or "unknown"} for row in rows]


_names_cache = TTLCache(ttl_seconds=300.0)


def known_user_names(db: Any, guild_id: str) -> dict[str, str]:
    """userId -> best known display name (current nickname, else last seen username)."""
    cached = _names_cache.get(guild_id)
    if cached is not None:
        return cached
    names: dict[str, str] = {}
    cutoff = _utc_now() - ACTIVE_MEMBER_SEARCH_WINDOW
    pipeline = [
        {"$match": {"guildId": guild_id, "joinedAt": {"$gte": cutoff}}},
        {"$sort": {"joinedAt": 1}},
        {"$group": {"_id": "$userId", "userName": {"$last": "$userName"}}},
    ]
    for row in db[COLL_PARTICIPANTS].aggregate(pipeline):
        name = str(row.get("userName") or "")
        if name:
            names[str(row["_id"])] = name
    for doc in db[COLL_NICKNAME_STATE].find({"guildId": guild_id}, projection={"userId": 1, "nickname": 1}):
        nickname = str(doc.get("nickname") or "")
        if nickname:
            names[str(doc.get("userId"))] = nickname
    _names_cache.set(guild_id, names)
    return names


def chat_channels(db: Any, guild_id: str) -> list[dict[str, Any]]:
    """Text channels of a guild that have stored messages, most recent activity first."""
    pipeline = [
        {"$match": {"guildId": guild_id}},
        {"$sort": {"sentAt": -1}},
        {"$group": {"_id": "$channelId", "count": {"$sum": 1}, "lastAt": {"$first": "$sentAt"}}},
        {"$sort": {"lastAt": -1}},
        {"$limit": 200},
    ]
    rows = list(db[COLL_CHAT].aggregate(pipeline))
    return [
        {"channelId": str(row["_id"]), "count": int(row.get("count") or 0), "lastAt": _iso(row.get("lastAt"))}
        for row in rows
        if row.get("_id")
    ]


def chat_messages(
    db: Any,
    guild_id: str,
    channel_id: str | None,
    before: datetime | None,
    limit: int,
    *,
    after: datetime | None = None,
    user_id: str | None = None,
    msg_type: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    sort: str = "desc",
) -> dict[str, Any]:
    where: dict[str, Any] = {"guildId": guild_id}
    if channel_id:
        where["channelId"] = channel_id
    if user_id:
        where["authorUserId"] = user_id
    if before is not None or after is not None or date_from is not None or date_to is not None:
        bounds: dict[str, Any] = {}
        if before is not None:
            bounds["$lt"] = before
        if after is not None:
            bounds["$gt"] = after
        if date_from is not None:
            bounds["$gte"] = date_from
        if date_to is not None:
            bounds["$lte"] = date_to
        where["sentAt"] = bounds
    if msg_type == "image":
        where["attachments.kind"] = "image"
    elif msg_type == "file":
        where["attachments.0"] = {"$exists": True}
    elif msg_type == "link":
        where["content"] = {"$regex": r"https?://"}
    elif msg_type == "text":
        where["$and"] = [
            {"attachments.0": {"$exists": False}},
            {"content": {"$not": {"$regex": r"https?://"}}},
        ]
    direction = 1 if sort == "asc" else -1
    docs = list(db[COLL_CHAT].find(where, sort=[("sentAt", direction)], limit=limit + 1))
    has_more = len(docs) > limit
    page = docs[:limit]
    items = [
        {
            "messageId": str(doc.get("messageId") or doc.get("_id")),
            "channelId": str(doc.get("channelId") or ""),
            "authorUserId": str(doc.get("authorUserId") or ""),
            "authorName": doc.get("authorName"),
            "content": doc.get("content") or "",
            "sentAt": _iso(doc.get("sentAt")),
            "editedAt": _iso(doc["editedAt"]) if doc.get("editedAt") else None,
            "deletedAt": _iso(doc["deletedAt"]) if doc.get("deletedAt") else None,
            "attachments": [
                {
                    "id": str(a.get("id") or ""),
                    "filename": a.get("filename") or "файл",
                    "contentType": a.get("contentType") or "",
                    "size": int(a.get("size") or 0),
                    "kind": a.get("kind") or "file",
                    "path": a.get("path") or "",
                    "stored": bool(a.get("stored")),
                    "url": a.get("url") or "",
                }
                for a in (doc.get("attachments") or [])
                if isinstance(a, dict)
            ],
        }
        for doc in page
    ]
    items.sort(key=lambda item: item["sentAt"])  # ленту всегда показываем хронологически
    return {
        "guildId": guild_id,
        "channelId": channel_id or "",
        "items": items,
        "hasMore": has_more,
        "sort": sort,
        "nextBefore": items[0]["sentAt"] if items else None,
        "nextAfter": items[-1]["sentAt"] if items else None,
    }


WEB_INDEXES: list[tuple[str, list[tuple[str, int]], str]] = [
    (COLL_PARTICIPANTS, [("guildId", 1), ("joinedAt", 1)], "web_guildId_joinedAt"),
    (COLL_SESSIONS, [("guildId", 1), ("status", 1), ("endedAt", -1)], "web_guildId_status_endedAt"),
    ("web_audit_logs", [("guildId", 1), ("at", -1)], "web_audit_guildId_at"),
    # имя совпадает с индексом writer'а бота (dsbot ensure_indexes) — иначе Mongo считает это
    # «тот же ключ под другим именем» (code 85) и пересоздание конфликует
    (COLL_CHAT, [("guildId", 1), ("channelId", 1), ("sentAt", -1)], "chat_guildId_channelId_sentAt"),
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
