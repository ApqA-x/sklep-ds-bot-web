from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from api import auth as auth_module
from api import discord_api
from api import queries
from api.config import WebConfig
from api.main import create_app
from fakes import FakeDB

GUILD = "170000000000000000"
OTHER_GUILD = "180000000000000000"
ADMIN = "160000000000000001"

MANAGE = 1 << 5
ADMINISTRATOR = 1 << 3


def _auth_cfg(**overrides) -> WebConfig:
    base = dict(
        mongo_uri="",
        mongo_db="",
        discord_client_id="cid",
        discord_client_secret="secret",
        discord_redirect_uri="https://example.invalid/api/auth/callback",
        web_session_secret="s" * 32,
        web_public_url="https://example.invalid",
    )
    base.update(overrides)
    return WebConfig(**base)


def _client(cfg: WebConfig) -> TestClient:
    app = create_app(config=cfg, db=FakeDB())
    # secure-cookies (session + oauth state) require an https origin;
    # follow_redirects=False keeps 302 dance (login/callback) assertable
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    queries._leaderboard_cache.clear()
    auth_module._clear_recheck_cache()


@pytest.fixture()
def mocked_oauth(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"user": {"id": ADMIN, "username": "admin"}, "guilds": [], "resolver": {}}

    async def fake_token(cfg, code, redirect_uri):
        return "access-token"

    async def fake_user_guilds(access_token):
        return state["user"], state["guilds"]

    async def fake_resolve_permissions(cfg, guild_id, user_id):
        entry = state["resolver"].get(guild_id)
        if entry is None:
            return ("denied", 0)
        return entry

    async def fake_bot_guilds(cfg):
        return [{"id": GUILD, "name": "Main"}, {"id": OTHER_GUILD, "name": "Other"}]

    monkeypatch.setattr(discord_api, "oauth_token", fake_token)
    monkeypatch.setattr(discord_api, "oauth_user_guilds", fake_user_guilds)
    monkeypatch.setattr(discord_api, "resolve_permissions", fake_resolve_permissions)
    monkeypatch.setattr(discord_api, "bot_guilds", fake_bot_guilds)
    return state


def _login(client: TestClient, state: dict, perms: dict) -> None:
    # `perms` now feeds the live resolver (as if freshly computed from roles),
    # not an OAuth snapshot: (allowed, bits) per guild.
    state["guilds"] = [{"id": gid, "permissions": str(bits)} for gid, bits in perms.items()]
    state["resolver"] = {gid: ("allowed", bits) for gid, bits in perms.items()}
    login_response = client.get("/api/auth/login")
    assert login_response.status_code == 302
    location = login_response.headers["location"]
    assert "discord.com/oauth2/authorize" in location
    assert "scope=identify%20guilds" in location
    oauth_state = re.search(r"[&?]state=([^&]+)", location).group(1)
    callback = client.get(f"/api/auth/callback?code=abc&state={oauth_state}")
    assert callback.status_code == 302


def test_dev_mode_allows_reads_and_whoami() -> None:
    client = _client(WebConfig(mongo_uri="", mongo_db=""))
    whoami = client.get("/api/auth/whoami").json()
    assert whoami["authenticated"] is False
    assert whoami["devMode"] is True
    response = client.get(f"/api/guild/{GUILD}/leaderboard")
    assert response.status_code == 200


def test_read_requires_login_when_auth_enabled() -> None:
    client = _client(_auth_cfg())
    response = client.get(f"/api/guild/{GUILD}/leaderboard")
    assert response.status_code == 401


def test_login_flow_grants_access_by_live_permissions(mocked_oauth: dict) -> None:
    client = _client(_auth_cfg())
    _login(client, mocked_oauth, {GUILD: MANAGE | ADMINISTRATOR, OTHER_GUILD: MANAGE})

    whoami = client.get("/api/auth/whoami").json()
    assert whoami["authenticated"] is True
    access = {a["guildId"]: a for a in whoami["access"]}
    # D02: administrator is listed; MANAGE_GUILD-only guilds are no access at all.
    assert access[GUILD]["canWrite"] is True
    assert OTHER_GUILD not in access

    assert client.get(f"/api/guild/{GUILD}/settings").status_code in {200, 404}
    assert client.get(f"/api/guild/{OTHER_GUILD}/leaderboard").status_code == 403
    assert client.get("/api/guild/190000000000000000/leaderboard").status_code == 403


def test_callback_rejects_bad_state(mocked_oauth: dict) -> None:
    client = _client(_auth_cfg())
    client.get("/api/auth/login")
    response = client.get("/api/auth/callback?code=abc&state=wrong")
    assert response.status_code == 400


def test_logout_clears_session(mocked_oauth: dict) -> None:
    client = _client(_auth_cfg())
    _login(client, mocked_oauth, {GUILD: MANAGE})
    assert client.get("/api/auth/whoami").json()["authenticated"] is True
    client.post("/api/auth/logout")
    assert client.get("/api/auth/whoami").json()["authenticated"] is False


