from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import mutations, queries
from .auth import actor_of, require_guild_admin
from .bot import _check_channel, _check_role
from .models import ChatPresetAction, GuildSettingsPatch, ListMemberAction, StalkerAction

SNOWFLAKE_RE = re.compile(r"^\d{5,25}$")


def _date_bound(value: str, *, end: bool) -> datetime | None:
    try:
        return queries.parse_date_bound(value, end=end)
    except queries.InvalidDate:
        raise HTTPException(status_code=422, detail="invalid date") from None

router = APIRouter(
    prefix="/api/guild/{guildId}",
    tags=["write"],
    dependencies=[Depends(require_guild_admin)],
)


def _db(request: Request) -> Any:
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return db


def _guild(guild_id: str) -> str:
    if not SNOWFLAKE_RE.match(guild_id):
        raise HTTPException(status_code=422, detail="invalid guildId")
    return guild_id


@router.patch("/settings")
async def patch_settings(request: Request, guildId: str, body: GuildSettingsPatch) -> dict:
    guild = _guild(guildId)
    if not body.mongo_set():
        raise HTTPException(status_code=422, detail="empty patch")
    # G04: чужие resource ID отвергаются сервером до записи — не полагаться на UI picker.
    for field in (
        "summaryChannelId",
        "fallbackSummaryChannelId",
        "activityChannelId",
    ):
        channel_id = getattr(body, field, None)
        if channel_id:
            await _check_channel(request, guild, channel_id)
    for channel_id in (body.trackedChannelIds or []):
        await _check_channel(request, guild, channel_id)
    for channel_id in (body.activityCategoryChannelIds or {}).values():
        await _check_channel(request, guild, channel_id)
    if body.autoRoleId:
        await _check_role(request, guild, body.autoRoleId)
    status, doc = mutations.patch_guild_settings(_db(request), guild, body, actor_of(request))
    if status == "conflict":
        # T06.8: 409 с безопасными данными новой версии; клиент сам решает,
        # какие поля применить к свежей revision (автоматического overwrite нет)
        raise HTTPException(
            status_code=409,
            detail={"error": "revision_conflict", "current": doc or {}},
        )
    return doc or {}


@router.post("/trusted")
def mutate_trusted(request: Request, guildId: str, body: ListMemberAction) -> dict:
    guild = _guild(guildId)
    doc = mutations.mutate_id_list(
        _db(request), guild, "trustedUserIds", body, actor_of(request)
    )
    return doc or {}


@router.post("/autoUnmute")
def mutate_auto_unmute(request: Request, guildId: str, body: ListMemberAction) -> dict:
    guild = _guild(guildId)
    doc = mutations.mutate_id_list(
        _db(request), guild, "autoUnmuteUserIds", body, actor_of(request)
    )
    return doc or {}


@router.post("/stalker")
def mutate_stalker(request: Request, guildId: str, body: StalkerAction) -> dict:
    guild = _guild(guildId)
    return mutations.mutate_stalker(_db(request), guild, body, actor_of(request))


@router.get("/chat-presets")
def list_chat_presets(request: Request, guildId: str) -> dict:
    guild = _guild(guildId)
    return {"guildId": guild, "items": queries.chat_presets(_db(request), guild)}


@router.post("/chat-presets")
def mutate_chat_preset(request: Request, guildId: str, body: ChatPresetAction) -> dict:
    guild = _guild(guildId)
    return mutations.mutate_chat_preset(_db(request), guild, body, actor_of(request))


@router.get("/stalker")
def list_stalker(request: Request, guildId: str) -> dict:
    guild = _guild(guildId)
    return {"guildId": guild, "items": queries.stalker_subscriptions(_db(request), guild)}


@router.get("/audit")
def get_audit(
    request: Request,
    guildId: str,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    origin: str = Query("", pattern="^(|web|discord)$"),
    userId: str = Query("", description="фильтр «над кем» совершено действие"),
    action: str = Query(""),
    ok: str = Query("", pattern="^(||1|0)$"),
    dateFrom: str = Query(""),
    dateTo: str = Query(""),
    sort: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict:
    guild = _guild(guildId)
    if userId and not SNOWFLAKE_RE.match(userId):
        raise HTTPException(status_code=422, detail="invalid userId")
    return mutations.audit_page(
        _db(request),
        guild,
        page,
        size,
        origin or None,
        target_user=userId or None,
        action=action.strip() or None,
        ok=None if ok == "" else ok == "1",
        date_from=_date_bound(dateFrom, end=False),
        date_to=_date_bound(dateTo, end=True),
        sort=sort,
    )


@router.get("/audit/actions")
def get_audit_actions(request: Request, guildId: str) -> dict:
    guild = _guild(guildId)
    return {"guildId": guild, "items": mutations.audit_actions(_db(request), guild)}
