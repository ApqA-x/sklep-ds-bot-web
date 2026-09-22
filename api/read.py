from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import queries
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
) -> dict:
    guild = _snowflake(guildId, "guildId")
    _period(period)
    items, cached = queries.leaderboard(_db(request), guild, period, limit)
    return {"guildId": guild, "period": period, "limit": limit, "cached": cached, "items": items}


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


@router.get("/chat/channels")
def chat_channels(request: Request, guildId: str) -> dict:
    guild = _snowflake(guildId, "guildId")
    return {"guildId": guild, "items": queries.chat_channels(_db(request), guild)}


@router.get("/chat")
def chat_messages(
    request: Request,
    guildId: str,
    channelId: str = Query(...),
    before: str = Query(""),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    guild = _snowflake(guildId, "guildId")
    _snowflake(channelId, "channelId")
    before_dt = None
    if before:
        try:
            parsed = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="before must be an ISO datetime") from None
        before_dt = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return queries.chat_messages(_db(request), guild, channelId, before_dt, limit)


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
    return {"guildId": guild, "guildName": guild_name, "channels": channels, "roles": roles, "users": users}
