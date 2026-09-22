from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api import auth as auth_module
from api import discord_api
from api import queries
from api.config import WebConfig
from api.main import create_app
from api.mutations import COLL_AUDIT, COLL_STALKER
from fakes import FakeCollection, FakeDB

GUILD = "170000000000000000"
USER = "160000000000000001"
ADMIN = 1 << 3
MANAGE = 1 << 5


def _settings_db(**fields) -> FakeDB:
    db = FakeDB()
    doc = {"_id": GUILD, "createdAt": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    doc.update(fields)
    db[queries.COLL_GUILD_SETTINGS] = FakeCollection(queries.COLL_GUILD_SETTINGS, docs=[doc])
    return db


def _dev_client(db: FakeDB) -> TestClient:
    cfg = WebConfig(mongo_uri="", mongo_db="")
    return TestClient(create_app(config=cfg, db=db))


def _update_calls(db: FakeDB, coll: str):
    return [c for c in db[coll].calls if c[0] in ("update_one", "insert_one", "delete_one")]


def test_patch_settings_updates_and_audits() -> None:
    db = _settings_db(trustedUserIds=[USER], updatedAt=datetime(2026, 9, 1, tzinfo=timezone.utc))
    client = _dev_client(db)
    response = client.patch(
        f"/api/guild/{GUILD}/settings",
        json={"summaryChannelId": USER, "trackingMode": "specific", "trackedChannelIds": [USER]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["summaryChannelId"] == USER
    assert body["trackingMode"] == "specific"
    call = _update_calls(db, queries.COLL_GUILD_SETTINGS)[-1]
    assert call[0] == "update_one"
    assert set(call[3]["$set"]) == {"summaryChannelId", "trackingMode", "trackedChannelIds", "updatedAt"}
    audit = db[COLL_AUDIT].docs[0]
    assert audit["action"] == "settings.patch"
    assert audit["actorName"] == "dev"
    assert audit["before"]["trackingMode"] is None
    assert audit["after"]["summaryChannelId"] == USER


def test_patch_forbids_non_allowlisted_fields() -> None:
    db = _settings_db()
    client = _dev_client(db)
    response = client.patch(f"/api/guild/{GUILD}/settings", json={"managedVoiceChannelId": "123"})
    assert response.status_code == 422


def test_patch_rejects_invalid_values() -> None:
    client = _dev_client(_settings_db())
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"trackingMode": "weird"}).status_code == 422
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"trustedUserIds": ["not-a-snowflake"]}).status_code == 422
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"activityEventTypes": ["nope"]}).status_code == 422
    assert client.patch(f"/api/guild/{GUILD}/settings", json={}).status_code == 422


def test_patch_conflict_on_stale_updated_at() -> None:
    db = _settings_db(updatedAt=datetime(2026, 9, 10, 12, tzinfo=timezone.utc))
    client = _dev_client(db)
    response = client.patch(
        f"/api/guild/{GUILD}/settings",
        json={"summaryChannelId": USER, "expectedUpdatedAt": "2026-09-01T00:00:00Z"},
    )
    assert response.status_code == 409
    assert len(_update_calls(db, queries.COLL_GUILD_SETTINGS)) == 0


def test_patch_creates_missing_settings_doc() -> None:
    db = FakeDB()
    client = _dev_client(db)
    response = client.patch(f"/api/guild/{GUILD}/settings", json={"trackingMode": "none"})
    assert response.status_code == 200
    assert response.json()["trackingMode"] == "none"
    assert response.json()["guildId"] == GUILD


def test_trusted_add_remove_atomic() -> None:
    db = _settings_db(trustedUserIds=[])
    client = _dev_client(db)
    add = client.post(f"/api/guild/{GUILD}/trusted", json={"userId": USER, "action": "add"})
    assert add.status_code == 200
    assert add.json()["trustedUserIds"] == [USER]
    remove = client.post(f"/api/guild/{GUILD}/trusted", json={"userId": USER, "action": "remove"})
    assert remove.status_code == 200
    assert remove.json()["trustedUserIds"] == []
    actions = [doc["action"] for doc in db[COLL_AUDIT].docs]
    assert actions == ["trustedUserIds.add", "trustedUserIds.remove"]
    # удаление последнего элемента не должно «восстанавливать» старый список в after
    assert db[COLL_AUDIT].docs[1]["after"]["ids"] == []


