"""An offline old bot can authorize archive reads without granting archive writes."""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api import audit_sync, auth, discord_api
from api.config import ConfigError, WebConfig, load_config
from api.main import create_app
from fakes import FakeDB


ACTIVE = "1034893946213642270"
ARCHIVE_A = "1035551411687145572"
ARCHIVE_B = "997877836872421506"
USER = "160000000000000001"
ADMINISTRATOR = 1 << 3


def _config() -> WebConfig:
    return WebConfig(
        app_env="production",
        mongo_uri="",
        mongo_db="",
        discord_token="new-bot-token",
        discord_application_id="1558569238841786571",
        archive_discord_token="old-bot-token",
        archive_discord_application_id="1507491713864826951",
        discord_client_id="1558569238841786571",
        discord_client_secret="new-client-secret",
        discord_redirect_uri="https://panel.example/api/auth/callback",
        web_session_secret="s" * 40,
        web_public_url="https://panel.example",
        guild_allowlist=frozenset({ACTIVE, ARCHIVE_A, ARCHIVE_B}),
        archive_guild_allowlist=frozenset({ARCHIVE_A, ARCHIVE_B}),
    )


def _login(client: TestClient) -> None:
    login = client.get("/api/auth/login", follow_redirects=False)
    assert login.status_code == 302
    state = re.search(r"[&?]state=([^&]+)", login.headers["location"]).group(1)
    callback = client.get(f"/api/auth/callback?code=example&state={state}", follow_redirects=False)
    assert callback.status_code == 302


def test_archive_configuration_fails_closed() -> None:
    env = {
        "WEB_ENV": "production",
        "DISCORD_TOKEN": "new-bot-token",
        "DISCORD_CLIENT_ID": "new-id",
        "DISCORD_CLIENT_SECRET": "new-secret",
        "WEB_SESSION_SECRET": "s" * 40,
        "WEB_PUBLIC_URL": "https://panel.example",
        "WEB_GUILD_ALLOWLIST": f"{ACTIVE},{ARCHIVE_A}",
        "WEB_ARCHIVE_GUILD_ALLOWLIST": ARCHIVE_A,
    }
    with pytest.raises(ConfigError, match="WEB_ARCHIVE_DISCORD_TOKEN"):
        load_config(env=env)
    env.update(WEB_ARCHIVE_DISCORD_TOKEN="old-bot-token", WEB_ARCHIVE_DISCORD_APPLICATION_ID="1507491713864826951")
    cfg = load_config(env=env)
    assert cfg.for_guild(ACTIVE).discord_token == "new-bot-token"
    assert cfg.for_guild(ARCHIVE_A).discord_token == "old-bot-token"
    env["WEB_ARCHIVE_GUILD_ALLOWLIST"] = ARCHIVE_B
    with pytest.raises(ConfigError, match="subset"):
        load_config(env=env)


def test_archives_use_old_bot_for_live_permissions_and_never_allow_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    auth._clear_recheck_cache()
    seen: list[tuple[str, str]] = []

    async def oauth_token(*_args):
        return "oauth-token"

    async def oauth_user_guilds(*_args):
        return {"id": USER, "username": "owner"}, []

    async def bot_guilds(cfg):
        if cfg.discord_token == "new-bot-token":
            # Even if the old bot still sees ACTIVE, archived guilds are not
            # promoted to writable by the new bot's guild listing.
            return [{"id": ACTIVE, "name": "New"}, {"id": ARCHIVE_A, "name": "Old A"}]
        assert cfg.discord_token == "old-bot-token"
        return [{"id": ARCHIVE_A, "name": "Old A"}, {"id": ARCHIVE_B, "name": "Old B"}]

    async def resolve_permissions(cfg, guild_id, user_id):
        assert user_id == USER
        seen.append((guild_id, cfg.discord_token))
        return "allowed", ADMINISTRATOR

    monkeypatch.setattr(discord_api, "oauth_token", oauth_token)
    monkeypatch.setattr(discord_api, "oauth_user_guilds", oauth_user_guilds)
    monkeypatch.setattr(discord_api, "bot_guilds", bot_guilds)
    monkeypatch.setattr(discord_api, "resolve_permissions", resolve_permissions)
    client = TestClient(create_app(config=_config(), db=FakeDB()), base_url="https://panel.example")
    _login(client)

    access = {row["guildId"]: row for row in client.get("/api/guilds").json()["guilds"]}
    assert set(access) == {ACTIVE, ARCHIVE_A, ARCHIVE_B}
    assert access[ACTIVE]["canWrite"] is True
    assert access[ARCHIVE_A]["canWrite"] is False
    assert access[ARCHIVE_B]["canWrite"] is False
    assert (ACTIVE, "new-bot-token") in seen
    assert (ARCHIVE_A, "old-bot-token") in seen
    assert (ARCHIVE_B, "old-bot-token") in seen

    assert client.get(f"/api/guild/{ARCHIVE_A}/leaderboard").status_code == 200
    blocked = client.get(f"/api/internal/diagnostics/{ARCHIVE_A}")
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "archived guild is read-only"
    blocked = client.post(
        f"/api/guild/{ARCHIVE_A}/audit/discord/sync",
        headers={"Origin": "https://panel.example"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "archived guild is read-only"


def test_archive_permission_revocation_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    auth._clear_recheck_cache()

    async def oauth_token(*_args):
        return "oauth-token"

    async def oauth_user_guilds(*_args):
        return {"id": USER, "username": "owner"}, []

    async def resolve_permissions(cfg, guild_id, user_id):
        if guild_id == ARCHIVE_A:
            return "denied", 0
        return "allowed", ADMINISTRATOR

    monkeypatch.setattr(discord_api, "oauth_token", oauth_token)
    monkeypatch.setattr(discord_api, "oauth_user_guilds", oauth_user_guilds)
    monkeypatch.setattr(discord_api, "resolve_permissions", resolve_permissions)
    client = TestClient(create_app(config=_config(), db=FakeDB()), base_url="https://panel.example")
    _login(client)
    assert client.get(f"/api/guild/{ARCHIVE_A}/leaderboard").status_code == 403


def test_background_audit_sync_skips_archives(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    ticks = 0

    async def one_cycle(_seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks > 1:
            raise asyncio.CancelledError

    async def sync(_db, _cfg, guild: str) -> None:
        called.append(guild)

    monkeypatch.setattr(audit_sync.asyncio, "sleep", one_cycle)
    monkeypatch.setattr(audit_sync, "known_guilds", lambda _db: [ACTIVE, ARCHIVE_A, ARCHIVE_B])
    monkeypatch.setattr(audit_sync, "sync_guild", sync)
    app = SimpleNamespace(state=SimpleNamespace(db=FakeDB(), config=_config()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(audit_sync.run_loop(app))
    assert called == [ACTIVE]
