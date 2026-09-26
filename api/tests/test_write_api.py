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
    doc = {"_id": GUILD, "createdAt": datetime(2026, 1, 1, tzinfo=timezone.utc), "revision": 0}
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
        f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "summaryChannelId": USER, "trackingMode": "specific", "trackedChannelIds": [USER]},
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
    response = client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "managedVoiceChannelId": "123"})
    assert response.status_code == 422


def test_patch_rejects_invalid_values() -> None:
    client = _dev_client(_settings_db())
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "trackingMode": "weird"}).status_code == 422
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "trustedUserIds": ["not-a-snowflake"]}).status_code == 422
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "activityEventTypes": ["nope"]}).status_code == 422
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, }).status_code == 422


def test_patch_command_access_and_categories() -> None:
    db = _settings_db()
    client = _dev_client(db)
    response = client.patch(
        f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, 
            "commandAccess": {"stalker": "admin", "Jump": "all"},
            "activityCategoryChannelIds": {"join-leave": USER, "messages": USER},
        },
    )
    assert response.status_code == 200
    call = _update_calls(db, queries.COLL_GUILD_SETTINGS)[-1]
    # имена команд нормализуются в нижний регистр, категории пишутся как есть
    assert call[3]["$set"]["commandAccess"] == {"stalker": "admin", "jump": "all"}
    assert call[3]["$set"]["activityCategoryChannelIds"] == {"join-leave": USER, "messages": USER}


def test_patch_rejects_bad_command_access_and_categories() -> None:
    client = _dev_client(_settings_db())
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "commandAccess": {"jump": "owner"}}).status_code == 422
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "commandAccess": {"Bad Name": "all"}}).status_code == 422
    assert client.patch(
        f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "activityCategoryChannelIds": {"nope": USER}}
    ).status_code == 422
    assert client.patch(
        f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "activityCategoryChannelIds": {"profile": "not-a-snowflake"}}
    ).status_code == 422


def test_patch_activity_event_colors() -> None:
    db = _settings_db()
    client = _dev_client(db)
    response = client.patch(
        f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "activityEventColors": {"member_join": 0x00FF00, "member_leave": 0xED4245}},
    )
    assert response.status_code == 200
    call = _update_calls(db, queries.COLL_GUILD_SETTINGS)[-1]
    assert call[3]["$set"]["activityEventColors"] == {"member_join": 0x00FF00, "member_leave": 0xED4245}
    assert response.json()["revision"] == 1
    # пустой словарь = сброс всех кастомных цветов (на свежей revision)
    reset = client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 1, "activityEventColors": {}})
    assert reset.status_code == 200
    assert reset.json()["revision"] == 2
    assert _update_calls(db, queries.COLL_GUILD_SETTINGS)[-1][3]["$set"]["activityEventColors"] == {}


def test_patch_rejects_bad_activity_event_colors() -> None:
    client = _dev_client(_settings_db())
    # неизвестный тип события
    assert (
        client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "activityEventColors": {"nope": 255}}).status_code == 422
    )
    # цвет вне диапазона RGB
    assert (
        client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "activityEventColors": {"member_join": -1}}).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "activityEventColors": {"member_join": 0x1000000}}
        ).status_code
        == 422
    )
    # не число
    assert (
        client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "activityEventColors": {"member_join": "red"}}).status_code
        == 422
    )


def test_patch_conflict_on_stale_revision() -> None:
    # S01/S04 (unit-слой): устаревшая revision → 409 с безопасными данными
    # новой версии; документ не меняется
    db = _settings_db(updatedAt=datetime(2026, 9, 10, 12, tzinfo=timezone.utc), revision=7)
    client = _dev_client(db)
    response = client.patch(
        f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "summaryChannelId": USER},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current"]["revision"] == 7
    doc = db[queries.COLL_GUILD_SETTINGS].docs[0]
    assert "summaryChannelId" not in doc and doc["revision"] == 7


