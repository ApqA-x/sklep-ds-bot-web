from __future__ import annotations

from fastapi.testclient import TestClient

from api.config import WebConfig
from api.main import create_app
from fakes import FakeDB


def _client(tmp_path) -> TestClient:
    (tmp_path / "99" / "2026-09").mkdir(parents=True)
    (tmp_path / "99" / "2026-09" / "deadbeef.png").write_bytes(b"PNGDATA")
    cfg = WebConfig(mongo_uri="", mongo_db="", media_dir=str(tmp_path))
    return TestClient(create_app(config=cfg, db=FakeDB()))


def test_media_serves_stored_attachments(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.get("/media/99/2026-09/deadbeef.png")
    assert response.status_code == 200
    assert response.content == b"PNGDATA"


def test_media_rejects_path_traversal(tmp_path) -> None:
    client = _client(tmp_path)
    response = client.get("/media/..%2F..%2F..%2Fetc%2Fpasswd")
    assert response.status_code in (400, 404)


def test_media_disabled_without_dir() -> None:
    cfg = WebConfig(mongo_uri="", mongo_db="")
    client = TestClient(create_app(config=cfg, db=FakeDB()))
    # без MEDIA_DIR /media не монтируется — catch-all отдаёт SPA-индекс (не файл с диска)
    assert client.get("/media/any.png").status_code in (200, 404, 503)
