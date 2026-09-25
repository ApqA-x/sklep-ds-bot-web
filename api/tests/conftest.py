"""Tests run against an explicit development-mode configuration.

The image/production entry point defaults to WEB_ENV=production (fail-closed);
without this file the module-level `app = create_app()` in api.main would run
production validation against whatever environment variables the test host has.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("WEB_ENV", "development")


@pytest.fixture(autouse=True)
def _fresh_rate_buckets():
    """T16: бакеты живут в модуле процесса — без сброса тесты делили бы
    лимиты друг с друга и становились порядково-зависимыми (429-«призраки»)."""
    from api import limits

    limits.reset_all()
    yield
    limits.reset_all()