def test_patch_requires_revision_token() -> None:
    # S04: без токена/не число/отрицательная — 4xx, и никакой записи
    db = _settings_db()
    client = _dev_client(db)
    for body in (
        {"summaryChannelId": USER},
        {"summaryChannelId": USER, "expectedRevision": "abc"},
        {"summaryChannelId": USER, "expectedRevision": -1},
    ):
        response = client.patch(f"/api/guild/{GUILD}/settings", json=body)
        assert response.status_code == 422, body
    doc = db[queries.COLL_GUILD_SETTINGS].docs[0]
    assert "summaryChannelId" not in doc


def test_patch_creates_missing_settings_doc() -> None:
    db = FakeDB()
    client = _dev_client(db)
    response = client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "trackingMode": "none"})
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


def test_chat_preset_add_list_remove() -> None:
    db = _settings_db()
    client = _dev_client(db)
    channel = "190000000000000000"
    add = client.post(
        f"/api/guild/{GUILD}/chat-presets",
        json={"action": "add", "text": "  привет  ", "name": " приветик ", "channelIds": [channel]},
    )
    assert add.status_code == 200
    preset_id = add.json()["presetId"]
    doc = db[queries.COLL_CHAT_PRESETS].docs[0]
    assert doc["_id"] == preset_id
    assert doc["guildId"] == GUILD
    assert doc["text"] == "привет"  # текст нормализуется моделью
    assert doc["name"] == "приветик"
    assert doc["channelIds"] == [channel]
    listing = client.get(f"/api/guild/{GUILD}/chat-presets")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == preset_id and items[0]["text"] == "привет"
    assert items[0]["name"] == "приветик" and items[0]["channelIds"] == [channel]
    assert items[0]["createdAt"]
    actions = [d["action"] for d in db[COLL_AUDIT].docs]
    assert actions == ["chatPreset.add"]
    remove = client.post(f"/api/guild/{GUILD}/chat-presets", json={"action": "remove", "presetId": preset_id})
    assert remove.status_code == 200
    assert db[queries.COLL_CHAT_PRESETS].docs == []
    assert [d["action"] for d in db[COLL_AUDIT].docs] == ["chatPreset.add", "chatPreset.remove"]


def test_chat_preset_add_without_name_keeps_null() -> None:
    db = _settings_db()
    client = _dev_client(db)
    add = client.post(
        f"/api/guild/{GUILD}/chat-presets",
        json={"action": "add", "text": "анонс", "channelIds": ["190000000000000000"]},
    )
    assert add.status_code == 200
    doc = db[queries.COLL_CHAT_PRESETS].docs[0]
    assert doc["name"] is None
    item = client.get(f"/api/guild/{GUILD}/chat-presets").json()["items"][0]
    assert item["name"] is None


def test_chat_preset_validation() -> None:
    db = _settings_db()
    client = _dev_client(db)
    channel = "190000000000000000"
    assert (
        client.post(f"/api/guild/{GUILD}/chat-presets", json={"action": "add", "text": "   ", "channelIds": [channel]})
        .status_code
        == 422
    )
    assert client.post(f"/api/guild/{GUILD}/chat-presets", json={"action": "add"}).status_code == 422
    # канал обязателен: без него пресет некуда отправлять
    assert client.post(f"/api/guild/{GUILD}/chat-presets", json={"action": "add", "text": "x"}).status_code == 422
    assert (
        client.post(
            f"/api/guild/{GUILD}/chat-presets", json={"action": "add", "text": "x", "channelIds": ["не-id"]}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/guild/{GUILD}/chat-presets",
            json={"action": "add", "text": "x", "channelIds": [channel], "name": "y" * 101},
        ).status_code
        == 422
    )
    assert client.post(f"/api/guild/{GUILD}/chat-presets", json={"action": "remove"}).status_code == 422
    assert (
        client.post(
            f"/api/guild/{GUILD}/chat-presets",
            json={"action": "add", "text": "x" * 2001, "channelIds": [channel]},
        ).status_code
        == 422
    )
    assert db[queries.COLL_CHAT_PRESETS].docs == []


