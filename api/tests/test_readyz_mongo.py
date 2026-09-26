"""T12 R01/R07 на реальном pymongo: bounded-ожидание и восстановление readiness.

Unit-слой подменяет клиент фейком; здесь проверяются настоящие свойства драйвера:
- недоступная Mongo → /api/readyz 503 в пределах serverSelectionTimeout (не
  навсегда висящий запрос), при этом liveness остаётся 200;
- возврат зависимости → readiness восстанавливается сам, без ручных действий.
"""
from __future__ import annotations

import os
import time

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.config import WebConfig
from api.main import create_app
from stand_guard import guard_mongo_uri  # noqa: E402

pytestmark = pytest.mark.integration

URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")
# «упавшая» Mongo без обхода stand_guard: тот же разрешённый host:port, но
# вымышленный replicaSet — server selection честно не проходит и упирается
# в serverSelectionTimeoutMS (проверяем именно bounded-ожидание драйвера)
DEAD_URI = f"{URI}/?replicaSet=t12-no-such-rs"


def _server_up() -> bool:
    try:
        client = MongoClient(URI, serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


def _client_with(mongo) -> TestClient:
    cfg = WebConfig(mongo_uri="")  # клиент передаём явно — стендовый URI не хардкодим в конфиг
    return TestClient(create_app(config=cfg, mongo_client=mongo))


def test_readyz_503_bounded_when_mongo_down_and_recovers() -> None:
    guard_mongo_uri(URI)
    guard_mongo_uri(DEAD_URI)
    if not _server_up():
        pytest.skip(f"test mongo not reachable at {URI}")

    dead = MongoClient(DEAD_URI, serverSelectionTimeoutMS=1000, connect=False)
    client = _client_with(dead)
    started = time.monotonic()
    response = client.get("/api/readyz")
    elapsed = time.monotonic() - started
    assert response.status_code == 503
    assert elapsed < 5.0, "readiness обязан отвечать в пределах ограниченного timeout"
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["mongo"]["ok"] is False
    # liveness не зависит от отвала БД (R01)
    assert client.get("/api/healthz").status_code == 200

    # R07: «возврат» зависимости — новый рабочий клиент вместо мёртвого
    live = MongoClient(URI, serverSelectionTimeoutMS=2000, connect=False)
    client.app.state.mongo = live
    started = time.monotonic()
    response = client.get("/api/readyz")
    assert response.status_code == 200
    assert time.monotonic() - started < 5.0
    assert response.json()["status"] == "ready"
    live.close()
    dead.close()
