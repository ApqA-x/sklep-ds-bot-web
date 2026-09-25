from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import discord_api, queries
from .auth import require_guild_read

router = APIRouter(
    prefix="/api/guild/{guildId}",
    tags=["read"],
    dependencies=[Depends(require_guild_read)],
)

SNOWFLAKE_RE = re.compile(r"^\d{5,25}$")


def _db(request: Request) -> Any:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return db


def _snowflake(value: str, field: str) -> str:
    if not SNOWFLAKE_RE.match(value):
        raise HTTPException(status_code=422, detail=f"invalid {field}")
    return value


def _session_id(value: str) -> str:
    try:
        uuid.UUID(value)
    except ValueError:
        raise HTTPException(status_code=422, detail="invalid sessionId") from None
    return value


def _period(value: str) -> str:
    if value not in queries.PERIODS:
        raise HTTPException(status_code=422, detail="period must be one of 7d|30d|all")
    return value


@router.get("/leaderboard")
def get_leaderboard(
    request: Request,
    guildId: str,
    period: str = Query("30d"),
    limit: int = Query(50, ge=1, le=100),
    page: int = Query(1, ge=1, le=200),
    q: str = Query("", max_length=64),
) -> dict:
    guild = _snowflake(guildId, "guildId")
    _period(period)
    items, total, cached = queries.leaderboard(_db(request), guild, period, limit, page, q.strip())
    return {
        "guildId": guild,
        "period": period,
        "limit": limit,
        "page": page,
        "q": q.strip(),
        "total": total,
        "cached": cached,
        "items": items,
    }


@router.get("/chat-leaderboard")
def get_chat_leaderboard(
    request: Request,
    guildId: str,
    period: str = Query("30d"),
    limit: int = Query(50, ge=1, le=100),
    page: int = Query(1, ge=1, le=200),
    q: str = Query("", max_length=64),
) -> dict:
    guild = _snowflake(guildId, "guildId")
    _period(period)
    items, total, cached = queries.chat_leaderboard(_db(request), guild, period, limit, page, q.strip())
    return {
        "guildId": guild,
        "period": period,
        "limit": limit,
        "page": page,
        "q": q.strip(),
        "total": total,
        "cached": cached,
        "items": items,
    }


@router.get("/sessions/active")
def get_active_sessions(request: Request, guildId: str) -> dict:
    guild = _snowflake(guildId, "guildId")
    return {"guildId": guild, "items": queries.active_sessions(_db(request), guild)}


@router.get("/sessions")
def get_sessions_history(
    request: Request,
    guildId: str,
    status: str = Query("closed"),
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=100),
) -> dict:
    guild = _snowflake(guildId, "guildId")
    if status == "active":
        return {"guildId": guild, "status": "active", "items": queries.active_sessions(_db(request), guild)}
    if status != "closed":
        raise HTTPException(status_code=422, detail="status must be closed or active")
    return queries.sessions_history(_db(request), guild, page, size)


@router.get("/sessions/{sessionId}")
def get_session_detail(request: Request, guildId: str, sessionId: str) -> dict:
    guild = _snowflake(guildId, "guildId")
    _session_id(sessionId)
    detail = queries.session_detail(_db(request), guild, sessionId)
    if detail is None:
        raise HTTPException(status_code=404, detail="session not found")
    return detail


@router.get("/users/{userId}")
def get_user_profile(
    request: Request,
    guildId: str,
    userId: str,
    period: str = Query("30d"),
) -> dict:
    guild = _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    _period(period)
    profile = queries.user_profile(_db(request), guild, userId, period)
    if profile is None:
        raise HTTPException(status_code=404, detail="user not seen by the bot on this guild")
    return profile


@router.get("/invites")
def get_invites(request: Request, guildId: str, period: str = Query("30d")) -> dict:
    guild = _snowflake(guildId, "guildId")
    _period(period)
    return queries.invites_overview(_db(request), guild, period)


@router.get("/settings")
def get_settings(request: Request, guildId: str) -> dict:
    guild = _snowflake(guildId, "guildId")
    doc = queries.settings_document(_db(request), guild)
    if doc is None:
        raise HTTPException(status_code=404, detail="no settings for this guild yet")
    return doc