def test_chat_preset_embed_add_list_remove() -> None:
    db = _settings_db()
    client = _dev_client(db)
    channel = "190000000000000000"
    add = client.post(
        f"/api/guild/{GUILD}/chat-presets",
        json={
            "action": "add",
            "kind": "embed",
            "name": "анонс",
            "channelIds": [channel],
            "embed": {"title": "  Заголовок  ", "description": "Описание", "color": 0x00FF00},
        },
    )
    assert add.status_code == 200
    doc = db[queries.COLL_CHAT_PRESETS].docs[0]
    assert doc["kind"] == "embed"
    assert "text" not in doc
    # пустые поля не сохраняются, пробелы в тексте нормализуются
    assert doc["embed"] == {"title": "Заголовок", "description": "Описание", "color": 0x00FF00}
    item = client.get(f"/api/guild/{GUILD}/chat-presets").json()["items"][0]
    assert item["kind"] == "embed"
    assert item["embed"] == {"title": "Заголовок", "description": "Описание", "color": 0x00FF00}
    preset_id = add.json()["presetId"]
    remove = client.post(f"/api/guild/{GUILD}/chat-presets", json={"action": "remove", "presetId": preset_id})
    assert remove.status_code == 200
    assert db[queries.COLL_CHAT_PRESETS].docs == []


def test_chat_preset_embed_validation() -> None:
    db = _settings_db()
    client = _dev_client(db)
    channel = "190000000000000000"
    # embed-пресет без блока
    assert (
        client.post(
            f"/api/guild/{GUILD}/chat-presets", json={"action": "add", "kind": "embed", "channelIds": [channel]}
        ).status_code
        == 422
    )
    # пустой блок
    assert (
        client.post(
            f"/api/guild/{GUILD}/chat-presets",
            json={"action": "add", "kind": "embed", "channelIds": [channel], "embed": {}},
        ).status_code
        == 422
    )
    # вложения (имена файлов) в пресет не сохраняются — файл живёт в одном сообщении
    assert (
        client.post(
            f"/api/guild/{GUILD}/chat-presets",
            json={
                "action": "add",
                "kind": "embed",
                "channelIds": [channel],
                "embed": {"title": "x", "image": "pic.png"},
            },
        ).status_code
        == 422
    )
    # канал обязателен
    assert (
        client.post(
            f"/api/guild/{GUILD}/chat-presets",
            json={"action": "add", "kind": "embed", "embed": {"title": "x"}},
        ).status_code
        == 422
    )
    assert db[queries.COLL_CHAT_PRESETS].docs == []


def test_chat_preset_legacy_docs_read_as_text_kind() -> None:
    db = _settings_db()
    client = _dev_client(db)
    # документ, записанный до появления kind
    db[queries.COLL_CHAT_PRESETS].docs.append(
        {"_id": "old1", "guildId": GUILD, "text": "старый", "name": None, "channelIds": ["190000000000000000"]}
    )
    item = client.get(f"/api/guild/{GUILD}/chat-presets").json()["items"][0]
    assert item["kind"] == "text"
    assert item["text"] == "старый"
    assert "embed" not in item


def test_chat_preset_remove_scoped_to_guild() -> None:
    db = _settings_db()
    client = _dev_client(db)
    add = client.post(
        f"/api/guild/{GUILD}/chat-presets",
        json={"action": "add", "text": "наш", "channelIds": ["190000000000000000"]},
    )
    preset_id = add.json()["presetId"]
    other = "170000000000000001"
    remove = client.post(f"/api/guild/{other}/chat-presets", json={"action": "remove", "presetId": preset_id})
    assert remove.status_code == 200  # чужой id просто ничего не удаляет
    assert db[queries.COLL_CHAT_PRESETS].docs[0]["_id"] == preset_id


