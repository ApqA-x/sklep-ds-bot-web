from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from . import discord_api, mutations
from .auth import actor_of, perms_for, require_guild_admin
from .config import WebConfig
from .models import (
    ChannelMessageAction,
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


async def _call(request: Request, method: str, path: str, *, json_body: dict | None = None, reason: str | None = None):
    cfg = _cfg(request)
    if not cfg.discord_token:
        raise HTTPException(status_code=503, detail="bot token not configured")
    if not _allow(_guild(request)):
        raise HTTPException(status_code=429, detail="too many bot actions, slow down")
    return await discord_api.bot_request(cfg, method, path, json_body=json_body, reason=reason)


def _finish(request: Request, action: str, arguments: dict[str, Any], status: int, payload: Any) -> dict:
    db = request.app.state.db
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    guild = _guild(request)
    ok = 200 <= status < 300
    detail = payload if isinstance(payload, dict) else {"raw": str(payload)[:300]}
    mutations.record_audit(
        db,
        guild_id=guild,
        actor=actor_of(request),
        action=f"bot.{action}",
        after={**arguments, "discordStatus": status, "detail": detail if not ok else None},
        ok=ok,
        origin="discord",
    )
    if not ok:
        raise HTTPException(status_code=502, detail={"discordStatus": status, "body": detail})
    return {"ok": True, "discordStatus": status}


@router.post("/member/{userId}/roles")
async def member_role(request: Request, guildId: str, userId: str, body: MemberRoleAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    method = "PUT" if body.action == "grant" else "DELETE"
    status, payload = await _call(request, method, f"/guilds/{guildId}/members/{userId}/roles/{body.roleId}")
    return _finish(request, "role", {"userId": userId, "roleId": body.roleId, "action": body.action}, status, payload)


@router.post("/member/{userId}/timeout")
async def member_timeout(request: Request, guildId: str, userId: str, body: TimeoutMemberAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    if body.mute:
        until = (datetime.now(timezone.utc) + timedelta(seconds=body.seconds)).isoformat()
        json_body: dict[str, Any] | None = {"communication_disabled_until": until}
    else:
        json_body = {"communication_disabled_until": None}
    status, payload = await _call(
        request, "PATCH", f"/guilds/{guildId}/members/{userId}", json_body=json_body, reason="voice_tracker web"
    )
    return _finish(request, "timeout", {"userId": userId, "mute": body.mute, "seconds": body.seconds if body.mute else None}, status, payload)


@router.post("/member/{userId}/move")
async def member_move(request: Request, guildId: str, userId: str, body: MoveMemberAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    status, payload = await _call(
        request,
        "PATCH",
        f"/guilds/{guildId}/members/{userId}",
        json_body={"channel_id": body.channelId},
    )
    return _finish(request, "move", {"userId": userId, "channelId": body.channelId}, status, payload)


@router.post("/member/{userId}/kick")
async def member_kick(request: Request, guildId: str, userId: str, body: KickMemberAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(userId, "userId")
    perms = await perms_for(request, guildId)
    if not perms & (1 << 3 | KICK_MEMBERS):
        raise HTTPException(status_code=403, detail="kick requires administrator or kick members")
    status, payload = await _call(
        request, "DELETE", f"/guilds/{guildId}/members/{userId}", reason=body.reason
    )
    return _finish(request, "kick", {"userId": userId, "reason": body.reason}, status, payload)


@router.post("/channel/{channelId}/message")
async def channel_message(request: Request, guildId: str, channelId: str, body: ChannelMessageAction) -> dict:
    _snowflake(guildId, "guildId")
    _snowflake(channelId, "channelId")
    status, payload = await _call(
        request, "POST", f"/channels/{channelId}/messages", json_body={"content": body.content}
    )
    return _finish(request, "message", {"channelId": channelId, "length": len(body.content)}, status, payload)


@router.post("/invite")
async def create_invite(request: Request, guildId: str, body: InviteCreateAction) -> dict:
    _snowflake(guildId, "guildId")
    status, payload = await _call(
        request,
        "POST",
        f"/channels/{body.channelId}/invites",
        json_body={"max_age": body.maxAge, "max_uses": body.maxUses},
    )
    return _finish(request, "invite.create", {"channelId": body.channelId}, status, payload)


@router.delete("/invite/{code}")
async def delete_invite(request: Request, guildId: str, code: str) -> dict:
    _snowflake(guildId, "guildId")
    if not INVITE_CODE_RE.match(code):
        raise HTTPException(status_code=422, detail="invalid invite code")
    status, payload = await _call(request, "DELETE", f"/invites/{code}")
    return _finish(request, "invite.delete", {"code": code}, status, payload)
