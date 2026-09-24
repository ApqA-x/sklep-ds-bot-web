"""M01-M07, M09-M10 (T05): авторизованная выдача media вместо публичного StaticFiles.

Файл отдаётся только после доказательства связи метаданных чата этой гильдии
с каноническим путём внутри MEDIA_DIR. Знание sha256-пути доступа не даёт.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import auth as auth_module
from api import discord_api
from api.config import WebConfig
from api.main import create_app
from fakes import FakeDB

A = "170000000000000000"
B = "170000000000000001"
USER = "160000000000000001"
ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 48
GIF = b"GIF89a" + b"\x00" * 48
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'

ATT_PNG = "200000000000000001"
ATT_SVG = "200000000000000002"
ATT_B = "200000000000000003"
ATT_GONE = "200000000000000004"


def _rel(guild: str, blob: bytes, ext: str = ".png") -> str:
    digest = hashlib.sha256(blob).hexdigest()
    return f"{guild}/2026-09/{digest}{ext}"


def _media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    for rel, blob in (
        (_rel(A, PNG), PNG),
        (_rel(A, JPEG, ".jpg"), JPEG),
        (_rel(A, GIF, ".gif"), GIF),
        (_rel(A, SVG, ".svg"), SVG),
        (_rel(B, PNG), PNG),
        # ATT_GONE: метаданные есть, файла на диске нет — равномерный 404
    ):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob)
    return root


def _docs() -> list[dict]:
    return [
        {
            "guildId": A,
            "messageId": "100",
            "channelId": "500",
            "attachments": [
                {"id": ATT_PNG, "filename": "карт.png", "contentType": "image/png", "kind": "image",
                 "path": _rel(A, PNG), "stored": True},
                {"id": ATT_SVG, "filename": "evil.svg", "contentType": "image/svg+xml", "kind": "image",
                 "path": _rel(A, SVG, ".svg"), "stored": True},
                {"id": "200000000000000011", "filename": "anim.gif", "contentType": "image/gif", "kind": "image",
                 "path": _rel(A, GIF, ".gif"), "stored": True},
                {"id": ATT_GONE, "filename": "gone.png", "contentType": "image/png", "kind": "image",
                 "path": _rel(A, ATT_GONE.encode() + b"x" * 100, ".png"), "stored": True},
            ],
        },
        {
            "guildId": A,
            "messageId": "101",
            "deletedAt": "2026-09-20T00:00:00+00:00",
            "attachments": [
                {"id": "200000000000000010", "filename": "old.jpg", "contentType": "image/jpeg",
                 "kind": "image", "path": _rel(A, JPEG, ".jpg"), "stored": True},
            ],
        },
        {
            "guildId": B,
            "messageId": "200",
            "attachments": [
                {"id": ATT_B, "filename": "b.png", "contentType": "image/png", "kind": "image",
                 "path": _rel(B, PNG), "stored": True},
            ],
        },
    ]


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, bits: int = ADMINISTRATOR):
    async def fake_token(cfg, code, redirect_uri):
        return "t"

    async def fake_user_guilds(access_token):
        return {"id": USER, "username": "boss"}, []

    async def fake_resolve_permissions(cfg, guild_id, user_id):
        # владелец панели — админ только своей гильдии A; в B у него нет прав
        if guild_id == A:
            return ("allowed", bits)
        return ("denied", 0)

    monkeypatch.setattr(discord_api, "oauth_token", fake_token)
    monkeypatch.setattr(discord_api, "oauth_user_guilds", fake_user_guilds)
    monkeypatch.setattr(discord_api, "resolve_permissions", fake_resolve_permissions)

    db = FakeDB()
    db["chat_messages"].docs.extend(_docs())
    cfg = WebConfig(
        mongo_uri="",
        mongo_db="",
        discord_token="t",
        discord_client_id="cid",
        discord_client_secret="secret",
        discord_redirect_uri="https://example.invalid/api/auth/callback",
        web_session_secret="s" * 32,
        web_public_url="https://example.invalid",
        guild_allowlist=frozenset({A, B}),
        media_dir=str(_media_root(tmp_path)),
    )
    auth_module._clear_recheck_cache()
    return TestClient(
        create_app(config=cfg, db=db), base_url="https://example.invalid", follow_redirects=False
    )


def _login(client: TestClient) -> None:
    login = client.get("/api/auth/login")
    state = re.search(r"[&?]state=([^&]+)", login.headers["location"]).group(1)
    client.get(f"/api/auth/callback?code=abc&state={state}")


def _canonical(guild: str, att: str) -> str:
    return f"/api/guild/{guild}/media/attachment/{att}"


# M01: знание hash-пути не заменяет сессию — анониму файл не отдаём ни на одном маршруте.
def test_m01_anonymous_denied_even_with_known_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)
    assert client.get(_canonical(A, ATT_PNG)).status_code == 401
    assert client.get(f"/media/{_rel(A, PNG)}").status_code == 401


# M02: файл гильдии B под URL гильдии A не выдаётся и его существование не раскрывается.
def test_m02_cross_guild_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)
    _login(client)
    assert client.get(_canonical(A, ATT_B)).status_code == 404
    # прямой URL чужой гильдии: прав в B нет → закрыто, существование файла не подтверждается
    assert client.get(f"/media/{_rel(B, PNG)}").status_code in (403, 404)


# M03: admin своей гильдии получает сохранённое вложение (и старый, и новый URL).
def test_m03_owner_admin_gets_media(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)
    _login(client)
    fresh = client.get(_canonical(A, ATT_PNG))
    assert fresh.status_code == 200
    assert fresh.content == PNG
    assert fresh.headers["content-type"] == "image/png"
    assert fresh.headers["x-content-type-options"] == "nosniff"
    assert fresh.headers["cache-control"] == "private, no-store"
    legacy = client.get(f"/media/{_rel(A, PNG)}")
    assert legacy.status_code == 200 and legacy.content == PNG


# M04: Manage Guild без Administrator (D02) — закрыты оба маршрута, включая прямой media URL.
def test_m04_manager_only_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch, bits=MANAGE_GUILD)
    _login(client)
    assert client.get(_canonical(A, ATT_PNG)).status_code == 403
    assert client.get(f"/media/{_rel(A, PNG)}").status_code == 403


# M05: traversal/symlink/неканонический путь — за media root выйти нельзя.
def test_m05_traversal_and_symlink_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)
    _login(client)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOP SECRET")
    root = Path(client.app.state.config.media_dir)
    evil = root / A / "2026-09" / "evil.png"
    try:
        evil.symlink_to(secret)
    except OSError:
        pass  # Windows без dev mode: симлинк недоступен — остальные проверки всё равно идут
    else:
        # даже с подложенными в базу метаданными неформатный digest отвергается
        client.app.state.db["chat_messages"].docs.append(
            {"guildId": A, "messageId": "666",
             "attachments": [{"id": "9", "filename": "e.png", "path": f"{A}/2026-09/evil.png",
                              "stored": True, "kind": "image"}]}
        )
        assert client.get(f"/media/{A}/2026-09/evil.png").status_code == 404
        assert client.get(_canonical(A, "9")).status_code == 404
    assert client.get(f"/media/{A}/..%2F..%2Fsecret.txt").status_code in (404, 422)
    assert client.get(f"/media/{A}/2026-09/deadbeef.png").status_code == 404
    assert b"TOP SECRET" not in client.get(_canonical(A, ATT_PNG)).content


# M06: SVG под видом картинки не исполняется: octet-stream + attachment + nosniff.
def test_m06_svg_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)
    _login(client)
    response = client.get(_canonical(A, ATT_SVG))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"


# M07: валидные растры (включая старые сохранённые пути) работают inline.
def test_m07_rasters_inline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)
    _login(client)
    response = client.get(_canonical(A, ATT_PNG))
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    # jpeg лежит в старом (deletedAt) сообщении — и новый, и старый маршрут работают
    response = client.get("/media/" + _rel(A, JPEG, ".jpg"))
    assert response.status_code == 200 and response.content == JPEG
    response = client.get("/media/" + _rel(A, GIF, ".gif"))
    assert response.status_code == 200 and response.headers["content-type"] == "image/gif"


# M09: роль отозвана — новый запрос закрыт сразу после истечения свежести, private-кэш не спасает.
def test_m09_revoked_role_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)
    _login(client)
    assert client.get(_canonical(A, ATT_PNG)).status_code == 200

    async def denied(cfg, guild_id, user_id):
        return ("denied", 0)

    monkeypatch.setattr(discord_api, "resolve_permissions", denied)
    auth_module._clear_recheck_cache()  # имитируем протёкший TTL-бюджет свежести
    assert client.get(_canonical(A, ATT_PNG)).status_code == 403
    assert client.get(f"/media/{_rel(A, PNG)}").status_code == 403


# M10 (веб-часть): удалённое сообщение остаётся в архиве (D04) — media доступна.
def test_m10_deleted_message_media_retained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)
    _login(client)
    response = client.get("/media/" + _rel(A, JPEG, ".jpg"))
    assert response.status_code == 200
    doc = next(d for d in client.app.state.db["chat_messages"].docs if d["messageId"] == "101")
    assert doc["deletedAt"] is not None


# файла нет на диске — одинаковый 404 без деталей (приватное существование не раскрываем).
def test_missing_file_uniform_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)
    _login(client)
    gone = client.get(_canonical(A, ATT_GONE))
    assert gone.status_code == 404
    assert "detail" in gone.json()


# MEDIA_DIR не настроен — 503 (хранилища нет), но приватных данных не отдаём.
def test_media_unconfigured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app(tmp_path, monkeypatch)
    client.app.state.config.media_dir = ""
    _login(client)
    assert client.get(_canonical(A, ATT_PNG)).status_code == 503