def test_audit_page_lists_recent_first() -> None:
    db = _settings_db()
    client = _dev_client(db)
    client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "trackingMode": "none"})
    client.post(f"/api/guild/{GUILD}/trusted", json={"userId": USER, "action": "add"})
    response = client.get(f"/api/guild/{GUILD}/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["action"] for item in body["items"]] == ["trustedUserIds.add", "settings.patch"]
    assert all(item["origin"] == "web" for item in body["items"])


def test_audit_origin_filter() -> None:
    db = _settings_db()
    client = _dev_client(db)
    client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "trackingMode": "none"})  # origin web
    db[COLL_AUDIT].docs.append(  # имитация записи, выполненной через Discord API
        {
            "guildId": GUILD,
            "actorUserId": USER,
            "actorName": "boss",
            "action": "bot.kick",
            "before": None,
            "after": {"userId": "1"},
            "ok": True,
            "origin": "discord",
            "at": datetime(2026, 9, 20, tzinfo=timezone.utc),
        }
    )
    both = client.get(f"/api/guild/{GUILD}/audit").json()
    assert both["total"] == 2
    assert {item["origin"] for item in both["items"]} == {"web", "discord"}

    only_discord = client.get(f"/api/guild/{GUILD}/audit", params={"origin": "discord"}).json()
    assert only_discord["total"] == 1
    assert only_discord["items"][0]["action"] == "bot.kick"

    only_web = client.get(f"/api/guild/{GUILD}/audit", params={"origin": "web"}).json()
    assert only_web["total"] == 1
    assert only_web["items"][0]["action"] == "settings.patch"

    assert client.get(f"/api/guild/{GUILD}/audit", params={"origin": "bogus"}).status_code == 422


def _audit_fixture_db() -> FakeDB:
    db = _settings_db()
    target_a = "160000000000000009"
    target_b = "160000000000000008"

    def row(action: str, at: datetime, *, after, ok: bool = True, origin: str = "web") -> dict:
        return {
            "guildId": GUILD,
            "actorUserId": USER,
            "actorName": "mod",
            "action": action,
            "before": None,
            "after": after,
            "ok": ok,
            "origin": origin,
            "at": at,
        }

    db[COLL_AUDIT] = FakeCollection(
        COLL_AUDIT,
        docs=[
            row("bot.timeout", datetime(2026, 9, 1, 10, tzinfo=timezone.utc), after={"userId": target_a, "mute": True, "seconds": 600}),
            row("bot.move", datetime(2026, 9, 5, 10, tzinfo=timezone.utc), after={"userId": target_b, "channelId": "999"}, ok=False),
            row("stalker.add", datetime(2026, 9, 9, 10, tzinfo=timezone.utc), after={"subscriptionId": f"{GUILD}:{USER}:{target_a}"}),
            row("settings.patch", datetime(2026, 9, 20, 10, tzinfo=timezone.utc), after={"summaryChannelId": "555"}, origin="discord"),
        ],
        # FakeCollection.aggregate игнорирует pipeline — выдаём заранее подготовленный ответ $group
        aggregate_results=[
            [
                {"_id": "bot.timeout", "n": 1},
                {"_id": "bot.move", "n": 1},
                {"_id": "stalker.add", "n": 1},
                {"_id": "settings.patch", "n": 1},
            ]
        ],
    )
    return db


