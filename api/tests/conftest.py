"""Tests run against an explicit development-mode configuration.

The image/production entry point defaults to WEB_ENV=production (fail-closed);
without this file the module-level `app = create_app()` in api.main would run
production validation against whatever environment variables the test host has.
"""
from __future__ import annotations

import os

os.environ.setdefault("WEB_ENV", "development")
