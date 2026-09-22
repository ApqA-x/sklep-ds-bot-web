from __future__ import annotations

import logging
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import RedirectResponse

from . import discord_api
from .config import WebConfig

log = logging.getLogger("api.auth")

MANAGE_GUILD = 1 << 5
ADMINISTRATOR = 1 << 3
SESSION_KEY = "discord"
STATE_COOKIE = "oauth_state"
PERM_RECHECK_TTL = 60.0

router = APIRouter(prefix="/api/auth", tags=["auth"])
guilds_router = APIRouter(tags=["read"])

# (guildId, userId) -> (monotonic_ts, perms)
_recheck_cache: dict[tuple[str, str], tuple[float, int]] = {}


def _cfg(request: Request) -> WebConfig:
    return request.app.state.config


def _clear_recheck_cache() -> None:
    _recheck_cache.clear()


async def perms_for(request: Request, guild_id: str) -> int:
    """Session perms with a fresh Discord re-check (TTL 60s) when auth is enabled."""
    cfg = _cfg(request)
    if not cfg.auth_enabled:
        return ADMINISTRATOR  # dev mode: gate disabled, deployment must not expose port
    session_data = request.session.get(SESSION_KEY)
    if not session_data:
        raise HTTPException(status_code=401, detail="login required")
    user_id = str(session_data["userId"])
    cached = _recheck_cache.get((guild_id, user_id))
    if cached is not None and time.monotonic() - cached[0] < PERM_RECHECK_TTL:
        return cached[1]
    perms = int(session_data.get("perms", {}).get(guild_id, 0))
    try:
        fresh = await discord_api.member_permissions(cfg, guild_id, user_id)
        if fresh is not None:
            perms = fresh
    except discord_api.DiscordError as err:
        if err.status == 403 and perms == 0:
            perms = 0
        else:
            log.warning("permission re-check failed guild=%s: %s", guild_id, err)
    _recheck_cache[(guild_id, user_id)] = (time.monotonic(), perms)
    return perms


async def require_guild_read(request: Request) -> None:
    guild_id = request.path_params.get("guildId", "")
    perms = await perms_for(request, guild_id)
    if not perms & (MANAGE_GUILD | ADMINISTRATOR):
        raise HTTPException(status_code=403, detail="manage guild permission required")


async def require_guild_admin(request: Request) -> None:
    guild_id = request.path_params.get("guildId", "")
    perms = await perms_for(request, guild_id)
    if not perms & ADMINISTRATOR:
        raise HTTPException(status_code=403, detail="administrator permission required")


def actor_of(request: Request) -> dict[str, str]:
    """Author of a write action: Discord identity from session, or dev marker."""
    cfg = _cfg(request)
    if not cfg.auth_enabled:
        return {"userId": "0", "userName": "dev"}
    session_data = request.session.get(SESSION_KEY) or {}
    return {
        "userId": str(session_data.get("userId") or ""),
        "userName": str(session_data.get("userName") or ""),
    }


def _redirect_uri(cfg: WebConfig, request: Request) -> str:
    return cfg.discord_redirect_uri or str(request.url_for("auth_callback"))


def _is_https(cfg: WebConfig) -> bool:
    return cfg.web_public_url.startswith("https://")


@router.get("/login")
async def auth_login(request: Request):
    cfg = _cfg(request)
    if not cfg.auth_enabled:
        raise HTTPException(status_code=503, detail="auth is not configured")
    state = secrets.token_urlsafe(24)
    url = (
        "https://discord.com/oauth2/authorize"
        f"?client_id={quote(cfg.discord_client_id)}"
        "&response_type=code"
        "&scope=identify%20guilds"
        f"&redirect_uri={quote(_redirect_uri(cfg, request), safe='')}"
        f"&state={quote(state, safe='')}"
        "&prompt=none"
    )
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax", secure=_is_https(cfg)
    )
    return response


@router.get("/callback", name="auth_callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    cfg = _cfg(request)
    if not cfg.auth_enabled:
        raise HTTPException(status_code=503, detail="auth is not configured")
    if not code:
        raise HTTPException(status_code=400, detail="missing code")
    expected_state = request.cookies.get(STATE_COOKIE)
    if not expected_state or not secrets.compare_digest(expected_state, state):
        raise HTTPException(status_code=400, detail="invalid oauth state")
    try:
        access_token = await discord_api.oauth_token(cfg, code, _redirect_uri(cfg, request))
        user, guilds = await discord_api.oauth_user_guilds(access_token)
    except discord_api.DiscordError as err:
        log.warning("oauth exchange failed: %s", err)
        raise HTTPException(status_code=502, detail="discord oauth failed") from err
    perms = {}
    for guild in guilds:
        if int(guild.get("permissions") or 0) & (MANAGE_GUILD | ADMINISTRATOR):
            perms[str(guild["id"])] = int(guild["permissions"] or 0)
    request.session[SESSION_KEY] = {
        "userId": str(user["id"]),
        "userName": str(user.get("username") or ""),
        "perms": perms,
        "issuedAt": time.time(),
    }
    _clear_recheck_cache()
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(STATE_COOKIE)
    return response


@router.post("/logout")
async def auth_logout(request: Request):
    request.session.pop(SESSION_KEY, None)
    return {"ok": True}


@router.get("/whoami")
async def auth_whoami(request: Request):
    cfg = _cfg(request)
    if not cfg.auth_enabled:
        return {"authenticated": False, "authEnabled": False, "devMode": True, "access": []}
    session_data = request.session.get(SESSION_KEY)
    if not session_data:
        return {"authenticated": False, "authEnabled": True, "loginUrl": "/api/auth/login", "access": []}
    access = [
        {
            "guildId": gid,
            "canRead": True,
            "canWrite": bool(perms & ADMINISTRATOR),
        }
        for gid, perms in session_data.get("perms", {}).items()
    ]
    return {
        "authenticated": True,
        "authEnabled": True,
        "user": {"userId": session_data["userId"], "userName": session_data["userName"]},
        "access": access,
    }


def _guild_names(bot_guilds: list[dict]) -> dict[str, str]:
    return {str(g.get("id")): str(g.get("name") or "") for g in bot_guilds}


@guilds_router.get("/api/guilds")
async def list_guilds(request: Request):
    """Guilds the bot is present in, intersected with the user's permissions."""
    cfg = _cfg(request)
    bot_guilds = []
    try:
        bot_guilds = await discord_api.bot_guilds(cfg)
    except discord_api.DiscordError as err:
        log.warning("bot guild list failed: %s", err)
    if not bot_guilds and not cfg.auth_enabled:
        db = getattr(request.app.state, "db", None)
        if db is not None:
            names = {}
            for doc in db["guild_settings"].find({}, projection={"_id": 1}):
                names[str(doc["_id"])] = ""
            return {"guilds": [{"guildId": gid, "name": name or gid, "canRead": True, "canWrite": True} for gid, name in names.items()]}
    names = _guild_names(bot_guilds)
    if not cfg.auth_enabled:
        return {
            "guilds": [
                {"guildId": gid, "name": name or gid, "canRead": True, "canWrite": True}
                for gid, name in names.items()
            ]
        }
    session_data = request.session.get(SESSION_KEY)
    if not session_data:
        return {"guilds": []}
    result = []
    for gid, perms in session_data.get("perms", {}).items():
        if gid not in names:
            continue
        result.append(
            {
                "guildId": gid,
                "name": names[gid] or gid,
                "canRead": True,
                "canWrite": bool(perms & ADMINISTRATOR),
            }
        )
    return {"guilds": result}
