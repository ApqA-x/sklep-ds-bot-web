"""S01-S06 (T06): CAS-контракт revision на РЕАЛЬНОЙ Mongo.

Тесты идут на выделенный инстанс (по умолчанию mongodb://127.0.0.1:27099,
env TEST_MONGO_URI) и в собственную БД, которая пересоздаётся целиком.
Прод- Mongo (127.0.0.1:27017) не используется намеренно.
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402

from api import mutations, queries  # noqa: E402
from api.models import GuildSettingsPatch, ListMemberAction  # noqa: E402

URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")
GUILD = "170000000000000000"
ACTOR = {"userId": "160000000000000001", "userName": "tester"}


def _server_up() -> bool:
    try:
        client = MongoClient(URI, serverSelectionTimeoutMS=1500)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


@pytest.fixture()
def db():
    if not _server_up():
        pytest.skip(f"test mongo not reachable at {URI}")
    client = MongoClient(URI, serverSelectionTimeoutMS=3000)
    name = f"voice_tracker_t06_test_{uuid.uuid4().hex[:8]}"
    database = client[name]
    # guard: тестовая БД обязана быть изолированной и пустой
    assert name.startswith("voice_tracker_t06_test_")
    yield database
    client.drop_database(name)
    client.close()


def _patch(db, fields: dict):
    body = GuildSettingsPatch.model_validate({**fields, "expectedRevision": fields.get("expectedRevision")})
    return mutations.patch_guild_settings(db, GUILD, body, ACTOR)


def _fresh_revision(db) -> int:
    doc = db[queries.COLL_GUILD_SETTINGS].find_one({"_id": GUILD}) or {}
    return int(doc.get("revision") or 0)


# S01: два клиента читают revision 0 и патчат одно поле — один успех, один конфликт.
def test_s01_same_field_conflicts(db) -> None:
    status_a, doc_a = _patch(db, {"summaryChannelId": "140000000000000001", "expectedRevision": 0})
    assert status_a == "updated" and doc_a["revision"] == 1
    status_b, doc_b = _patch(db, {"summaryChannelId": "140000000000000002", "expectedRevision": 0})
    assert status_b == "conflict"
    assert doc_b["revision"] == 1  # безопасные данные новой версии
    doc = db[queries.COLL_GUILD_SETTINGS].find_one({"_id": GUILD})
    assert doc["summaryChannelId"] == "140000000000000001"  # второй клиент НЕ затёр
    assert _fresh_revision(db) == 1


# S02: web-намерение (цвета) и bot-намерение (autoRole) пересекаются — retry сохраняет оба.
def test_s02_independent_intents_survive_retry(db) -> None:
    _patch(db, {"activityEventColors": {"member_join": 1}, "expectedRevision": 0})
    # «бот» читал более ранний снимок (0) → его первый CAS проваливается...
    status, _ = _patch(db, {"autoRoleId": "150000000000000005", "expectedRevision": 0})
    assert status == "conflict"
    # ...retry: перечитать свежий документ и повторно применить конкретное намерение
    status, doc = _patch(db, {"autoRoleId": "150000000000000005", "expectedRevision": _fresh_revision(db)})
    assert status == "updated"
    assert doc["activityEventColors"] == {"member_join": 1}  # намерение web не потеряно
    assert doc["autoRoleId"] == "150000000000000005"


# S03: $addToSet/$pull из разных соединений + stale full-list patch не затирает список.
def test_s03_list_ops_then_stale_overwrite_conflicts(db) -> None:
    user_a, user_b, user_c = "160000000000000011", "160000000000000012", "160000000000000013"
    mutations.mutate_id_list(
        db, GUILD, "trustedUserIds", ListMemberAction(userId=user_a, action="add"), ACTOR
    )
    mutations.mutate_id_list(
        db, GUILD, "trustedUserIds", ListMemberAction(userId=user_b, action="add"), ACTOR
    )
    mutations.mutate_id_list(
        db, GUILD, "trustedUserIds", ListMemberAction(userId=user_c, action="remove"), ACTOR
    )
    rev_after_lists = _fresh_revision(db)
    assert rev_after_lists == 3  # каждая операция подняла revision

    # устаревший снимок: клиент читал мир до списочных операций (revision 0)
    status, doc = _patch(db, {"trustedUserIds": [], "expectedRevision": 0})
    assert status == "conflict"
    fresh = db[queries.COLL_GUILD_SETTINGS].find_one({"_id": GUILD})
    assert sorted(fresh["trustedUserIds"]) == sorted([user_a, user_b])  # состав = операциям
    # намерение удалить user_b через элементную операцию работает и поверх
    mutations.mutate_id_list(
        db, GUILD, "trustedUserIds", ListMemberAction(userId=user_b, action="remove"), ACTOR
    )
    assert db[queries.COLL_GUILD_SETTINGS].find_one({"_id": GUILD})["trustedUserIds"] == [user_a]
    assert _fresh_revision(db) == rev_after_lists + 1


# S05: конкурентное создание отсутствующего документа — ровно один успех, дубликат = конфликт.
def test_s05_concurrent_create_single_document(db) -> None:
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(channel: str) -> None:
        client = MongoClient(URI)
        own = client[db.name]
        body = GuildSettingsPatch.model_validate({"summaryChannelId": channel, "expectedRevision": 0})
        barrier.wait()
        status, _ = mutations.patch_guild_settings(own, GUILD, body, ACTOR)
        results.append(status)
        client.close()

    threads = [threading.Thread(target=worker, args=(f"14000000000000000{i}",)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["conflict", "updated"]
    docs = list(db[queries.COLL_GUILD_SETTINGS].find({"_id": GUILD}))
    assert len(docs) == 1
    assert docs[0]["revision"] == 1


# S06: повторная миграция не сбрасывает revision; неизвестные поля сохранены.
def test_s06_migration_idempotent_keeps_fields(db) -> None:
    db[queries.COLL_GUILD_SETTINGS].insert_one(
        {"_id": GUILD, "trustedUserIds": ["160000000000000009"], "futureWidget": {"keep": True}}
    )
    assert queries.migrate_settings_revision(db) == 1
    assert queries.migrate_settings_revision(db) == 0  # повтор — no-op
    doc = db[queries.COLL_GUILD_SETTINGS].find_one({"_id": GUILD})
    assert doc["revision"] == 0 and doc["futureWidget"] == {"keep": True}
    status, doc = _patch(db, {"summaryChannelId": "140000000000000007", "expectedRevision": 0})
    assert status == "updated"
    doc = db[queries.COLL_GUILD_SETTINGS].find_one({"_id": GUILD})
    assert doc["revision"] == 1
    assert doc["futureWidget"] == {"keep": True}  # CAS $set не затирает неизвестное
    assert doc["trustedUserIds"] == ["160000000000000009"]


# Аудит фиксирует исход CAS: конфликт — не успех.
def test_conflict_writes_no_success_audit(db) -> None:
    _patch(db, {"summaryChannelId": "140000000000000001", "expectedRevision": 0})
    status, _ = _patch(db, {"summaryChannelId": "140000000000000002", "expectedRevision": 0})
    assert status == "conflict"
    rows = list(db[mutations.COLL_AUDIT].find({"action": "settings.patch"}))
    assert len(rows) == 1 and rows[0]["ok"] is True  # только первая запись