@router.get("/members")
def search_members(
    request: Request,
    guildId: str,
    q: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(10, ge=1, le=25),
) -> dict:
    guild = _snowflake(guildId, "guildId")
    return {"guildId": guild, "q": q, "items": queries.search_members(_db(request), guild, q, limit)}


@router.get("/users/{userId}/member")
async def member_state(request: Request, guildId: str, userId: str) -> dict:
    """Живое состояние участника из Discord: роли, тайм-аут, голосовой канал."""
    guild = _snowflake(guildId, "guildId")
    user = _snowflake(userId, "userId")
    from . import discord_api

    cfg = request.app.state.config
    try:
        member = await discord_api.get_member(cfg, guild, user)
    except discord_api.DiscordError:
        member = None
    if member is None:
        return {"guildId": guild, "userId": user, "source": "unavailable",
                "roleIds": None, "timeoutUntil": None, "voiceChannelId": None}
    voice = member.get("voice") or {}
    return {
        "guildId": guild,
        "userId": user,
        "source": "discord",
        "roleIds": [str(r) for r in member.get("roles") or []],
        "timeoutUntil": member.get("communication_disabled_until"),
        "voiceChannelId": (str(voice["channel_id"]) if voice.get("channel_id") else None),
    }


@router.get("/users/{userId}/card")
async def user_card(request: Request, guildId: str, userId: str) -> dict:
    """Карточка для шапки профиля: ник/юзернейм, аватар, банер и история аватарок.

    Прошлые аватарки Discord через бот-API не отдаёт — копит свою историю:
    каждый хеш, увиденный здесь, попадает в user_avatar_history.
    """
    guild = _snowflake(guildId, "guildId")
    user = _snowflake(userId, "userId")
    cfg = request.app.state.config
    db = _db(request)
    now = datetime.now(timezone.utc)
    result: dict[str, Any] = {"guildId": guild, "userId": user, "source": "unavailable",
                              "username": None, "globalName": None, "nick": None,
                              "joinedAt": None, "avatarUrl": discord_api.default_avatar_url(user),
                              "bannerUrl": None, "accentColor": None}
    if cfg.discord_token:
        try:
            member = await discord_api.get_member(cfg, guild, user)
        except discord_api.DiscordError:
            member = None
        if member is not None:
            account = member.get("user") or {}
            guild_hash = member.get("avatar")
            global_hash = account.get("avatar")
            for kind, h in (("guild", guild_hash), ("global", global_hash)):
                if h:
                    db[queries.COLL_AVATAR_HISTORY].update_one(
                        {"guildId": guild, "userId": user, "hash": h},
                        {"$setOnInsert": {"firstSeen": now}, "$set": {"lastSeen": now, "kind": kind}},
                        upsert=True,
                    )
            result = {
                **result,
                "source": "discord",
                "username": account.get("username"),
                "globalName": account.get("global_name"),
                "nick": member.get("nick"),
                "joinedAt": member.get("joined_at"),
                "avatarUrl": discord_api.avatar_url(
                    guild, user, guild_hash or global_hash, "guild" if guild_hash else "global"
                ),
                "bannerUrl": discord_api.banner_url(user, account.get("banner")),
                "accentColor": account.get("accent_color"),
            }
    result["avatars"] = [
        {
            "hash": str(doc.get("hash") or ""),
            "kind": str(doc.get("kind") or "global"),
            "url": discord_api.avatar_url(guild, user, str(doc.get("hash") or ""), str(doc.get("kind") or "global")),
            "firstSeen": queries._iso(doc.get("firstSeen")),
            "lastSeen": queries._iso(doc.get("lastSeen")),
        }
        for doc in db[queries.COLL_AVATAR_HISTORY].find({"guildId": guild, "userId": user}, sort=[("firstSeen", -1)])
    ]
    return result


