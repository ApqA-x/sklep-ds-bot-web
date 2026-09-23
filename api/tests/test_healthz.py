from __future__ import annotations

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
    class _Admin:
        def command(self, name):
            raise RuntimeError("no server")

    admin = _Admin()

    def __getitem__(self, name):
        return {"name": name}


def _client(mongo, **overrides) -> TestClient:
    cfg = WebConfig(**overrides)
    app = create_app(config=cfg, mongo_client=mongo)
    return TestClient(app)


def test_healthz_ok_when_mongo_up() -> None:
    client = _client(FakePingableMongo())
    response = client.get("/api/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mongo"]["ok"] is True
    assert body["auth_enabled"] is False


def test_healthz_degraded_when_mongo_down() -> None:
    client = _client(FailingMongo())
    response = client.get("/api/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["mongo"]["ok"] is False


def test_healthz_without_mongo_client() -> None:
    client = _client(None, mongo_uri="")
    response = client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json()["mongo"]["ok"] is False


def test_unknown_api_route_is_json_404() -> None:
    client = _client(FakePingableMongo())
    response = client.get("/api/nonexistent")
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


def test_spa_catchall_does_not_shadow_api() -> None:
    client = _client(FakePingableMongo())
    response = client.get("/")
    # without a built ui the catch-all reports 503 instead of leaking errors
    assert response.status_code in {200, 503}


def test_spa_cache_headers(tmp_path, monkeypatch) -> None:
    dist = (tmp_path / "dist").resolve()
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "index-abc123.js").write_text("js")
    (dist / "index.html").write_text("<html>")
    monkeypatch.setattr("api.main.UI_DIST", dist)
    client = _client(FakePingableMongo())
    asset = client.get("/assets/index-abc123.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
    page = client.get("/any/client/route")
    assert page.status_code == 200
    assert "no-store" in page.headers["cache-control"]
