from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from api import queries
from api.config import WebConfig
from api.main import create_app
from fakes import FakeCollection, FakeDB

import pytest

GUILD = "170000000000000000"
USER = "160000000000000000"


def _dt(day: int) -> datetime:
    return datetime(2026, 9, day, tzinfo=timezone.utc)


def _client(db: FakeDB | None) -> TestClient:
    cfg = WebConfig(mongo_uri="", mongo_db="")
    app = create_app(config=cfg, db=db)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    queries._leaderboard_cache.clear()
    queries._invites_cache.clear()
    queries._names_cache.clear()
    from api import discord_api

    discord_api._BOT_GUILD_CACHE.clear()
    discord_api._GUILD_RESOURCE_CACHE.clear()


def test_leaderboard_endpoint() -> None:
    db = FakeDB()
    db[queries.COLL_PARTICIPANTS] = FakeCollection(
        queries.COLL_PARTICIPANTS,
        aggregate_results=[[{"_id": "5", "userName": "U", "totalMs": 42, "appearances": 1}]],
    )
    client = _client(db)
    response = client.get(f"/api/guild/{GUILD}/leaderboard", params={"period": "7d", "limit": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["guildId"] == GUILD
    assert body["items"] == [{"userId": "5", "userName": "U", "totalMs": 42, "appearances": 1}]


def test_leaderboard_validation() -> None:
    client = _client(FakeDB())
    assert client.get("/api/guild/abc/leaderboard").status_code == 422
    assert client.get(f"/api/guild/{GUILD}/leaderboard", params={"period": "5d"}).status_code == 422
    assert client.get(f"/api/guild/{GUILD}/leaderboard", params={"limit": 0}).status_code == 422
    assert client.get(f"/api/guild/{GUILD}/leaderboard", params={"limit": 101}).status_code == 422


def test_sessions_active_endpoint() -> None:
    db = FakeDB()
    db[queries.COLL_SESSIONS] = FakeCollection(queries.COLL_SESSIONS, docs=[
        {"_id": "s1", "guildId": GUILD, "channelId": "c1", "status": "active", "startedAt": _dt(9)},
    ])
    client = _client(db)
    response = client.get(f"/api/guild/{GUILD}/sessions/active")
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["id"] == "s1"
    assert body["items"][0]["participants"] == []


def test_sessions_history_and_status_param() -> None:
    db = FakeDB()
    client = _client(db)
    response = client.get(f"/api/guild/{GUILD}/sessions", params={"status": "weird"})
    assert response.status_code == 422
    response = client.get(f"/api/guild/{GUILD}/sessions")
    assert response.status_code == 200
    assert response.json()["items"] == []
    response = client.get(f"/api/guild/{GUILD}/sessions", params={"status": "active"})
    assert response.json()["items"] == []


def test_session_detail_endpoint() -> None:
    db = FakeDB()
    session_uuid = "6f1e2b3c-4d5e-6f70-8192-a3b4c5d6e7f8"
    db[queries.COLL_SESSIONS] = FakeCollection(queries.COLL_SESSIONS, docs=[
        {"_id": session_uuid, "guildId": GUILD, "channelId": "c", "status": "closed",
         "startedAt": _dt(8), "endedAt": _dt(9), "endedByUserId": "1", "summaryMessage": "sum"},
    ])
    db[queries.COLL_PARTICIPANTS] = FakeCollection(queries.COLL_PARTICIPANTS, docs=[
        {"sessionId": session_uuid, "userId": "5", "userName": "U", "joinedAt": _dt(8),
         "leftAt": _dt(9), "durationMs": 10, "active": False},
    ])
    client = _client(db)
    response = client.get(f"/api/guild/{GUILD}/sessions/{session_uuid}")
    assert response.status_code == 200
    body = response.json()
    assert body["summaryMessage"] == "sum"
    assert body["participants"][0]["userId"] == "5"

    assert client.get(f"/api/guild/{GUILD}/sessions/not-a-uuid").status_code == 422
    assert client.get(f"/api/guild/{GUILD}/sessions/00000000-0000-0000-0000-000000000000").status_code == 404


def test_user_profile_endpoint_404_and_ok() -> None:
    db = FakeDB()
    client = _client(db)
    user = "160000000000000000"
    assert client.get(f"/api/guild/{GUILD}/users/{user}").status_code == 404

    db[queries.COLL_PARTICIPANTS] = FakeCollection(queries.COLL_PARTICIPANTS,
        docs=[{"guildId": GUILD, "userId": user, "userName": "U", "joinedAt": _dt(9)}],
        aggregate_results=[
            [{"_id": None, "totalMs": 7, "appearances": 1}],
            [{"_id": "2026-09-09", "ms": 7}],
        ])
    response = client.get(f"/api/guild/{GUILD}/users/{user}", params={"period": "all"})
    assert response.status_code == 200
    body = response.json()
    assert body["totalMs"] == 7
    assert body["daily"] == [{"date": "2026-09-09", "ms": 7}]


def test_invites_endpoint() -> None:
    db = FakeDB()
    db[queries.COLL_JOIN_ATTRIBUTIONS] = FakeCollection(queries.COLL_JOIN_ATTRIBUTIONS,
        aggregate_results=[[{"_id": "i", "userName": "N", "count": 2}]])
    client = _client(db)
    response = client.get(f"/api/guild/{GUILD}/invites", params={"period": "7d"})
    assert response.status_code == 200
    body = response.json()
    assert body["byInviter"] == [{"userId": "i", "userName": "N", "count": 2}]
    assert body["attributions"] == []


def test_settings_endpoint() -> None:
    db = FakeDB()
    client = _client(db)
    assert client.get(f"/api/guild/{GUILD}/settings").status_code == 404
    db[queries.COLL_GUILD_SETTINGS] = FakeCollection(queries.COLL_GUILD_SETTINGS, docs=[
        {"_id": GUILD, "trackingMode": "all"},
    ])
    response = client.get(f"/api/guild/{GUILD}/settings")
    assert response.status_code == 200
    assert response.json()["trackingMode"] == "all"


def test_members_endpoint_requires_q() -> None:
    client = _client(FakeDB())
    assert client.get(f"/api/guild/{GUILD}/members").status_code == 422
    response = client.get(f"/api/guild/{GUILD}/members", params={"q": "a"})
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_read_endpoints_503_without_db() -> None:
    client = _client(None)
    response = client.get(f"/api/guild/{GUILD}/leaderboard")
    assert response.status_code == 503


def test_names_endpoint_without_token_uses_db_users() -> None:
    db = FakeDB()
    db[queries.COLL_PARTICIPANTS] = FakeCollection(
        queries.COLL_PARTICIPANTS,
        aggregate_results=[[{"_id": USER, "userName": "oldest_name"}, {"_id": "160000000000000001", "userName": "plain"}]],
    )
    db[queries.COLL_NICKNAME_STATE] = FakeCollection(
        queries.COLL_NICKNAME_STATE,
        docs=[{"guildId": GUILD, "userId": USER, "nickname": "fresh_nick"}, {"guildId": GUILD, "userId": "x", "nickname": ""}],
    )
    client = _client(db)
    response = client.get(f"/api/guild/{GUILD}/names")
    assert response.status_code == 200
    body = response.json()
    assert body["guildId"] == GUILD
    # без бот-токена каналы/роли/имя гильдии недоступны — пустые словари и null
    assert body["guildName"] is None
    assert body["channels"] == {}
    assert body["roles"] == {}
    assert body["users"][USER] == "fresh_nick"  # никнейм перекрывает прошлое userName
    assert body["users"]["160000000000000001"] == "plain"
    assert "x" not in body["users"]


def test_names_endpoint_validation() -> None:
    client = _client(FakeDB())
    assert client.get("/api/guild/abc/names").status_code == 422
