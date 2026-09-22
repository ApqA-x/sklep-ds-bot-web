from __future__ import annotations

import asyncio
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


def test_picker_endpoint_without_token_returns_empty_lists() -> None:
    client = _client(FakeDB())
    response = client.get(f"/api/guild/{GUILD}/picker")
    assert response.status_code == 200
    body = response.json()
    assert body == {"guildId": GUILD, "roles": [], "voiceChannels": [], "textChannels": []}
    assert client.get("/api/guild/abc/picker").status_code == 422


def test_picker_splits_voice_and_text_channels(monkeypatch) -> None:
    from api import discord_api

    rows = [
        {"id": "10", "name": "Категория", "type": 4, "parentId": None, "position": 0},
        {"id": "11", "name": "General", "type": 0, "parentId": "10", "position": 1},
        {"id": "12", "name": "Voice", "type": 2, "parentId": "10", "position": 2},
        {"id": "13", "name": "Announce", "type": 5, "parentId": None, "position": 3},
    ]

    async def fake_roles(cfg, guild):
        return []

    async def fake_channels(cfg, guild):
        return rows

    async def fake_top(cfg, guild):
        return None

    monkeypatch.setattr(discord_api, "guild_roles_raw", fake_roles)
    monkeypatch.setattr(discord_api, "guild_channels_raw", fake_channels)
    monkeypatch.setattr(discord_api, "bot_top_role_position", fake_top)
    client = _client(FakeDB())
    body = client.get(f"/api/guild/{GUILD}/picker").json()
    assert [c["id"] for c in body["voiceChannels"]] == ["12"]
    assert [c["id"] for c in body["textChannels"]] == ["11", "13"]



def test_fetch_bot_guilds_falls_back_when_application_endpoint_fails(monkeypatch) -> None:
    from api import discord_api

    async def fake_get_json(url, headers, *, op):
        if "applications/" in url:
            raise discord_api.DiscordError(op, 404, {"message": "404: Not Found"})
        return [{"id": GUILD, "name": "Гильдия"}]

    monkeypatch.setattr(discord_api, "_get_json", fake_get_json)
    cfg = WebConfig(mongo_uri="", mongo_db="", discord_token="t", discord_application_id="999")
    guilds = asyncio.run(discord_api.fetch_bot_guilds(cfg))
    assert guilds == [{"id": GUILD, "name": "Гильдия"}]


def test_bot_top_role_position_degrades_to_none_on_member_error(monkeypatch) -> None:
    from api import discord_api

    async def fake_get_json(url, headers, *, op):
        if "/members/" in url:
            raise discord_api.DiscordError(op, 50035, {"message": "Invalid Form Body"})
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(discord_api, "_get_json", fake_get_json)
    cfg = WebConfig(mongo_uri="", mongo_db="", discord_token="t", discord_application_id="123")
    assert asyncio.run(discord_api.bot_top_role_position(cfg, GUILD)) is None


def test_picker_keeps_roles_when_bot_hierarchy_unknown(monkeypatch) -> None:
    from api import discord_api

    role_rows = [{"id": "1", "name": "Mod", "position": 5, "color": 0xA6E3A1}]

    async def fake_roles(cfg, guild):
        return role_rows

    async def fake_channels(cfg, guild):
        return []

    async def fake_top(cfg, guild):
        raise discord_api.DiscordError("bot_member", 400, {"message": "no"})

    monkeypatch.setattr(discord_api, "guild_roles_raw", fake_roles)
    monkeypatch.setattr(discord_api, "guild_channels_raw", fake_channels)
    monkeypatch.setattr(discord_api, "bot_top_role_position", fake_top)
    client = _client(FakeDB())
    body = client.get(f"/api/guild/{GUILD}/picker").json()
    assert [r["id"] for r in body["roles"]] == ["1"]
    assert body["roles"][0]["assignable"] is True  # иерархия неизвестна — не скрываем роли


