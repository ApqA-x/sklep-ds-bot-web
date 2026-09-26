"""T03 resolver tests: live Discord permissions (A01, A02, A05, A06).

The real Get Guild Member REST response has no `permissions` field — the
resolver must compute bits from guild owner flag, @everyone and member roles,
and must distinguish denied (404) from unavailable (error/timeout).
"""
from __future__ import annotations

import asyncio

import pytest

from api import discord_api
from api.config import WebConfig

GUILD = "170000000000000000"
OWNER = "160000000000000001"
ADMIN_ROLE = "150000000000000001"
MOD_ROLE = "150000000000000002"
USER = "160000000000000009"

ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5


@pytest.fixture(autouse=True)
def _clean_caches() -> None:
    discord_api._PERM_CONTEXT_CACHE.clear()


def _cfg() -> WebConfig:
    return WebConfig(discord_token="t")


def _patch_rest(monkeypatch, routes: dict[str, object]) -> None:
    """routes: url-suffix -> payload or Exception to raise."""

    async def fake_get_json(url, headers, *, op):
        for suffix, value in routes.items():
            if url.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unmocked url {url}")

    monkeypatch.setattr(discord_api, "_get_json", fake_get_json)


def _guild_routes(**over) -> dict[str, object]:
    routes: dict[str, object] = {
        f"/guilds/{GUILD}": {"id": GUILD, "owner_id": OWNER},
        f"/guilds/{GUILD}/roles": [
            {"id": GUILD, "permissions": str(MANAGE_GUILD)},  # @everyone
            {"id": ADMIN_ROLE, "permissions": str(ADMINISTRATOR)},
            {"id": MOD_ROLE, "permissions": str(MANAGE_GUILD)},
        ],
    }
    routes.update(over)
    return routes


# A01: owner without any admin role is allowed.
def test_owner_allowed_without_roles(monkeypatch) -> None:
    _patch_rest(monkeypatch, _guild_routes())
    status, perms = asyncio.run(discord_api.resolve_permissions(_cfg(), GUILD, OWNER))
    assert status == "allowed"
    assert perms & ADMINISTRATOR


# A02: member REST response without `permissions` field — bits from roles.
def test_permissions_computed_from_roles(monkeypatch) -> None:
    member = {"user": {"id": USER}, "roles": [MOD_ROLE]}  # no "permissions" key
    _patch_rest(monkeypatch, _guild_routes(**{f"/guilds/{GUILD}/members/{USER}": member}))
    status, perms = asyncio.run(discord_api.resolve_permissions(_cfg(), GUILD, USER))
    assert status == "allowed"
    assert perms & MANAGE_GUILD
    assert not perms & ADMINISTRATOR

    member = {"user": {"id": USER}, "roles": [ADMIN_ROLE]}
    _patch_rest(monkeypatch, _guild_routes(**{f"/guilds/{GUILD}/members/{USER}": member}))
    status, perms = asyncio.run(discord_api.resolve_permissions(_cfg(), GUILD, USER))
    assert status == "allowed" and perms & ADMINISTRATOR


# A05: member 404 after leaving is denied; no fallback to anything stored.
def test_member_404_denied(monkeypatch) -> None:
    routes = _guild_routes()
    routes[f"/guilds/{GUILD}/members/{USER}"] = discord_api.DiscordError("member", 404, "not found")
    _patch_rest(monkeypatch, routes)
    status, perms = asyncio.run(discord_api.resolve_permissions(_cfg(), GUILD, USER))
    assert status == "denied" and perms == 0


# A06: 429/5xx/bot-403 are unavailable — never a silent deny or allow.
@pytest.mark.parametrize(
    "error",
    [
        discord_api.DiscordError("member", 429, "rate limited"),
        discord_api.DiscordError("member", 500, "server error"),
        discord_api.DiscordError("member", 403, "bot cannot see member"),
    ],
)
def test_errors_unavailable(monkeypatch, error) -> None:
    routes = _guild_routes()
    routes[f"/guilds/{GUILD}/members/{USER}"] = error
    _patch_rest(monkeypatch, routes)
    with pytest.raises(discord_api.PermissionUnavailable):
        asyncio.run(discord_api.resolve_permissions(_cfg(), GUILD, USER))


def test_guild_404_denied(monkeypatch) -> None:
    routes = _guild_routes()
    routes[f"/guilds/{GUILD}"] = discord_api.DiscordError("perm_guild", 404, "unknown guild")
    _patch_rest(monkeypatch, routes)
    status, perms = asyncio.run(discord_api.resolve_permissions(_cfg(), GUILD, USER))
    assert status == "denied" and perms == 0


def test_no_token_is_unavailable(monkeypatch) -> None:
    _patch_rest(monkeypatch, _guild_routes())
    with pytest.raises(discord_api.PermissionUnavailable):
        asyncio.run(discord_api.resolve_permissions(WebConfig(discord_token=""), GUILD, USER))
