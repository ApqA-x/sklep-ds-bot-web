"""R26-04: durable-журнал локальных DB-мутаций + идемпотентная audit-проекция.

Регрессия ревью-пробы settings_changed_without_recoverable_audit: раньше CAS
проходил, audit-вставка падала — изменение оставалось безjournal-строк. Теперь
намерение пишется ДО эффекта, а audit — проекция терминальной записи журнала с
детерминированным _id; сбой проекции восстановим (reproject) без повторной мутации.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api import operations, queries
from api.config import WebConfig
from api.main import create_app
from api.mutations import COLL_AUDIT, COLL_STALKER, reproject_pending_audits
from fakes import FakeCollection, FakeDB

GUILD = "170000000000000000"
USER = "160000000000000001"
CHANNEL = "190000000000000000"


def _settings_db(**fields) -> FakeDB:
    db = FakeDB()
    doc = {"_id": GUILD, "createdAt": datetime(2026, 1, 1, tzinfo=timezone.utc), "revision": 0}
    doc.update(fields)
    db[queries.COLL_GUILD_SETTINGS] = FakeCollection(queries.COLL_GUILD_SETTINGS, docs=[doc])
    return db


def _dev_client(db: FakeDB) -> TestClient:
    cfg = WebConfig(mongo_uri="", mongo_db="")
    return TestClient(create_app(config=cfg, db=db))


def _ops_of_kind(db: FakeDB, kind: str) -> list[dict]:
    return [d for d in db[operations.COLL_OPERATIONS].docs if d.get("kind") == kind]


def _patch(client: TestClient, expected: int, **fields):
    return client.patch(f"/api/guild/{GUILD}/settings", json={"expectedRevision": expected, **fields})


def _revision(db: FakeDB) -> int:
    return int((db[queries.COLL_GUILD_SETTINGS].find_one({"_id": GUILD}) or {}).get("revision") or 0)


# T1 (зеркало O01 для локальных writes): журнал недоступен — мутации нет вовсе.
def test_journal_intent_failure_blocks_mutation() -> None:
    db = _settings_db()
    db[operations.COLL_OPERATIONS] = FakeCollection(operations.COLL_OPERATIONS, fail_insert=True)
    client = _dev_client(db)
    response = _patch(client, 0, summaryChannelId=CHANNEL)
    assert response.status_code == 503
    assert _revision(db) == 0  # CAS не исполнялся
    assert db[COLL_AUDIT].docs == []


# T2 (портированная проба ревью): эффект применён, проекция упала — след восстанавливаем.
def test_settings_audit_failure_leaves_recoverable_trace() -> None:
    db = _settings_db()
    db[COLL_AUDIT] = FakeCollection(COLL_AUDIT, fail_insert=True)
    client = _dev_client(db)
    response = _patch(client, 0, summaryChannelId=CHANNEL)
    assert response.status_code == 200
    assert _revision(db) == 1

    ops = _ops_of_kind(db, "db.settings.patch")
    assert len(ops) == 1  # не ноль журнальных строк, как было в дефекте
    op = ops[0]
    assert op["state"] == "succeeded" and op["auditError"] is True
    assert db[COLL_AUDIT].docs == []  # проекция ещё не выполнена

    db[COLL_AUDIT].fail_insert = False
    outcome = reproject_pending_audits(db)
    assert outcome == {"considered": 1, "recovered": 1, "remaining": 0}
    rows = db[COLL_AUDIT].docs
    assert len(rows) == 1
    assert rows[0]["operationId"] == op["_id"]
    assert rows[0]["_id"] == operations.audit_entry_id(GUILD, op["_id"])
    assert _revision(db) == 1  # reproject не повторяет мутацию

    # повтор идемпотентен: флаг auditError снят, дубль отсекается детерминированным _id
    assert reproject_pending_audits(db)["recovered"] == 0
    assert len(db[COLL_AUDIT].docs) == 1


# T3: все маршрутные мутации ведут journal и несут operationId в аудите.
def test_list_stalker_preset_audits_carry_operation_id() -> None:
    db = _settings_db(trustedUserIds=[])
    client = _dev_client(db)
    watcher = "160000000000000002"
    assert client.post(f"/api/guild/{GUILD}/trusted", json={"userId": USER, "action": "add"}).status_code == 200
    assert client.post(
        f"/api/guild/{GUILD}/stalker",
        json={"watcherUserId": watcher, "targetUserId": USER, "action": "add"},
    ).status_code == 200
    preset = client.post(
        f"/api/guild/{GUILD}/chat-presets", json={"action": "add", "text": "анонс", "channelIds": [CHANNEL]}
    )
    assert preset.status_code == 200

    expected = [
        ("db.list.mutate", "trustedUserIds.add"),
        ("db.stalker.mutate", "stalker.add"),
        ("db.preset.mutate", "chatPreset.add"),
    ]
    assert [d["action"] for d in db[COLL_AUDIT].docs] == [action for _kind, action in expected]
    for kind, action in expected:
        ops = _ops_of_kind(db, kind)
        assert len(ops) == 1 and ops[0]["state"] in operations.TERMINAL
        op = ops[0]
        row = next(d for d in db[COLL_AUDIT].docs if d["action"] == action)
        assert row["operationId"] == op["_id"]
        assert row["_id"] == operations.audit_entry_id(GUILD, op["_id"])
        assert row["ok"] is True

    # контракты after прежние (тесты test_write_api завязаны на эти поля)
    list_row = next(d for d in db[COLL_AUDIT].docs if d["action"] == "trustedUserIds.add")
    assert list_row["before"] == {"ids": []}
    assert list_row["after"] == {"ids": [USER], "userId": USER}
    stalker_row = next(d for d in db[COLL_AUDIT].docs if d["action"] == "stalker.add")
    assert stalker_row["after"]["subscriptionId"] == f"{GUILD}:{watcher}:{USER}"
    preset_row = next(d for d in db[COLL_AUDIT].docs if d["action"] == "chatPreset.add")
    assert preset_row["after"]["presetId"] == preset.json()["presetId"]
    assert db[COLL_STALKER].docs[0]["_id"] == f"{GUILD}:{watcher}:{USER}"


# T4: конфликт — это failed-запись журнала, а не аудит об успехе.
def test_conflict_writes_failed_operation_not_audit() -> None:
    db = _settings_db(revision=5)
    client = _dev_client(db)
    response = _patch(client, 0, summaryChannelId=CHANNEL)
    assert response.status_code == 409
    ops = _ops_of_kind(db, "db.settings.patch")
    assert len(ops) == 1
    assert ops[0]["state"] == "failed"
    assert ops[0]["error"]["classification"] == "revision_conflict"
    assert db[COLL_AUDIT].docs == []
    assert _revision(db) == 5


# T5: recovery наблюдаем через существующий UI-опрос статуса — без новых эндпоинтов.
def test_status_endpoint_reprojects() -> None:
    db = _settings_db()
    db[COLL_AUDIT] = FakeCollection(COLL_AUDIT, fail_insert=True)
    client = _dev_client(db)
    response = _patch(client, 0, summaryChannelId=CHANNEL)
    assert response.status_code == 200
    op = _ops_of_kind(db, "db.settings.patch")[0]

    stuck = client.get(f"/api/guild/{GUILD}/bot/operations/{op['_id']}")
    assert stuck.status_code == 200
    assert stuck.json()["audited"] is False  # сбой проекции остаётся видимым

    db[COLL_AUDIT].fail_insert = False
    recovered = client.get(f"/api/guild/{GUILD}/bot/operations/{op['_id']}")
    assert recovered.status_code == 200
    assert recovered.json()["audited"] is True
    assert recovered.json()["state"] == "succeeded"
    rows = db[COLL_AUDIT].docs
    assert len(rows) == 1 and rows[0]["operationId"] == op["_id"]
    assert _revision(db) == 1  # перепроекция не повторила мутацию


def test_lost_finish_fence_after_settings_cas_is_not_reported_as_saved(monkeypatch) -> None:
    db = _settings_db()
    real_finish = operations.finish

    def lose_fence(db_arg, op_id, claimed, state, **kwargs):
        db_arg[operations.COLL_OPERATIONS].update_one({"_id": op_id}, {"$inc": {"fenceVersion": 1}})
        return real_finish(db_arg, op_id, claimed, state, **kwargs)

    monkeypatch.setattr(operations, "finish", lose_fence)
    client = _dev_client(db)
    response = _patch(client, 0, summaryChannelId=CHANNEL)

    assert response.status_code == 504
    detail = response.json()["detail"]
    assert detail["error"] == "ownership_lost"
    assert detail["state"] == "executing"
    assert detail["operationId"] == _ops_of_kind(db, "db.settings.patch")[0]["_id"]
    assert _revision(db) == 1  # эффект мог примениться, повторять его нельзя
    assert db[COLL_AUDIT].docs == []  # старый worker не проецирует чужой финал
    status = client.get(f"/api/guild/{GUILD}/bot/operations/{detail['operationId']}")
    assert status.status_code == 200
    assert status.json()["state"] == "executing"


def test_lost_finish_fence_after_revision_conflict_is_not_reported_as_conflict(monkeypatch) -> None:
    db = _settings_db(revision=5)
    monkeypatch.setattr(operations, "finish", lambda *_args, **_kwargs: False)

    response = _patch(_dev_client(db), 0, summaryChannelId=CHANNEL)

    assert response.status_code == 504
    assert response.json()["detail"]["error"] == "ownership_lost"
    assert response.json()["detail"]["operationId"] == _ops_of_kind(db, "db.settings.patch")[0]["_id"]
    assert _revision(db) == 5


def test_effect_exception_returns_unknown_with_operation_id() -> None:
    from api import mutations

    db = _settings_db()

    def partial_effect():
        db[queries.COLL_GUILD_SETTINGS].update_one({"_id": GUILD}, {"$inc": {"revision": 1}})
        raise RuntimeError("injected failure after effect")

    with pytest.raises(Exception) as caught:
        mutations._journal_effect(
            db,
            guild_id=GUILD,
            actor={"userId": USER, "userName": "tester"},
            kind="db.settings.patch",
            arguments={"expectedRevision": 0, "auditAction": "settings.patch"},
            effect=partial_effect,
        )

    assert getattr(caught.value, "status_code", None) == 504
    assert caught.value.detail["error"] == "outcome_unproven"
    assert caught.value.detail["state"] == "unknown"
    assert caught.value.detail["operationId"] == _ops_of_kind(db, "db.settings.patch")[0]["_id"]
    assert _revision(db) == 1


def test_finish_exception_after_effect_returns_operation_id(monkeypatch) -> None:
    db = _settings_db()

    def unavailable_finish(*_args, **_kwargs):
        raise RuntimeError("journal temporarily unavailable")

    monkeypatch.setattr(operations, "finish", unavailable_finish)
    response = _patch(_dev_client(db), 0, summaryChannelId=CHANNEL)

    assert response.status_code == 504
    assert response.json()["detail"]["error"] == "journal_unavailable_after_effect"
    assert response.json()["detail"]["operationId"] == _ops_of_kind(db, "db.settings.patch")[0]["_id"]
    assert _revision(db) == 1
    assert db[COLL_AUDIT].docs == []
