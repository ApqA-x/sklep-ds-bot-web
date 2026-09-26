from __future__ import annotations

import pytest

from api.config import ConfigError, WebConfig, load_config


def test_defaults_are_production_and_fail_closed() -> None:
    with pytest.raises(ConfigError) as err:
        load_config(env={})
    message = str(err.value)
    assert "DISCORD_CLIENT_ID" in message
    assert "DISCORD_CLIENT_SECRET" in message
    assert "WEB_SESSION_SECRET" in message
    assert "DISCORD_TOKEN" in message
    assert "WEB_PUBLIC_URL" in message


def test_dev_mode_defaults_stay_usable_without_secrets() -> None:
    cfg = load_config(env={"WEB_ENV": "development"})
    assert cfg.app_env == "development"
    assert cfg.auth_enabled is False


def test_auth_enabled_requires_all_parts() -> None:
    cfg = WebConfig(discord_client_id="a", discord_client_secret="b", web_session_secret="c")
    assert cfg.auth_enabled is True
    cfg.web_session_secret = "  "
    assert cfg.auth_enabled is False
    cfg.web_session_secret = "c"
    cfg.dev_bypass_auth = True
    assert cfg.auth_enabled is False


def test_env_parsing_and_bad_port_falls_back() -> None:
    cfg = load_config(
        env={
            "WEB_ENV": "production",
            "MONGO_URI": "mongodb://host.docker.internal:27017",
            "MONGO_DB": "voice_tracker",
            "WEB_PORT": "not-a-number",
            "DISCORD_TOKEN": "t",
            "DISCORD_CLIENT_ID": " cid ",
            "DISCORD_CLIENT_SECRET": "secret",
            "WEB_SESSION_SECRET": "s" * 32,
            "WEB_PUBLIC_URL": "https://example.invalid",
            "WEB_GUILD_ALLOWLIST": "170000000000000000, 170000000000000001",
        }
    )
    assert cfg.web_port == 8000
    assert cfg.discord_client_id == "cid"
    assert cfg.web_public_url == "https://example.invalid"
    assert cfg.auth_enabled is True
    assert cfg.guild_allowlist == frozenset({"170000000000000000", "170000000000000001"})


def test_blank_mongo_uri_falls_back_to_default() -> None:
    cfg = load_config(env={"WEB_ENV": "development", "MONGO_URI": "   ", "MONGO_DB": ""})
    assert cfg.mongo_uri == "mongodb://localhost:27017"
    assert cfg.mongo_db == "voice_tracker"


def test_error_never_contains_secret_values() -> None:
    with pytest.raises(ConfigError) as err:
        load_config(env={"WEB_ENV": "production", "DISCORD_CLIENT_SECRET": "sup3r-s3cret"})
    assert "sup3r-s3cret" not in str(err.value)
