"""C01-C05: fail-closed startup configuration (AI_RELEASE_ACCEPTANCE.md group C).

These tests assert the SAFE behavior required at release, not the historical
defect reproduction from review_checks.py.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from api.config import ConfigError, WebConfig, load_config
from api.main import create_app
from fakes import FakeDB

GOOD = {
    "WEB_ENV": "production",
    "DISCORD_TOKEN": "t",
    "DISCORD_CLIENT_ID": "cid",
    "DISCORD_CLIENT_SECRET": "secret",
    "WEB_SESSION_SECRET": "s" * 40,
    "WEB_PUBLIC_URL": "https://panel.example",
    "DISCORD_REDIRECT_URI": "https://panel.example/api/auth/callback",
    "WEB_GUILD_ALLOWLIST": "170000000000000000",
    "MONGO_URI": "",
    "MONGO_DB": "",
}


def _production_env(**overrides) -> dict[str, str]:
    env = dict(GOOD)
    env.update({key: value for key, value in overrides.items()})
    return env


# C01: every mandatory key missing on its own must break production startup.
@pytest.mark.parametrize(
    "missing_key",
    [
        "DISCORD_CLIENT_ID",
        "DISCORD_CLIENT_SECRET",
        "WEB_SESSION_SECRET",
        "DISCORD_TOKEN",
    ],
)
def test_missing_each_required_key_breaks_startup(missing_key: str) -> None:
    env = _production_env()
    del env[missing_key]
    with pytest.raises(ConfigError) as err:
        load_config(env=env)
    assert missing_key in str(err.value)


# D01: production без валидного allowlist не стартует.
@pytest.mark.parametrize("raw", ["", "   ", ","])
def test_production_requires_guild_allowlist(raw: str) -> None:
    env = _production_env(WEB_GUILD_ALLOWLIST=raw)
    with pytest.raises(ConfigError) as err:
        load_config(env=env)
    assert "WEB_GUILD_ALLOWLIST" in str(err.value)


def test_allowlist_rejects_non_snowflake() -> None:
    with pytest.raises(ConfigError) as err:
        load_config(env=_production_env(WEB_GUILD_ALLOWLIST="170000000000000000, not-a-number"))
    assert "WEB_GUILD_ALLOWLIST" in str(err.value)


def test_missing_public_url_breaks_startup() -> None:
    env = _production_env()
    del env["WEB_PUBLIC_URL"]
    del env["DISCORD_REDIRECT_URI"]
    with pytest.raises(ConfigError) as err:
        load_config(env=env)
    assert "WEB_PUBLIC_URL" in str(err.value)


def test_short_session_secret_breaks_startup() -> None:
    with pytest.raises(ConfigError) as err:
        load_config(env=_production_env(WEB_SESSION_SECRET="short"))
    assert "WEB_SESSION_SECRET" in str(err.value)
    assert "short" not in str(err.value)


# C02: unknown mode refuses startup with no fallback into dev.
def test_unknown_env_refuses_startup() -> None:
    with pytest.raises(ConfigError) as err:
        load_config(env=_production_env(WEB_ENV="staging"))
    assert "WEB_ENV" in str(err.value)


def test_production_plus_bypass_is_forbidden() -> None:
    with pytest.raises(ConfigError) as err:
        load_config(env=_production_env(WEB_DEV_BYPASS_AUTH="1"))
    assert "WEB_DEV_BYPASS_AUTH" in str(err.value)


# C03: with a valid production config, anonymous API access is rejected;
# the login endpoint itself stays reachable.
def test_anonymous_api_gets_401_and_login_stays_reachable() -> None:
    cfg = load_config(env=_production_env())
    client = TestClient(
        create_app(config=cfg, db=FakeDB()), base_url="https://panel.example", follow_redirects=False
    )
    guild = "170000000000000000"  # из allowlist: анониму положен 401, а не 404
    responses = [
        client.get(f"/api/guild/{guild}/audit"),
        client.patch(f"/api/guild/{guild}/settings", json={"logChannelId": "1"}),
        client.post(f"/api/guild/{guild}/trusted", json={"userId": "1"}),
        client.delete(f"/api/guild/{guild}/bot/invite/some-code"),
    ]
    for response in responses:
        assert response.status_code == 401, response.request.url
    login = client.get("/api/auth/login")
    assert login.status_code == 302
    assert login.headers["location"].startswith("https://discord.com/oauth2/authorize")


# D01: гильдия вне allowlist не существует для панели даже до проверки сессии.
def test_guild_outside_allowlist_is_404() -> None:
    cfg = load_config(env=_production_env())
    client = TestClient(
        create_app(config=cfg, db=FakeDB()), base_url="https://panel.example", follow_redirects=False
    )
    foreign = "170000000000000099"
    assert client.get(f"/api/guild/{foreign}/audit").status_code == 404
    assert client.get("/api/auth/whoami").status_code == 200  # own session unaffected


def test_production_app_cannot_be_constructed_with_auth_disabled() -> None:
    cfg = WebConfig(
        app_env="production",
        discord_client_id="cid",
        discord_client_secret="secret",
        web_session_secret="",  # would disable auth_enabled
        discord_token="t",
        web_public_url="https://panel.example",
    )
    with pytest.raises(ConfigError):
        create_app(config=cfg, db=FakeDB())


# C04: dev bypass works only in development/test and forces auth off even
# with complete secrets; image default (no WEB_ENV) stays production.
@pytest.mark.parametrize("mode", ["development", "test"])
def test_bypass_only_in_allowed_modes(mode: str) -> None:
    cfg = load_config(env=_production_env(WEB_ENV=mode, WEB_DEV_BYPASS_AUTH="1"))
    assert cfg.auth_enabled is False


def test_default_without_web_env_is_production() -> None:
    env = dict(GOOD)
    del env["WEB_ENV"]
    cfg = load_config(env=env)
    assert cfg.app_env == "production"
    assert cfg.auth_enabled is True


# C05: redirect URI must match the public origin; spoofed hosts cannot steer
# the OAuth callback; production http is limited to loopback.
def test_redirect_origin_mismatch_rejected() -> None:
    with pytest.raises(ConfigError) as err:
        load_config(env=_production_env(DISCORD_REDIRECT_URI="https://evil.example/api/auth/callback"))
    assert "DISCORD_REDIRECT_URI" in str(err.value)


def test_production_http_public_url_rejected_for_non_loopback() -> None:
    with pytest.raises(ConfigError):
        load_config(
            env=_production_env(
                WEB_PUBLIC_URL="http://panel.example",
                DISCORD_REDIRECT_URI="http://panel.example/api/auth/callback",
            )
        )


@pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000"])
def test_production_http_allowed_for_loopback(url: str) -> None:
    cfg = load_config(
        env=_production_env(WEB_PUBLIC_URL=url, DISCORD_REDIRECT_URI=url + "/api/auth/callback")
    )
    assert cfg.auth_enabled is True


def test_forwarded_host_cannot_spoof_oauth_callback() -> None:
    cfg = load_config(env=_production_env())
    client = TestClient(
        create_app(config=cfg, db=FakeDB()),
        base_url="https://evil-spoofed.example",
        follow_redirects=False,
    )
    login = client.get("/api/auth/login", headers={"Host": "evil-spoofed.example", "X-Forwarded-Host": "evil-spoofed.example"})
    location = login.headers["location"]
    redirect = re.search(r"[?&]redirect_uri=([^&]+)", location).group(1)
    from urllib.parse import unquote

    assert unquote(redirect) == "https://panel.example/api/auth/callback"