@router.get("/audit/discord")
def audit_discord(
    request: Request,
    guildId: str,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    actionType: int = Query(0, ge=0, description="0 = все типы"),
    actor: str = Query(""),
    target: str = Query(""),
    dateFrom: str = Query(""),
    dateTo: str = Query(""),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict:
    """Журнал аудита Discord из локальной копии (discord_audit_logs) с фильтрами."""
    from . import audit_sync

    guild = _snowflake(guildId, "guildId")
    db = _db(request)
    where: dict[str, Any] = {"guildId": guild}
    if actionType:
        where["actionType"] = actionType
    if actor:
        where["actorUserId"] = _snowflake(actor, "actor")
    if target:
        who = _snowflake(target, "target")
        where["$or"] = [{"targetUserId": who}, {"targetId": who}]
    bounds: dict[str, Any] = {}
    for raw, op in ((dateFrom, "$gte"), (dateTo, "$lte")):
        if raw:
            try:
                bounds[op] = queries.parse_date_bound(raw, end=op == "$lte")
            except queries.InvalidDate:
                raise HTTPException(status_code=422, detail="invalid date") from None
    if bounds:
        where["at"] = bounds
    direction = 1 if sort == "asc" else -1
    total = int(db[audit_sync.COLL_DISCORD_AUDIT].count_documents(where))
    docs = db[audit_sync.COLL_DISCORD_AUDIT].find(
        where, sort=[("at", direction)], skip=(page - 1) * size, limit=size
    )
    items = [
        {
            "id": doc.get("entryId"),
            "at": queries._iso(doc.get("at")),
            "actionType": doc.get("actionType"),
            "action": doc.get("action"),
            "actorUserId": doc.get("actorUserId"),
            "actorName": doc.get("actorName"),
            "targetUserId": doc.get("targetUserId"),
            "targetUserName": doc.get("targetUserName"),
            "targetId": doc.get("targetId"),
            "channelId": doc.get("channelId"),
            "count": doc.get("count"),
            "deleteMessageDays": doc.get("deleteMessageDays"),
            "reason": doc.get("reason"),
            "changes": doc.get("changes") or [],
            "options": doc.get("options") or {},
        }
        for doc in docs
    ]
    return {"guildId": guild, "source": "discord", "page": page, "size": size, "total": total, "items": items}


@router.get("/audit/discord/actions")
def audit_discord_actions(request: Request, guildId: str) -> dict:
    """Типы действий с количеством — наполняет фильтр журнала Discord."""
    from . import audit_sync

    guild = _snowflake(guildId, "guildId")
    rows = _db(request)[audit_sync.COLL_DISCORD_AUDIT].aggregate(
        [
            {"$match": {"guildId": guild}},
            {"$group": {"_id": {"type": "$actionType", "action": "$action"}, "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ]
    )
    return {
        "guildId": guild,
        "items": [
            {"actionType": int(row["_id"]["type"]), "action": str(row["_id"]["action"]), "count": int(row.get("n") or 0)}
            for row in rows
            if isinstance(row.get("_id"), dict) and row["_id"].get("type")
        ],
    }


@router.get("/audit/discord/status")
def audit_discord_status(request: Request, guildId: str) -> dict:
    """T11: честная сводка синхронизации — полнота (backfillComplete), отставание,
    403-доступ, курсоры. UI по нему различает «загружено» / «дозагрузка истории» /
    «нет доступа» / «отстаёт» и не обещает недоступную Discord-историю (H09)."""
    from . import audit_sync

    guild = _snowflake(guildId, "guildId")
    return {"guildId": guild, **audit_sync.status_snapshot(_db(request), guild)}


@router.post("/audit/discord/sync")
async def audit_discord_sync(request: Request, guildId: str) -> dict:
    """Обновить локальную копию журнала Discord прямо сейчас. Read-only обращение к
    Discord + идемпотентный upsert — журнал операций (operations) не требуется.
    Координируется с фоновым циклом через lease (H08): занят → skipped без гонки."""
    from . import audit_sync

    guild = _snowflake(guildId, "guildId")
    cfg = request.app.state.config
    if not cfg.discord_token:
        raise HTTPException(status_code=503, detail="bot token not configured")
    result = await audit_sync.sync_guild(_db(request), cfg, guild,
                                         scan_pages=audit_sync.MANUAL_SCAN_PAGES)
    if not result["ok"] and result["discordStatus"] == 403:
        result["error"] = "у роли бота нет разрешения «Просматривать журнал аудита»"
    return {"guildId": guild, **result}


@router.get("/chat/channels")
def chat_channels(request: Request, guildId: str) -> dict:
    guild = _snowflake(guildId, "guildId")
    return {"guildId": guild, "items": queries.chat_channels(_db(request), guild)}


@router.get("/chat")
def chat_messages(
    request: Request,
    guildId: str,
    channelId: list[str] = Query(default=[]),
    before: str = Query(""),
    after: str = Query(""),
    cursor: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
    userId: str = Query(""),
    type: str = Query("", pattern="^(||text|link|file|image)$"),
    dateFrom: str = Query(""),
    dateTo: str = Query(""),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict:
    guild = _snowflake(guildId, "guildId")
    # UI отдаёт мультивыбор: канал приходит повторением channelId, пустой выбор = все каналы
    ids = list(dict.fromkeys(_snowflake(c, "channelId") for c in channelId if c))
    if len(ids) > 100:
        raise HTTPException(status_code=422, detail="too many channels")
    if userId:
        _snowflake(userId, "userId")

    def _iso_dt(value: str, field: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{field} must be an ISO datetime") from None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def _bound(value: str, *, end: bool) -> datetime | None:
        try:
            return queries.parse_date_bound(value, end=end)
        except queries.InvalidDate:
            raise HTTPException(status_code=422, detail="invalid date") from None

    try:
        return queries.chat_messages(
            _db(request),
            guild,
            ids or None,
            _iso_dt(before, "before"),
            limit,
            after=_iso_dt(after, "after"),
            cursor=cursor,
            user_id=userId or None,
            msg_type=type or None,
            date_from=_bound(dateFrom, end=False),
            date_to=_bound(dateTo, end=True),
            sort=sort,
        )
    except queries.ChatCursorError as exc:
        # повреждённый/чужой/смешанный курсор — явная 400 (H04), UI сбрасывает на первую страницу
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/picker")
async def guild_picker(request: Request, guildId: str) -> dict:
    guild = _snowflake(guildId, "guildId")
    from . import discord_api

    cfg = request.app.state.config
    try:
        role_rows = await discord_api.guild_roles_raw(cfg, guild)
        channel_rows = await discord_api.guild_channels_raw(cfg, guild)
    except discord_api.DiscordError:
        # без валидного бот-токена (или бот не в этой гильдии) списки пустые — UI покажет ручной ввод id
        role_rows, channel_rows = [], []
    try:
        top_position = await discord_api.bot_top_role_position(cfg, guild)
    except Exception:  # noqa: BLE001 - неизвестная иерархия бота не должна скрывать роли
        top_position = None
    roles = discord_api.build_role_options(role_rows, guild, top_position)
    channels = discord_api.build_voice_channels(channel_rows)
    text_channels = discord_api.build_text_channels(channel_rows)
    return {"guildId": guild, "roles": roles, "voiceChannels": channels, "textChannels": text_channels}


@router.get("/names")
async def guild_names(request: Request, guildId: str) -> dict:
    guild = _snowflake(guildId, "guildId")
    from . import discord_api

    cfg = request.app.state.config
    channels: dict[str, str] = {}
    roles: dict[str, str] = {}
    guild_name = None
    try:
        channels = await discord_api.guild_channel_names(cfg, guild)
        roles = await discord_api.guild_role_names(cfg, guild)
        for entry in await discord_api.bot_guilds(cfg):
            if str(entry.get("id")) == guild:
                guild_name = str(entry.get("name") or "")
                break
    except discord_api.DiscordError:
        pass  # without a bot token names fall back to ids in the UI
    db = getattr(request.app.state, "db", None)
    users = queries.known_user_names(db, guild) if db is not None else {}
    user_colors: dict[str, str] = {}
    if db is not None:
        try:
            role_rows = await discord_api.guild_roles_raw(cfg, guild)
        except discord_api.DiscordError:
            role_rows = []
        role_meta = {
            str(row.get("id")): (int(row.get("color") or 0), int(row.get("position") or 0))
            for row in role_rows
            if row.get("id")
        }
        user_colors = queries.known_user_colors(db, guild, role_meta)
    return {
        "guildId": guild,
        "guildName": guild_name,
        "channels": channels,
        "roles": roles,
        "users": users,
        "userColors": user_colors,
    }
