from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from .config import WebConfig

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
_BOT_GUILD_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_BOT_GUILD_TTL = 60.0
_GUILD_RESOURCE_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
_GUILD_RESOURCE_TTL = 300.0


class DiscordError(RuntimeError):
    def __init__(self, op: str, status: int, body: str) -> None:
        super().__init__(f"discord {op} failed: {status}")
        self.op = op
        self.status = status
        self.body = body


async def bot_request(
    cfg: WebConfig,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    reason: str | None = None,
) -> tuple[int, Any]:
    """Bot-token call to Discord REST; returns (status, payload). No exception on 4xx."""
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    if reason:
        headers["X-Audit-Log-Reason"] = quote(reason)
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.request(method, f"{API}{path}", json=json_body) as response:
            try:
                payload = await response.json()
            except Exception:
                payload = {"raw": (await response.text())[:300]}
            return response.status, payload


async def _get_json(url: str, headers: dict[str, str], *, op: str) -> Any:
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url) as response:
            if response.status >= 400:
                raise DiscordError(op, response.status, await response.text())
            return await response.json()


async def oauth_token(cfg: WebConfig, code: str, redirect_uri: str) -> str:
    data = {
        "client_id": cfg.discord_client_id,
        "client_secret": cfg.discord_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{API}/oauth2/token", data=data) as response:
            if response.status >= 400:
                raise DiscordError("oauth_token", response.status, await response.text())
            payload = await response.json()
    return str(payload["access_token"])


async def oauth_user_guilds(access_token: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {access_token}"}
    user = await _get_json(f"{API}/users/@me", headers, op="oauth_me")
    guilds = await _get_json(f"{API}/users/@me/guilds", headers, op="oauth_guilds")
    return user, guilds


async def fetch_bot_guilds(cfg: WebConfig) -> list[dict[str, Any]]:
    if not cfg.discord_token:
        return []
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    if cfg.discord_application_id:
        try:
            rows = await _get_json(
                f"{API}/applications/{cfg.discord_application_id}/guilds", headers, op="bot_guilds"
            )
            if rows:
                return rows
        except DiscordError as err:
            log.warning("application guild list unavailable (%s), falling back to bot guilds", err)
    return await _get_json(f"{API}/users/@me/guilds", headers, op="bot_guilds") or []


async def bot_guilds(cfg: WebConfig) -> list[dict[str, Any]]:
    key = cfg.discord_application_id or cfg.mongo_db
    cached = _BOT_GUILD_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _BOT_GUILD_TTL:
        return cached[1]
    guilds = await fetch_bot_guilds(cfg)
    _BOT_GUILD_CACHE[key] = (time.monotonic(), guilds)
    return guilds


async def _guild_resource_rows(cfg: WebConfig, guild_id: str, kind: str) -> list[dict[str, Any]]:
    key = (guild_id, kind)
    cached = _GUILD_RESOURCE_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _GUILD_RESOURCE_TTL:
        return cached[1]
    if not cfg.discord_token:
        return []
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    rows = await _get_json(f"{API}/guilds/{guild_id}/{kind}", headers, op=kind) or []
    _GUILD_RESOURCE_CACHE[key] = (time.monotonic(), rows)
    return rows


def _rows_to_names(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {str(row["id"]): str(row.get("name") or "") for row in rows if row.get("id")}


async def guild_channel_names(cfg: WebConfig, guild_id: str) -> dict[str, str]:
    return _rows_to_names(await _guild_resource_rows(cfg, guild_id, "channels"))


async def guild_role_names(cfg: WebConfig, guild_id: str) -> dict[str, str]:
    return _rows_to_names(await _guild_resource_rows(cfg, guild_id, "roles"))


async def guild_roles_raw(cfg: WebConfig, guild_id: str) -> list[dict[str, Any]]:
    return await _guild_resource_rows(cfg, guild_id, "roles")


async def guild_channels_raw(cfg: WebConfig, guild_id: str) -> list[dict[str, Any]]:
    return await _guild_resource_rows(cfg, guild_id, "channels")


async def bot_user_id(cfg: WebConfig) -> str | None:
    """The bot's own user id (== application id); the bot user object as fallback."""
    if cfg.discord_application_id:
        return cfg.discord_application_id
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    me = await _get_json(f"{API}/users/@me", headers, op="bot_me")
    return str(me.get("id") or "") or None


async def bot_top_role_position(cfg: WebConfig, guild_id: str) -> int | None:
    """Position of the bot's highest role in a guild, or None when it cannot be determined."""
    if not cfg.discord_token:
        return None
    key = (guild_id, "topRolePosition")
    cached = _GUILD_RESOURCE_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _GUILD_RESOURCE_TTL:
        return cached[1]
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    try:
        me_id = await bot_user_id(cfg)
        member = await _get_json(f"{API}/guilds/{guild_id}/members/{me_id}", headers, op="bot_member")
    except DiscordError as err:
        log.warning("bot member lookup failed in guild %s (%s); treating all unmanaged roles as assignable", guild_id, err)
        _GUILD_RESOURCE_CACHE[key] = (time.monotonic(), None)
        return None
    bot_role_ids = {str(rid) for rid in member.get("roles", [])}
    rows = await _guild_resource_rows(cfg, guild_id, "roles")
    top = max((int(row.get("position") or 0) for row in rows if str(row.get("id")) in bot_role_ids), default=0)
    _GUILD_RESOURCE_CACHE[key] = (time.monotonic(), top)
    return top


def build_role_options(
    rows: list[dict[str, Any]], guild_id: str, bot_top_position: int | None
) -> list[dict[str, Any]]:
    """Assignable-role picker list: no @everyone, no managed roles, below the bot's top role."""
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: int(r.get("position") or 0), reverse=True):
        rid = str(row.get("id") or "")
        if rid == "" or rid == str(guild_id):
            continue
        managed = bool(row.get("managed") or row.get("tags"))
        position = int(row.get("position") or 0)
        assignable = not managed and (bot_top_position is None or position < bot_top_position)
        out.append(
            {
                "id": rid,
                "name": str(row.get("name") or rid),
                "color": int(row.get("color") or 0),
                "position": position,
                "managed": managed,
                "assignable": assignable,
            }
        )
    return out


VOICE_CHANNEL_TYPES = (2, 13)


def build_voice_channels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: (int(r.get("position") or 0), str(r.get("id") or ""))):
        if int(row.get("type") or 0) not in VOICE_CHANNEL_TYPES:
            continue
        cid = str(row.get("id") or "")
        if cid == "":
            continue
        out.append({"id": cid, "name": str(row.get("name") or cid), "type": int(row.get("type"))})
    return out


async def get_member(cfg: WebConfig, guild_id: str, user_id: str) -> dict[str, Any] | None:
    """Raw guild member via bot token; None when the user is not a member."""
    if not cfg.discord_token:
        return None
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    try:
        return await _get_json(f"{API}/guilds/{guild_id}/members/{user_id}", headers, op="member")
    except DiscordError as err:
        if err.status == 404:
            return None
        raise


async def member_permissions(cfg: WebConfig, guild_id: str, user_id: str) -> int | None:
    """Computed permissions bitfield of a member via bot token, or None if not a member."""
    if not cfg.discord_token:
        return None
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    try:
        member = await _get_json(
            f"{API}/guilds/{guild_id}/members/{user_id}", headers, op="member"
        )
    except DiscordError as err:
        if err.status == 404:
            return None
        raise
    raw = member.get("permissions")
    if raw is None:
        return None
    return int(raw)
