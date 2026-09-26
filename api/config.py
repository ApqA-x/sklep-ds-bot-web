from __future__ import annotations

from dataclasses import dataclass, field
from os import environ
from typing import Any

PRODUCTION = "production"
DEVELOPMENT = "development"
TEST = "test"
APP_ENVS = (PRODUCTION, DEVELOPMENT, TEST)

_TRUTHY = {"1", "true", "yes", "on"}
_MIN_SESSION_SECRET = 32


class ConfigError(RuntimeError):
    """Unusable configuration. Names keys only — never their values."""


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _get(source: Any, key: str, fallback: str = "") -> str:
    return _clean(source.get(key, "")) or fallback


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


@dataclass(slots=True)
class WebConfig:
    # Direct construction defaults to development (offline tests/local runs).
    # The env-driven startup path (load_config) defaults to production instead.
    app_env: str = DEVELOPMENT
    dev_bypass_auth: bool = False
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
    media_dir: str = ""
    log_level: str = "INFO"
    warnings: list[str] = field(default_factory=list)

    @property
    def is_production(self) -> bool:
        return self.app_env == PRODUCTION

    @property
    def secrets_complete(self) -> bool:
        return bool(
            self.discord_client_id.strip()
            and self.discord_client_secret.strip()
            and self.web_session_secret.strip()
        )

    @property
    def auth_enabled(self) -> bool:
        return self.secrets_complete and not self.dev_bypass_auth


def validate_config(cfg: WebConfig) -> None:
    """Fail fast on unusable configuration. Raises ConfigError with key names only."""
    if cfg.app_env not in APP_ENVS:
        raise ConfigError(f"WEB_ENV must be one of: {', '.join(APP_ENVS)}")

    if cfg.is_production and cfg.dev_bypass_auth:
        raise ConfigError(
            "WEB_DEV_BYPASS_AUTH is forbidden when WEB_ENV=production"
        )

    redirect_issue = _origin_mismatch(cfg)
    if redirect_issue is not None:
        raise ConfigError(redirect_issue)

    if not cfg.is_production:
        return

    missing: list[str] = []
    if not cfg.discord_client_id:
        missing.append("DISCORD_CLIENT_ID")
    if not cfg.discord_client_secret:
        missing.append("DISCORD_CLIENT_SECRET")
    if not cfg.web_session_secret:
        missing.append("WEB_SESSION_SECRET")
    elif len(cfg.web_session_secret) < _MIN_SESSION_SECRET:
        raise ConfigError(
            f"WEB_SESSION_SECRET must be at least {_MIN_SESSION_SECRET} characters"
        )
    if not cfg.discord_token:
        missing.append("DISCORD_TOKEN")
    if not cfg.web_public_url and not cfg.discord_redirect_uri:
        missing.append("WEB_PUBLIC_URL (or DISCORD_REDIRECT_URI)")
    if missing:
        raise ConfigError("WEB_ENV=production requires: " + ", ".join(missing))

    if cfg.web_public_url:
        _check_public_url(cfg.web_public_url)


def _origin_mismatch(cfg: WebConfig) -> str | None:
    if not (cfg.web_public_url and cfg.discord_redirect_uri):
        return None
    from urllib.parse import urlsplit

    public = urlsplit(cfg.web_public_url)
    redirect = urlsplit(cfg.discord_redirect_uri)
    if (public.scheme, public.netloc) != (redirect.scheme, redirect.netloc):
        return (
            "DISCORD_REDIRECT_URI must share scheme and host with WEB_PUBLIC_URL "
            "(WEB_PUBLIC_URL="
            + f"{public.scheme}://{public.netloc}, DISCORD_REDIRECT_URI="
            + f"{redirect.scheme}://{redirect.netloc})"
        )
    return None


def _check_public_url(url: str) -> None:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    if parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1"}:
        return
    raise ConfigError(
        "WEB_PUBLIC_URL must be https (http is only allowed for localhost/127.0.0.1)"
    )


def load_config(env: Any = None, *, validate: bool = True) -> WebConfig:
    source = environ if env is None else env
    raw_env = _get(source, "WEB_ENV") or PRODUCTION
    app_env = raw_env.lower()
    if app_env not in APP_ENVS:
        raise ConfigError(f"WEB_ENV must be one of: {', '.join(APP_ENVS)}")
    cfg = WebConfig(
        app_env=app_env,
        dev_bypass_auth=_truthy(_get(source, "WEB_DEV_BYPASS_AUTH")),
        mongo_uri=_get(source, "MONGO_URI", "mongodb://localhost:27017"),
        mongo_db=_get(source, "MONGO_DB", "voice_tracker"),
        discord_token=_get(source, "DISCORD_TOKEN"),
        discord_application_id=_get(source, "DISCORD_APPLICATION_ID"),
        discord_client_id=_get(source, "DISCORD_CLIENT_ID"),
        discord_client_secret=_get(source, "DISCORD_CLIENT_SECRET"),
        discord_redirect_uri=_get(source, "DISCORD_REDIRECT_URI"),
        web_session_secret=_get(source, "WEB_SESSION_SECRET"),
        web_public_url=_get(source, "WEB_PUBLIC_URL"),
        media_dir=_get(source, "MEDIA_DIR"),
        log_level=_get(source, "LOG_LEVEL", "INFO").upper(),
    )
    try:
        cfg.web_port = int(_get(source, "WEB_PORT", "8000"))
    except ValueError:
        cfg.web_port = 8000
    if validate:
        validate_config(cfg)
    return cfg