def test_guilds_endpoint_requires_live_admin(mocked_oauth: dict) -> None:
    client = _client(_auth_cfg())
    _login(client, mocked_oauth, {GUILD: MANAGE})
    # MANAGE_GUILD alone is not panel access (D02) — nothing is listed.
    assert client.get("/api/guilds").json()["guilds"] == []
    mocked_oauth["resolver"][GUILD] = ("allowed", MANAGE | ADMINISTRATOR)
    auth_module._clear_recheck_cache()
    guilds = client.get("/api/guilds").json()["guilds"]
    assert guilds == [
        {"guildId": GUILD, "name": "Main", "canRead": True, "canWrite": True}
    ]


def test_guilds_empty_for_anonymous_when_auth_enabled() -> None:
    client = _client(_auth_cfg())
    assert client.get("/api/guilds").json()["guilds"] == []


# --- A-group acceptance tests (AI_RELEASE_ACCEPTANCE.md) ---------------------

ADMIN_BITS = MANAGE | ADMINISTRATOR


def _logged_in_admin(mocked_oauth: dict, client: TestClient) -> None:
    _login(client, mocked_oauth, {GUILD: ADMIN_BITS})


def test_a04_role_revoked_after_ttl_denied(mocked_oauth: dict) -> None:
    client = _client(_auth_cfg())
    _logged_in_admin(mocked_oauth, client)
    assert client.get(f"/api/guild/{GUILD}/settings").status_code in {200, 404}
    # role revoked: live resolver now reports MANAGE only
    mocked_oauth["resolver"][GUILD] = ("allowed", MANAGE)
    # within the 60s bound the previous decision may stand
    assert client.get(f"/api/guild/{GUILD}/settings").status_code in {200, 404}
    # age the cache past the bound — the fresh answer must be applied
    key = (GUILD, ADMIN)
    ts, status, perms = auth_module._recheck_cache[key]
    auth_module._recheck_cache[key] = (ts - auth_module.PERM_RECHECK_TTL - 1, status, perms)
    assert client.get(f"/api/guild/{GUILD}/settings").status_code == 403


def test_a06_unavailable_check_is_503_not_allow(mocked_oauth: dict) -> None:
    client = _client(_auth_cfg())
    _logged_in_admin(mocked_oauth, client)

    async def unavailable(cfg, guild_id, user_id):
        raise discord_api.PermissionUnavailable("synthetic outage")

    auth_module._clear_recheck_cache()
    original = discord_api.resolve_permissions
    discord_api.resolve_permissions = unavailable
    try:
        response = client.get(f"/api/guild/{GUILD}/settings")
        assert response.status_code == 503
        # the 503 must not be re-cached as a grant: next call is 503 again
        assert client.get(f"/api/guild/{GUILD}/settings").status_code == 503
    finally:
        discord_api.resolve_permissions = original


def test_a07_recheck_cache_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(auth_module, "_PERM_CACHE_MAX", 8)
    for i in range(20):
        auth_module._perm_cache_put((f"g{i}", "u"), "allowed", 0)
    assert len(auth_module._recheck_cache) <= 8


def test_a08_absolute_session_expiry(mocked_oauth: dict, monkeypatch) -> None:
    client = _client(_auth_cfg())
    _logged_in_admin(mocked_oauth, client)
    assert client.get(f"/api/guild/{GUILD}/settings").status_code in {200, 404}
    cfg = _auth_cfg()
    assert cfg.session_max_age_hours == 8
    # move wall clock past the absolute session bound
    real_time = auth_module.time.time
    monkeypatch.setattr(auth_module.time, "time", lambda: real_time() + 9 * 3600)
    try:
        assert client.get(f"/api/guild/{GUILD}/settings").status_code == 401
        whoami = client.get("/api/auth/whoami").json()
        assert whoami["authenticated"] is False
    finally:
        monkeypatch.undo()


def test_a10_new_admin_not_in_any_personal_list(mocked_oauth: dict) -> None:
    # the fake bot reports a third guild the user just got admin in; there is no
    # personal whitelist anywhere in the code path — resolver says allowed.
    async def fake_bot_guilds(cfg):
        return [
            {"id": GUILD, "name": "Main"},
            {"id": "180000000000000005", "name": "Newly Administered"},
        ]

    import api.discord_api as da

    original = da.bot_guilds
    da.bot_guilds = fake_bot_guilds
    try:
        client = _client(_auth_cfg())
        _login(client, mocked_oauth, {GUILD: ADMIN_BITS})
        mocked_oauth["resolver"]["180000000000000005"] = ("allowed", ADMINISTRATOR)
        guilds = client.get("/api/guilds").json()["guilds"]
        assert "180000000000000005" in [g["guildId"] for g in guilds]
        assert client.get("/api/guild/180000000000000005/settings").status_code in {200, 404}
    finally:
        da.bot_guilds = original
