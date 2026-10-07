from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from api import discord_api
from api.config import WebConfig
from api.main import create_app
from fakes import FakeDB


GUILD = "170000000000000000"


def _client() -> TestClient:
    cfg = WebConfig(
        app_env="production",
        discord_token="bot-token-for-test",
        discord_client_id="client-id",
        discord_client_secret="client-secret-for-test",
        web_session_secret="s" * 40,
        web_public_url="https://panel.example",
        discord_redirect_uri="https://panel.example/api/auth/callback",
        guild_allowlist=frozenset({GUILD}),
        mongo_uri="",
    )
    return TestClient(create_app(config=cfg, db=FakeDB()), base_url="https://panel.example")


def test_session_cookie_writes_require_configured_origin() -> None:
    for origin, expected in [
        (None, 403),
        ("null", 403),
        ("https://evil.example", 403),
        ("https://panel.example.evil.example", 403),
        ("https://panel.example", 200),
    ]:
        client = _client()
        client.cookies.set("session", "unsigned-test-cookie")
        headers = {"Origin": origin} if origin is not None else {}
        response = client.post("/api/auth/logout", headers=headers)
        assert response.status_code == expected


def test_origin_gate_covers_all_mutating_verbs_and_does_not_use_host() -> None:
    client = _client()
    client.cookies.set("session", "unsigned-test-cookie")
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        response = client.request(method, f"/api/guild/{GUILD}/settings", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403
    # A forged Host cannot become the trusted origin.
    response = client.post("/api/auth/logout", headers={"Host": "evil.example", "Origin": "https://evil.example"})
    assert response.status_code == 403


def test_public_probes_hide_internals_and_admin_diagnostics_remain_private() -> None:
    client = _client()
    health = client.get("/api/healthz")
    ready = client.get("/api/readyz")
    assert health.json() == {"status": "ok"}
    assert set(ready.json()) == {"status"}
    assert client.get(f"/api/internal/diagnostics/{GUILD}").status_code == 401
    assert client.get("/api/auth/callback?code=example").status_code == 400  # OAuth GET is unaffected
    assert health.headers["x-content-type-options"] == "nosniff"
    assert health.headers["x-frame-options"] == "DENY"
    assert health.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert health.headers["strict-transport-security"] == "max-age=31536000"


def test_authenticated_oauth_session_accepts_only_same_origin_writes(monkeypatch) -> None:
    async def token(*_args):
        return "access-token"

    async def user_guilds(*_args):
        return {"id": "160000000000000001", "username": "admin"}, []

    async def permissions(*_args):
        return "allowed", 1 << 3

    monkeypatch.setattr(discord_api, "oauth_token", token)
    monkeypatch.setattr(discord_api, "oauth_user_guilds", user_guilds)
    monkeypatch.setattr(discord_api, "resolve_permissions", permissions)
    client = _client()
    login = client.get("/api/auth/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    callback = client.get(f"/api/auth/callback?code=abc&state={state}", follow_redirects=False)
    assert callback.status_code == 302
    assert client.get(f"/api/internal/diagnostics/{GUILD}").status_code == 200
    assert client.post("/api/auth/logout").status_code == 403
    assert client.get("/api/auth/whoami").json()["authenticated"] is True
    assert client.post("/api/auth/logout", headers={"Origin": "https://panel.example"}).status_code == 200
    assert client.get("/api/auth/whoami").json()["authenticated"] is False
