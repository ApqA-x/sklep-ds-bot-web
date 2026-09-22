from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from . import mutations, queries
from .auth import actor_of, require_guild_admin
from .models import GuildSettingsPatch, ListMemberAction, StalkerAction

SNOWFLAKE_RE = re.compile(r"^\d{5,25}$")

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
def patch_settings(request: Request, guildId: str, body: GuildSettingsPatch) -> dict:
    guild = _guild(guildId)
    if not body.mongo_set():
        raise HTTPException(status_code=422, detail="empty patch")
    status, doc = mutations.patch_guild_settings(_db(request), guild, body, actor_of(request))
    if status == "conflict":
        raise HTTPException(status_code=409, detail="settings changed since you loaded them")
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
) -> dict:
    guild = _guild(guildId)
    return mutations.audit_page(_db(request), guild, page, size, origin or None)
