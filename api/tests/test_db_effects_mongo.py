"""R26-04: durable-журнал локальных мутаций на РЕАЛЬНОЙ Mongo (порт пробы ревью).

Сценарий: CAS-запись прошла, первая вставка в web_audit_logs упала. Журнал обязан
сохранить терминальную запись с auditError, а bounded reproject — достроить ровно
одну audit-проекцию без повторной мутации (revision не ползёт).

Стенд: отдельный инстанс (по умолчанию mongodb://127.0.0.1:27099, env TEST_MONGO_URI),
БД пересоздаётся целиком; guard'ы fail-closed отсекают прод-ресурсы.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402

from api import mutations, operations, queries  # noqa: E402
from api.models import GuildSettingsPatch  # noqa: E402
from stand_guard import guard_db_name, guard_mongo_uri  # noqa: E402

pytestmark = pytest.mark.integration

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
    guard_mongo_uri(URI)
    if not _server_up():
        pytest.skip(f"test mongo not reachable at {URI}")
    client = MongoClient(URI, serverSelectionTimeoutMS=3000)
    name = f"voice_tracker_t04_test_{uuid.uuid4().hex[:8]}"
    guard_db_name(name)
    database = client[name]
    yield database
    client.drop_database(name)
    client.close()


class _FailFirstAuditDB:
    """Прокси БД: первая вставка в web_audit_logs бросает ошибку, дальше — прокидываем.

    Имитация отказа записи аудита сразу после применённого CAS-эффекта — ровно тот
    дефект, который в старом коде оставлял изменение без восстанавливаемого следа.
    """

    def __init__(self, database: Any) -> None:
        self._db = database
        self._armed = True

    def __getitem__(self, name: str) -> Any:
        coll = self._db[name]
        if name != mutations.COLL_AUDIT:
            return coll
        outer = self

        class _AuditColl:
            def insert_one(self, doc: dict, **kw: Any):
                if outer._armed:
                    outer._armed = False
                    raise RuntimeError("audit insert failed (injected)")
                return coll.insert_one(doc, **kw)

            def __getattr__(self, attr: str):
                return getattr(coll, attr)

        return _AuditColl()

    def __getattr__(self, attr: str):
        return getattr(self._db, attr)


def _revision(db) -> int:
    doc = db[queries.COLL_GUILD_SETTINGS].find_one({"_id": GUILD}) or {}
    return int(doc.get("revision") or 0)


def test_settings_audit_failure_leaves_recoverable_trace_real_mongo(db) -> None:
    proxy = _FailFirstAuditDB(db)
    body = GuildSettingsPatch.model_validate(
        {"summaryChannelId": "140000000000000001", "expectedRevision": 0}
    )
    status, doc = mutations.patch_guild_settings(proxy, GUILD, body, ACTOR)
    assert status == "updated"
    assert doc["revision"] == 1  # эффект применён
    assert db[mutations.COLL_AUDIT].count_documents({}) == 0  # проекция упала

    op = db[operations.COLL_OPERATIONS].find_one({"kind": "db.settings.patch"})
    assert op is not None  # не ноль журнальных строк, как было в дефекте
    assert op["state"] == "succeeded" and op["auditError"] is True

    outcome = mutations.reproject_pending_audits(db, guild_id=GUILD)
    assert outcome["recovered"] == 1

    rows = list(db[mutations.COLL_AUDIT].find({}))
    assert len(rows) == 1
    assert rows[0]["operationId"] == op["_id"]
    assert rows[0]["_id"] == operations.audit_entry_id(GUILD, op["_id"])
    assert rows[0]["action"] == "settings.patch"
    assert rows[0]["after"]["revision"] == 1  # before/after привязаны к применённой revision
    assert _revision(db) == 1  # reproject не повторил мутацию

    again = mutations.reproject_pending_audits(db, guild_id=GUILD)
    assert again["recovered"] == 0
    assert db[mutations.COLL_AUDIT].count_documents({}) == 1  # дубль отсекается _id


def test_all_route_mutations_journal_real_mongo(db) -> None:
    from api.models import ChatPresetAction, ListMemberAction, StalkerAction

    mutations.mutate_id_list(
        db, GUILD, "trustedUserIds", ListMemberAction(userId="160000000000000011", action="add"), ACTOR
    )
    mutations.mutate_stalker(
        db,
        GUILD,
        StalkerAction(watcherUserId="160000000000000002", targetUserId="160000000000000011", action="add"),
        ACTOR,
    )
    preset = mutations.mutate_chat_preset(
        db,
        GUILD,
        ChatPresetAction(action="add", text="анонс", channelIds=["190000000000000000"]),
        ACTOR,
    )
    kinds = [d["kind"] for d in db[operations.COLL_OPERATIONS].find({})]
    assert kinds == ["db.list.mutate", "db.stalker.mutate", "db.preset.mutate"]
    for d in db[operations.COLL_OPERATIONS].find({}):
        assert d["state"] == "succeeded" and d["auditError"] is False
    rows = {d["action"]: d for d in db[mutations.COLL_AUDIT].find({})}
    assert set(rows) == {"trustedUserIds.add", "stalker.add", "chatPreset.add"}
    for action, row in rows.items():
        assert row["operationId"]
        assert row["_id"] == operations.audit_entry_id(GUILD, row["operationId"])
    assert rows["chatPreset.add"]["after"]["presetId"] == preset["presetId"]
