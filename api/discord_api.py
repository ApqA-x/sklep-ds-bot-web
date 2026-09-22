from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import aiohttp

from .config import WebConfig

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
        rows = await _get_json(
            f"{API}/applications/{cfg.discord_application_id}/guilds", headers, op="bot_guilds"
        )
        return rows or []
    return await _get_json(f"{API}/users/@me/guilds", headers, op="bot_guilds") or []


async def bot_guilds(cfg: WebConfig) -> list[dict[str, Any]]:
    key = cfg.discord_application_id or cfg.mongo_db
    cached = _BOT_GUILD_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _BOT_GUILD_TTL:
        return cached[1]
    guilds = await fetch_bot_guilds(cfg)
    _BOT_GUILD_CACHE[key] = (time.monotonic(), guilds)
    return guilds


async def _cached_guild_resource(cfg: WebConfig, guild_id: str, kind: str) -> dict[str, str]:
    key = (guild_id, kind)
    cached = _GUILD_RESOURCE_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _GUILD_RESOURCE_TTL:
        return cached[1]
    if not cfg.discord_token:
        return {}
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    rows = await _get_json(f"{API}/guilds/{guild_id}/{kind}", headers, op=kind) or []
    names = {str(row["id"]): str(row.get("name") or "") for row in rows if row.get("id")}
    _GUILD_RESOURCE_CACHE[key] = (time.monotonic(), names)
    return names


async def guild_channel_names(cfg: WebConfig, guild_id: str) -> dict[str, str]:
    return await _cached_guild_resource(cfg, guild_id, "channels")


async def guild_role_names(cfg: WebConfig, guild_id: str) -> dict[str, str]:
    return await _cached_guild_resource(cfg, guild_id, "roles")


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
