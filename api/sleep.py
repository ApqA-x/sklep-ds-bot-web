"""Guild-admin web controls for the shared voice sleep timer document."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import discord_api, operations
from .auth import actor_of, require_guild_admin
from .sleep_timers import SleepTimerBusy, SleepTimerConflict, SleepTimerStore


router = APIRouter(
    prefix="/api/guild/{guildId}/sleep", tags=["sleep-timers"],
    dependencies=[Depends(require_guild_admin)],
)
SNOWFLAKE = re.compile(r"^[0-9]{5,20}$")


class SetSleepTimer(BaseModel):
    hours: int = Field(strict=True, ge=1, le=24)


def _ids(guild_id: str, user_id: str) -> None:
    if not SNOWFLAKE.fullmatch(guild_id) or not SNOWFLAKE.fullmatch(user_id):
        raise HTTPException(status_code=422, detail="invalid guild or user ID")


def _store(request: Request) -> SleepTimerStore:
    return SleepTimerStore(request.app.state.db["voice_sleep_timers"])


def _request_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "").strip()
    if not key:
        raise HTTPException(status_code=422, detail="idempotency-key is required")
    return operations.validate_key(key, "idempotency-key")


async def _require_current_member(request: Request, guild_id: str, user_id: str) -> None:
    cfg = request.app.state.config
    if not cfg.discord_token:
        raise HTTPException(status_code=503, detail="member verification unavailable")
    status, payload = await discord_api.bot_request(
        cfg, "GET", f"/guilds/{guild_id}/members/{user_id}",
    )
    if status == 404:
        raise HTTPException(status_code=403, detail="target is not a member of this guild")
    if status != 200 or not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="member verification failed")
    user = payload.get("user")
    if not isinstance(user, dict) or str(user.get("id") or "") != user_id:
        raise HTTPException(status_code=502, detail="member identity could not be verified")


def _public_timer(timer: dict[str, Any] | None) -> dict[str, Any]:
    if timer is None:
        return {"status": "none", "dueAt": None, "hours": None, "resultAt": None, "reason": None}
    return {
        "status": timer.get("status", "unknown"),
        "dueAt": timer.get("dueAt"),
        "hours": timer.get("hours"),
        "resultAt": timer.get("resultAt"),
        "reason": timer.get("reason"),
    }


@router.get("/member/{userId}")
async def get_sleep_timer(request: Request, guildId: str, userId: str) -> dict[str, Any]:
    _ids(guildId, userId)
    timer = await asyncio.to_thread(_store(request).get, guildId, userId)
    return {"guildId": guildId, "userId": userId, **_public_timer(timer)}


@router.post("/member/{userId}")
async def set_sleep_timer(
    request: Request, guildId: str, userId: str, body: SetSleepTimer,
) -> dict[str, Any]:
    _ids(guildId, userId)
    request_id = _request_key(request)
    actor = actor_of(request)["userId"]
    await _require_current_member(request, guildId, userId)
    try:
        mutation = await asyncio.to_thread(
            _store(request).set, guildId, userId, body.hours,
            actor_user_id=actor, source="web", request_id=request_id,
        )
    except (SleepTimerBusy, SleepTimerConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "guildId": guildId, "userId": userId, "replayed": mutation.replayed,
        **_public_timer(mutation.timer),
    }


@router.delete("/member/{userId}")
async def cancel_sleep_timer(request: Request, guildId: str, userId: str) -> dict[str, Any]:
    _ids(guildId, userId)
    request_id = _request_key(request)
    actor = actor_of(request)["userId"]
    try:
        mutation = await asyncio.to_thread(
            _store(request).cancel, guildId, userId,
            actor_user_id=actor, source="web", request_id=request_id,
        )
    except (SleepTimerBusy, SleepTimerConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "guildId": guildId, "userId": userId, "replayed": mutation.replayed,
        "hadActiveTimer": mutation.outcome["hadActiveTimer"],
        **_public_timer(mutation.timer),
    }
