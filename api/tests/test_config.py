from __future__ import annotations

from api.config import WebConfig, load_config


def test_defaults() -> None:
    cfg = load_config(env={})
    assert cfg.mongo_uri == "mongodb://localhost:27017"
    assert cfg.mongo_db == "voice_tracker"
    assert cfg.web_port == 8000
    assert cfg.auth_enabled is False


def test_auth_enabled_requires_all_parts() -> None:
    cfg = WebConfig(discord_client_id="a", discord_client_secret="b", web_session_secret="c")
    assert cfg.auth_enabled is True
    cfg.web_session_secret = "  "
    assert cfg.auth_enabled is False


def test_env_parsing_and_bad_port_falls_back() -> None:
    cfg = load_config(
        env={
            "MONGO_URI": "mongodb://host.docker.internal:27017",
            "MONGO_DB": "voice_tracker",
            "WEB_PORT": "not-a-number",
            "DISCORD_CLIENT_ID": " cid ",
            "DISCORD_CLIENT_SECRET": "secret",
            "WEB_SESSION_SECRET": "s" * 32,
            "WEB_PUBLIC_URL": "https://example.invalid",
        }
    )
    assert cfg.web_port == 8000
    assert cfg.discord_client_id == "cid"
    assert cfg.web_public_url == "https://example.invalid"
    assert cfg.auth_enabled is True


def test_blank_mongo_uri_falls_back_to_default() -> None:
    cfg = load_config(env={"MONGO_URI": "   ", "MONGO_DB": ""})
    assert cfg.mongo_uri == "mongodb://localhost:27017"
    assert cfg.mongo_db == "voice_tracker"
