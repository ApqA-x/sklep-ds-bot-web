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
    state: dict = {"user": {"id": ADMIN, "username": "admin"}, "guilds": []}

    async def fake_token(cfg, code, redirect_uri):
        return "access-token"

    async def fake_user_guilds(access_token):
        return state["user"], state["guilds"]

    async def fake_member_permissions(cfg, guild_id, user_id):
        return None

    async def fake_bot_guilds(cfg):
        return [{"id": GUILD, "name": "Main"}, {"id": OTHER_GUILD, "name": "Other"}]

    monkeypatch.setattr(discord_api, "oauth_token", fake_token)
    monkeypatch.setattr(discord_api, "oauth_user_guilds", fake_user_guilds)
    monkeypatch.setattr(discord_api, "member_permissions", fake_member_permissions)
    monkeypatch.setattr(discord_api, "bot_guilds", fake_bot_guilds)
    return state


def _login(client: TestClient, state: dict, perms: dict) -> None:
    state["guilds"] = [{"id": gid, "permissions": str(bits)} for gid, bits in perms.items()]
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


def test_login_flow_grants_access_by_permissions(mocked_oauth: dict) -> None:
    client = _client(_auth_cfg())
    _login(client, mocked_oauth, {GUILD: MANAGE | ADMINISTRATOR, OTHER_GUILD: MANAGE})

    whoami = client.get("/api/auth/whoami").json()
    assert whoami["authenticated"] is True
    access = {a["guildId"]: a for a in whoami["access"]}
    assert access[GUILD]["canWrite"] is True
    assert access[OTHER_GUILD]["canWrite"] is False

    assert client.get(f"/api/guild/{GUILD}/settings").status_code in {200, 404}
    assert client.get(f"/api/guild/{OTHER_GUILD}/leaderboard").status_code in {200, 422, 500}
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


def test_guilds_endpoint_intersects_bot_and_user(mocked_oauth: dict) -> None:
    client = _client(_auth_cfg())
    _login(client, mocked_oauth, {GUILD: MANAGE})
    guilds = client.get("/api/guilds").json()["guilds"]
    assert guilds == [
        {"guildId": GUILD, "name": "Main", "canRead": True, "canWrite": False}
    ]


def test_guilds_empty_for_anonymous_when_auth_enabled() -> None:
    client = _client(_auth_cfg())
    assert client.get("/api/guilds").json()["guilds"] == []