def test_audit_filters_target_action_ok_date_sort() -> None:
    client = _dev_client(_audit_fixture_db())
    target_a = "160000000000000009"

    # «над кем»: after.userId ИЛИ target внутри stalker subscriptionId
    over_a = client.get(f"/api/guild/{GUILD}/audit", params={"userId": target_a}).json()
    assert {item["action"] for item in over_a["items"]} == {"bot.timeout", "stalker.add"}

    # фильтр по типу действия
    only_timeout = client.get(f"/api/guild/{GUILD}/audit", params={"action": "bot.timeout"}).json()
    assert only_timeout["total"] == 1
    assert only_timeout["items"][0]["action"] == "bot.timeout"

    # фильтр по итогу: только ошибки
    errors = client.get(f"/api/guild/{GUILD}/audit", params={"ok": "0"}).json()
    assert errors["total"] == 1
    assert errors["items"][0]["action"] == "bot.move"

    # диапазон дат: окно только вокруг bot.move (05 сен)
    ranged = client.get(
        f"/api/guild/{GUILD}/audit", params={"dateFrom": "2026-09-04", "dateTo": "2026-09-06"}
    ).json()
    assert [item["action"] for item in ranged["items"]] == ["bot.move"]

    # сортировка по возрастанию даты
    asc = client.get(f"/api/guild/{GUILD}/audit", params={"sort": "asc"}).json()
    assert [item["action"] for item in asc["items"]] == [
        "bot.timeout",
        "bot.move",
        "stalker.add",
        "settings.patch",
    ]

    # комбинирование фильтров: origin=discord + успешные → пусто (discord-запись ok, но origin другой у ошибок)
    combo = client.get(f"/api/guild/{GUILD}/audit", params={"origin": "discord", "ok": "0"}).json()
    assert combo["total"] == 0

    assert client.get(f"/api/guild/{GUILD}/audit", params={"userId": "bad"}).status_code == 422
    assert client.get(f"/api/guild/{GUILD}/audit", params={"sort": "sideways"}).status_code == 422
    assert client.get(f"/api/guild/{GUILD}/audit", params={"dateFrom": "not-a-date"}).status_code == 422


def test_audit_actions_facets() -> None:
    client = _dev_client(_audit_fixture_db())
    body = client.get(f"/api/guild/{GUILD}/audit/actions").json()
    assert {item["action"] for item in body["items"]} == {
        "bot.timeout",
        "bot.move",
        "stalker.add",
        "settings.patch",
    }
    assert all(item["count"] == 1 for item in body["items"])


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
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "trackingMode": "none"}).status_code == 401
    assert client.get(f"/api/guild/{GUILD}/audit").status_code == 401


@pytest.fixture()
def logged_in_manager(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"guilds": []}

    async def fake_token(cfg, code, redirect_uri):
        return "t"

    async def fake_user_guilds(access_token):
        return {"id": USER, "username": "boss"}, state["guilds"]

    async def fake_resolve_permissions(cfg, guild_id, user_id):
        if guild_id == GUILD:
            return ("allowed", MANAGE)  # Manage Guild, not Administrator
        return ("denied", 0)

    monkeypatch.setattr(discord_api, "oauth_token", fake_token)
    monkeypatch.setattr(discord_api, "oauth_user_guilds", fake_user_guilds)
    monkeypatch.setattr(discord_api, "resolve_permissions", fake_resolve_permissions)
    return state


def _login(client: TestClient, state: dict, perms: dict) -> None:
    state["guilds"] = [{"id": gid, "permissions": str(bits)} for gid, bits in perms.items()]
    login_response = client.get("/api/auth/login")
    import re

    oauth_state = re.search(r"[&?]state=([^&]+)", login_response.headers["location"]).group(1)
    client.get(f"/api/auth/callback?code=abc&state={oauth_state}")


def test_manager_denied_entirely_after_d02(logged_in_manager: dict) -> None:
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
    # D02 (T03): Manage Guild without Administrator is no panel access at all —
    # reads are closed too, not just writes.
    assert client.get(f"/api/guild/{GUILD}/leaderboard").status_code == 403
    assert client.get(f"/api/guild/{GUILD}/audit").status_code == 403
    assert client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": 0, "trackingMode": "none"}).status_code == 403
    assert client.post(f"/api/guild/{GUILD}/trusted", json={"userId": USER, "action": "add"}).status_code == 403