def test_build_role_options_rules() -> None:
    from api import discord_api

    rows = [
        {"id": GUILD, "name": "@everyone", "position": 0, "color": 0},
        {"id": "1", "name": "Admin", "position": 10, "color": 0xF38BA8},
        {"id": "2", "name": "Mod", "position": 5, "color": 0xA6E3A1},
        {"id": "3", "name": "Bot Role", "position": 8, "color": 0, "managed": True},
        {"id": "4", "name": "Boost", "position": 7, "color": 0xF9E2AF, "tags": {"boost": True}},
    ]
    options = discord_api.build_role_options(rows, GUILD, bot_top_position=6)
    ids = [o["id"] for o in options]
    assert ids == ["1", "3", "4", "2", GUILD][:4]  # @everyone исключён; остальные по position desc
    by_id = {o["id"]: o for o in options}
    assert by_id["1"]["assignable"] is False  # выше топ-роли бота
    assert by_id["2"]["assignable"] is True
    assert by_id["3"]["assignable"] is False  # managed
    assert by_id["4"]["assignable"] is False  # tags (интеграция)
    assert by_id["2"]["color"] == 0xA6E3A1

    # без информации о позиции бота (нет токена) — не фильтруем по иерархии
    options_no_top = discord_api.build_role_options(rows, GUILD, None)
    assert {o["id"]: o["assignable"] for o in options_no_top} == {"1": True, "2": True, "3": False, "4": False}


def test_build_voice_channels_only_voice_types() -> None:
    from api import discord_api

    rows = [
        {"id": "10", "name": "General", "type": 0, "position": 0},
        {"id": "11", "name": "Лобби", "type": 2, "position": 2},
        {"id": "12", "name": "Музыка", "type": 2, "position": 1},
        {"id": "13", "name": "Ивент", "type": 13, "position": 3},
        {"id": "14", "name": "Категория", "type": 4, "position": 4},
    ]
    channels = discord_api.build_voice_channels(rows)
    assert [c["id"] for c in channels] == ["12", "11", "13"]


def _chat_db() -> FakeDB:
    db = FakeDB()
    channel = "140000000000000000"
    db[queries.COLL_CHAT] = FakeCollection(
        queries.COLL_CHAT,
        docs=[
            {
                "_id": f"m{i}",
                "guildId": GUILD,
                "channelId": channel,
                "messageId": f"m{i}",
                "authorUserId": USER,
                "authorName": "alice",
                "content": f"text {i}",
                "sentAt": _dt(1 + i // 10).replace(hour=i % 10),
            }
            for i in range(12)
        ],
        aggregate_results=[[{"_id": channel, "count": 12, "lastAt": _dt(2).replace(hour=1)}]],
    )
    return db


def test_chat_channels_endpoint() -> None:
    db = _chat_db()
    client = _client(db)
    response = client.get(f"/api/guild/{GUILD}/chat/channels")
    assert response.status_code == 200
    items = response.json()["items"]
    assert items == [{"channelId": "140000000000000000", "count": 12, "lastAt": "2026-09-02T01:00:00Z"}]


def test_chat_messages_pagination_and_chronological_order() -> None:
    db = _chat_db()
    client = _client(db)
    channel = "140000000000000000"
    response = client.get(f"/api/guild/{GUILD}/chat", params={"channelId": channel, "limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["hasMore"] is True
    assert len(body["items"]) == 5
    texts = [item["content"] for item in body["items"]]
    assert texts == sorted(texts, key=lambda t: int(t.split()[-1]))  # хронологически
    oldest = body["items"][0]["sentAt"]
    assert body["nextBefore"] == oldest

    page2 = client.get(f"/api/guild/{GUILD}/chat", params={"channelId": channel, "limit": 5, "before": oldest}).json()
    assert all(item["sentAt"] < oldest for item in page2["items"])
    assert set(item["content"] for item in page2["items"]).isdisjoint(set(texts))


def test_chat_endpoint_validation() -> None:
    client = _client(FakeDB())
    assert client.get(f"/api/guild/{GUILD}/chat", params={"channelId": "bad"}).status_code == 422
    assert client.get(f"/api/guild/{GUILD}/chat").status_code == 422  # channelId обязателен
    assert (
        client.get(f"/api/guild/{GUILD}/chat", params={"channelId": "140000000000000000", "before": "oops"}).status_code
        == 422
    )
