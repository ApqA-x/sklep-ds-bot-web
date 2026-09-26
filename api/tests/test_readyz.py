"""T12: контракт readiness — 503 при недоступной зависимости, честный recovery.

R01: Mongo недоступна → /api/readyz 503 в пределах timeout; /api/healthz
(liveness) при этом остаётся 200 — отвал зависимости ≠ смерть процесса.
R06: в payload наружу не утекают сообщения исключений (в них бывают URI с
credentials).
R07: возврат зависимости → readiness восстанавливается сам, без ручных действий.
"""
from __future__ import annotations

import types

from fastapi.testclient import TestClient

from api.config import WebConfig
from api.main import create_app


class FakePingableMongo:
    def __init__(self) -> None:
        class _Admin:
            def command(self, name):
                assert name == "ping"
                return {"ok": 1}

        self.admin = _Admin()

    def __getitem__(self, name):
        return {"name": name}


class FailingMongo:
    """Ошибка с «секретным» содержимым — наружу должно уйти только имя типа."""

    class _Admin:
        def command(self, name):
            raise RuntimeError("cannot connect to mongodb://user:pass@secret-host:27017/db")

    admin = _Admin()

    def __getitem__(self, name):
        return {"name": name}


def _client(mongo, **overrides) -> TestClient:
    cfg = WebConfig(**overrides)
    app = create_app(config=cfg, mongo_client=mongo)
    return TestClient(app)


def test_readyz_ok_when_mongo_up() -> None:
    client = _client(FakePingableMongo())
    response = client.get("/api/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["mongo"]["ok"] is True
    assert body["unhealthyTasks"] == []


def test_readyz_503_when_mongo_down_without_leaking_error_text() -> None:
    client = _client(FailingMongo())
    response = client.get("/api/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert "mongo" in body["checks"]
    text = response.text
    assert "user:pass" not in text
    assert "secret-host" not in text
    assert "mongodb://" not in text
    # только имя типа исключения
    assert body["checks"]["mongo"]["error"] == "RuntimeError"


def test_healthz_stays_liveness_200_when_mongo_down() -> None:
    client = _client(FailingMongo())
    response = client.get("/api/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "liveness"
    assert body["status"] == "ok"
    assert body["mongo"]["ok"] is False


def test_readyz_recovers_when_dependency_returns() -> None:
    client = _client(FailingMongo())
    assert client.get("/api/readyz").status_code == 503
    # зависимость вернулась — следующий же запрос readiness зелёный (R07)
    client.app.state.mongo = FakePingableMongo()
    response = client.get("/api/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"


def test_readyz_media_mount_checked_without_leaking_path(tmp_path) -> None:
    missing = str(tmp_path / "no-such-mount")
    client = _client(FakePingableMongo(), media_dir=missing)
    response = client.get("/api/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["media"]["ok"] is False
    assert missing not in response.text

    present = tmp_path / "media"
    present.mkdir()
    client2 = _client(FakePingableMongo(), media_dir=str(present))
    assert client2.get("/api/readyz").status_code == 200


def test_readyz_schema_missing_blocks() -> None:
    client = _client(FakePingableMongo())
    client.app.state.schema_report = {"ok": False, "missing": ["chat_messages_guild_sent"]}
    response = client.get("/api/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["schema"]["ok"] is False


def test_readyz_drops_when_critical_task_is_dead() -> None:
    client = _client(FakePingableMongo())
    client.app.state.supervisor = types.SimpleNamespace(
        unhealthy_tasks=lambda: ["discord-audit-sync"],
        snapshot=list,
    )
    response = client.get("/api/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["unhealthyTasks"] == ["discord-audit-sync"]
    assert body["status"] == "not_ready"


def test_readyz_includes_loop_lag_snapshot() -> None:
    client = _client(FakePingableMongo())
    body = client.get("/api/readyz").json()
    assert "lagSeconds" in body["loop"]
    health = client.get("/api/healthz").json()
    assert "lagSeconds" in health["loop"]
