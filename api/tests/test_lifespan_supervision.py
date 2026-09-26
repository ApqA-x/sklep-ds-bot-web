"""T12/R05: lifespan ставит фоновые циклы под надзор и корректно их дренирует."""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from api.config import WebConfig
from api.main import create_app

from fakes import FakeDB


class FakePingableMongo:
    def __init__(self) -> None:
        closed = []
        self.closed = closed

        class _Admin:
            def command(self, name):
                return {"ok": 1}

        self.admin = _Admin()

    def close(self) -> None:
        self.closed.append(True)

    def __getitem__(self, name):
        return {"name": name}


def test_lifespan_spawns_supervised_tasks_and_drains_on_shutdown() -> None:
    mongo = FakePingableMongo()
    app = create_app(config=WebConfig(), mongo_client=mongo, db=FakeDB())
    with TestClient(app) as client:
        assert client.get("/api/readyz").status_code == 200
        names = {t["name"] for t in app.state.supervisor.snapshot()}
        assert "web-loop-monitor" in names
        assert "web-schema-recheck" in names
        # без DISCORD_TOKEN фоновый аудит не заводим
        assert "discord-audit-sync" not in names
        running = [t._task for t in app.state.supervisor._handles.values()]
        assert all(not t.done() for t in running)
    # после выхода из lifespan: все надзираемые задачи свёрнуты (cancel+await),
    # Mongo-клиент закрыт
    assert all(t.done() for t in running)
    assert mongo.closed == [True]


def test_lifespan_audit_loop_spawned_when_token_present(monkeypatch) -> None:
    # без реального Discord: подменяем run_loop на «вечный» цикл с beat
    from api import audit_sync

    async def fake_run_loop(app, on_cycle=None):
        if on_cycle is not None:
            on_cycle()
        await asyncio.Event().wait()

    monkeypatch.setattr(audit_sync, "run_loop", fake_run_loop)
    app = create_app(config=WebConfig(discord_token="x"), mongo_client=FakePingableMongo(), db=FakeDB())
    with TestClient(app) as client:
        body = client.get("/api/readyz").json()
        names = {t["name"] for t in app.state.supervisor.snapshot()}
        assert "discord-audit-sync" in names
        assert body["status"] == "ready"