def test_auto_unmute_endpoint() -> None:
    db = _settings_db(autoUnmuteUserIds=[])
    client = _dev_client(db)
    response = client.post(f"/api/guild/{GUILD}/autoUnmute", json={"userId": USER, "action": "add"})
    assert response.status_code == 200
    assert response.json()["autoUnmuteUserIds"] == [USER]


def test_stalker_add_and_remove() -> None:
    db = _settings_db()
    client = _dev_client(db)
    watcher = "160000000000000002"
    add = client.post(
        f"/api/guild/{GUILD}/stalker",
        json={"watcherUserId": watcher, "targetUserId": USER, "action": "add"},
    )
    assert add.status_code == 200
    sub_id = f"{GUILD}:{watcher}:{USER}"
    assert add.json()["subscriptionId"] == sub_id
    assert db[COLL_STALKER].docs[0]["_id"] == sub_id
    listing = client.get(f"/api/guild/{GUILD}/stalker")
    assert listing.status_code == 200
    assert listing.json()["items"][0]["watcherUserId"] == watcher
    remove = client.post(
        f"/api/guild/{GUILD}/stalker",
        json={"watcherUserId": watcher, "targetUserId": USER, "action": "remove"},
    )
    assert remove.status_code == 200
    assert db[COLL_STALKER].docs == []


def test_audit_page_lists_recent_first() -> None:
    db = _settings_db()
    client = _dev_client(db)
    client.patch(f"/api/guild/{GUILD}/settings", json={"trackingMode": "none"})
    client.post(f"/api/guild/{GUILD}/trusted", json={"userId": USER, "action": "add"})
    response = client.get(f"/api/guild/{GUILD}/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["action"] for item in body["items"]] == ["trustedUserIds.add", "settings.patch"]


def test_write_endpoints_require_login_when_auth_enabled() -> None:
    cfg = WebConfig(
        mongo_uri="",
        mongo_db="",
        discord_client_id="cid",
        discord_client_secret="secret",
        discord_redirect_uri="https://example.invalid/api/auth/callback",
        web_session_secret="s" * 32,
        web_public_url="https://example.invalid",
    )
    db = _settings_db()
    client = TestClient(create_app(config=cfg, db=db), base_url="https://testserver", follow_redirects=False)
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"trackingMode": "none"}).status_code == 401
    assert client.get(f"/api/guild/{GUILD}/audit").status_code == 401


@pytest.fixture()
def logged_in_manager(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"guilds": []}

    async def fake_token(cfg, code, redirect_uri):
        return "t"

    async def fake_user_guilds(access_token):
        return {"id": USER, "username": "boss"}, state["guilds"]

    async def fake_member_permissions(cfg, guild_id, user_id):
        return None

    monkeypatch.setattr(discord_api, "oauth_token", fake_token)
    monkeypatch.setattr(discord_api, "oauth_user_guilds", fake_user_guilds)
    monkeypatch.setattr(discord_api, "member_permissions", fake_member_permissions)
    return state


def _login(client: TestClient, state: dict, perms: dict) -> None:
    state["guilds"] = [{"id": gid, "permissions": str(bits)} for gid, bits in perms.items()]
    login_response = client.get("/api/auth/login")
    import re

    oauth_state = re.search(r"[&?]state=([^&]+)", login_response.headers["location"]).group(1)
    client.get(f"/api/auth/callback?code=abc&state={oauth_state}")


def test_manager_can_read_but_not_write(logged_in_manager: dict) -> None:
    auth_module._clear_recheck_cache()
    cfg = WebConfig(
        mongo_uri="",
        mongo_db="",
        discord_client_id="cid",
        discord_client_secret="secret",
        discord_redirect_uri="https://example.invalid/api/auth/callback",
        web_session_secret="s" * 32,
        web_public_url="https://example.invalid",
    )
    client = TestClient(create_app(config=cfg, db=_settings_db()), base_url="https://testserver", follow_redirects=False)
    _login(client, logged_in_manager, {GUILD: MANAGE})
    assert client.get(f"/api/guild/{GUILD}/leaderboard").status_code == 200
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"trackingMode": "none"}).status_code == 403
    assert client.post(f"/api/guild/{GUILD}/trusted", json={"userId": USER, "action": "add"}).status_code == 403
