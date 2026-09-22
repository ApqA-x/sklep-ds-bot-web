from __future__ import annotations

from dataclasses import dataclass, field
from os import environ
from typing import Any


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _get(source: Any, key: str, fallback: str = "") -> str:
    return _clean(source.get(key, "")) or fallback


@dataclass(slots=True)
class WebConfig:
    web_port: int = 8000
    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "voice_tracker"
    discord_token: str = ""
    discord_application_id: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""
    discord_redirect_uri: str = ""
    web_session_secret: str = ""
    web_public_url: str = ""
    log_level: str = "INFO"
    warnings: list[str] = field(default_factory=list)

    @property
    def auth_enabled(self) -> bool:
        return bool(
            self.discord_client_id.strip()
            and self.discord_client_secret.strip()
            and self.web_session_secret.strip()
        )


def load_config(env: Any = None) -> WebConfig:
    source = environ if env is None else env
    cfg = WebConfig(
        mongo_uri=_get(source, "MONGO_URI", "mongodb://localhost:27017"),
        mongo_db=_get(source, "MONGO_DB", "voice_tracker"),
        discord_token=_get(source, "DISCORD_TOKEN"),
        discord_application_id=_get(source, "DISCORD_APPLICATION_ID"),
        discord_client_id=_get(source, "DISCORD_CLIENT_ID"),
        discord_client_secret=_get(source, "DISCORD_CLIENT_SECRET"),
        discord_redirect_uri=_get(source, "DISCORD_REDIRECT_URI"),
        web_session_secret=_get(source, "WEB_SESSION_SECRET"),
        web_public_url=_get(source, "WEB_PUBLIC_URL"),
        log_level=_get(source, "LOG_LEVEL", "INFO").upper(),
    )
    try:
        cfg.web_port = int(_get(source, "WEB_PORT", "8000"))
    except ValueError:
        cfg.web_port = 8000
    return cfg
