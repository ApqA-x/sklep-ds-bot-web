from __future__ import annotations

import asyncio
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
_PERM_CACHE_MAX = 4096

router = APIRouter(prefix="/api/auth", tags=["auth"])
guilds_router = APIRouter(tags=["read"])

# (guildId, userId) -> (monotonic_ts, "allowed"|"denied", perms)
_recheck_cache: dict[tuple[str, str], tuple[float, str, int]] = {}


def _cfg(request: Request) -> WebConfig:
    return request.app.state.config


def _clear_recheck_cache() -> None:
    _recheck_cache.clear()
    discord_api._PERM_CONTEXT_CACHE.clear()


def _perm_cache_put(key: tuple[str, str], status: str, perms: int) -> None:
    if len(_recheck_cache) >= _PERM_CACHE_MAX:
        oldest = min(_recheck_cache, key=lambda k: _recheck_cache[k][0])
        del _recheck_cache[oldest]
    _recheck_cache[key] = (time.monotonic(), status, perms)


async def _resolve_fresh(request: Request, guild_id: str, user_id: str) -> tuple[str, int]:
    """Allowed/denied within the TTL bound; never an older grant, never an allow
    on errors — unavailable raises 503 so the client can retry."""
    cfg = _cfg(request)
    try:
        status, perms = await discord_api.resolve_permissions(cfg, guild_id, user_id)
    except discord_api.PermissionUnavailable as err:
        raise HTTPException(
            status_code=503, detail="discord permission check unavailable, retry later"
        ) from err
    _perm_cache_put((guild_id, user_id), status, perms)
    return status, perms


async def perms_for(request: Request, guild_id: str) -> int:
    """Current effective perms of the session user in a guild (TTL 60s, fresh resolver)."""
    cfg = _cfg(request)
    if not cfg.auth_enabled:
        return ADMINISTRATOR  # dev mode: gate disabled, deployment must not expose port
    if not cfg.guild_allowed(guild_id):
        # D01: guilds outside the release allowlist do not exist for the panel.
        raise HTTPException(status_code=404, detail="guild not found")
    session_data = request.session.get(SESSION_KEY)
    if not session_data:
        raise HTTPException(status_code=401, detail="login required")
    user_id = str(session_data["userId"])
    issued_at = float(session_data.get("issuedAt") or 0)
    if issued_at <= 0 or time.time() - issued_at > cfg.session_max_age_hours * 3600:
        request.session.pop(SESSION_KEY, None)
        raise HTTPException(status_code=401, detail="session expired, log in again")
    cached = _recheck_cache.get((guild_id, user_id))
    if cached is not None and time.monotonic() - cached[0] < PERM_RECHECK_TTL:
        status, perms = cached[1], cached[2]
    else:
        status, perms = await _resolve_fresh(request, guild_id, user_id)
    if status != "allowed":
        raise HTTPException(status_code=403, detail="not a member with access to this guild")
    return perms


async def require_guild_read(request: Request) -> None:
    # D02: panel policy is owner/Administrator; MANAGE_GUILD alone is not enough.
    guild_id = request.path_params.get("guildId", "")
    perms = await perms_for(request, guild_id)
    if not perms & ADMINISTRATOR:
        raise HTTPException(status_code=403, detail="administrator permission required")


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
    # Never derive the OAuth callback from the request Host/forwarded headers:
    # a spoofed host must not redirect Discord's code parameter to another origin.
    if cfg.discord_redirect_uri:
        return cfg.discord_redirect_uri
    if cfg.web_public_url:
        return cfg.web_public_url.rstrip("/") + "/api/auth/callback"
    return str(request.url_for("auth_callback"))


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
        user, _guilds = await discord_api.oauth_user_guilds(access_token)
    except discord_api.DiscordError as err:
        log.warning("oauth exchange failed: %s", err)
        raise HTTPException(status_code=502, detail="discord oauth failed") from err
    # The OAuth guild snapshot is deliberately NOT stored: panel access is decided
    # by the fresh resolver (owner flag + current roles), never by login-time perms.
    request.session[SESSION_KEY] = {
        "userId": str(user["id"]),
        "userName": str(user.get("username") or ""),
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
    user_id = str(session_data["userId"])
    issued_at = float(session_data.get("issuedAt") or 0)
    if issued_at <= 0 or time.time() - issued_at > cfg.session_max_age_hours * 3600:
        request.session.pop(SESSION_KEY, None)
        return {"authenticated": False, "authEnabled": True, "loginUrl": "/api/auth/login", "access": []}
    access = await _accessible_guilds(request, cfg, user_id)
    return {
        "authenticated": True,
        "authEnabled": True,
        "user": {"userId": user_id, "userName": str(session_data.get("userName") or "")},
        "access": access,
    }


async def _accessible_guilds(request: Request, cfg: WebConfig, user_id: str) -> list[dict]:
    """Bot-visible guilds where the CURRENT resolver says the user is allowed."""
    try:
        bot_guilds = await discord_api.bot_guilds(cfg)
    except discord_api.DiscordError as err:
        log.warning("bot guild list failed: %s", err)
        bot_guilds = []
    out = []
    for guild in bot_guilds:
        gid = str(guild.get("id") or "")
        if not gid or not cfg.guild_allowed(gid):
            continue
        try:
            status, perms = await _resolve_fresh(request, gid, user_id)
        except HTTPException:
            continue  # unavailable for this guild right now — omit from the list
        if status != "allowed":
            continue
        admin = bool(perms & ADMINISTRATOR)
        if not admin:
            # D02/D10: the panel is only for owner/Administrator; MANAGE_GUILD
            # alone is no access at all, so such guilds are not listed.
            continue
        out.append(
            {
                "guildId": gid,
                "name": str(guild.get("name") or gid),
                "canRead": True,
                "canWrite": True,
            }
        )
    return out


def _guild_names(bot_guilds: list[dict]) -> dict[str, str]:
    return {str(g.get("id")): str(g.get("name") or "") for g in bot_guilds}


@guilds_router.get("/api/guilds")
async def list_guilds(request: Request):
    """Guilds the bot is present in, intersected with the user's live permissions."""
    cfg = _cfg(request)
    if not cfg.auth_enabled:
        bot_guilds = []
        try:
            bot_guilds = await discord_api.bot_guilds(cfg)
        except discord_api.DiscordError as err:
            log.warning("bot guild list failed: %s", err)
        names = _guild_names(bot_guilds)
        db = getattr(request.app.state, "db", None)
        if db is not None:
            # T16 п.3: синхронный pymongo вне event loop (пусть и маленький scan)
            def _known_guild_ids() -> list[str]:
                from .limits import timeout_kwargs

                return [
                    str(doc["_id"])
                    for doc in db["guild_settings"].find(
                        {}, projection={"_id": 1}, **timeout_kwargs()
                    )
                ]

            for gid in await asyncio.to_thread(_known_guild_ids):
                names.setdefault(gid, "")
        return {
            "guilds": [
                {"guildId": gid, "name": name or gid, "canRead": True, "canWrite": True}
                for gid, name in names.items()
            ]
        }
    session_data = request.session.get(SESSION_KEY)
    if not session_data:
        return {"guilds": []}
    user_id = str(session_data["userId"])
    access = await _accessible_guilds(request, cfg, user_id)
    return {"guilds": access}
