"""T08 (O01–O10, unit-уровень): журнал операций до эффекта, идемпотентность, lease/fencing.

Внешний мир подменён на HTTP-boundary (discord_api.bot_request), БД — FakeCollection
с честной unique-семантикой _id. Конкурентные сценарии на реальной Mongo —
test_operations_mongo.py (маркер integration).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api import auth as auth_module
from api import bot as bot_module
from api import discord_api, operations
from api.config import WebConfig
from api.main import create_app
from fakes import DuplicateKeyError, FakeCollection, FakeDB

GUILD = "170000000000000000"
GUILD_B = "170000000000000001"
USER = "160000000000000001"
ROLE = "150000000000000000"
ROLE_B = "150000000000000009"
CHANNEL = "140000000000000000"
CHANNEL_B = "140000000000000001"


class ServerDisconnectedError(Exception):
    """Имя совпадает с aiohttp — classify_transport относит к «неизвестный исход»."""


class ClientConnectorError(Exception):
    """Имя совпадает с aiohttp — «соединение не установлено», эффекта точно не было."""


def _meta_payload(path: str):
    if path.startswith("/channels/"):
        cid = path.split("/")[2]
        return {"id": cid, "guild_id": GUILD, "type": 0}
    if path.endswith("/roles") and path.startswith("/guilds/"):
        return [{"id": ROLE, "name": "role"}, {"id": ROLE_B, "name": "role-b"}]
    return {"id": USER}


def _install_discord(monkeypatch, *, status: int = 200, raises: Exception | None = None) -> list[dict]:
    recorded: list[dict] = []

    async def fake_request(cfg, method, path, *, json_body=None, reason=None, files=None):
        if method == "GET":
            return 200, _meta_payload(path)
        if raises is not None:
            raise raises
        recorded.append({"method": method, "path": path, "json": json_body})
        return status, {"id": "effect-1"}

    monkeypatch.setattr(discord_api, "bot_request", fake_request)
    bot_module._reset_rate_buckets()
    auth_module._clear_recheck_cache()
    return recorded


_UNSET = object()


def _client(db: FakeDB | None | object = _UNSET, **cfg_overrides) -> TestClient:
    overrides = {"discord_token": "bot-token-fake"}
    overrides.update(cfg_overrides)
    cfg = WebConfig(mongo_uri="", mongo_db="", **overrides)
    if db is _UNSET:
        db = FakeDB()
    return TestClient(create_app(config=cfg, db=db))


def _role_url() -> str:
    return f"/api/guild/{GUILD}/bot/member/{USER}/roles"


def _grant(client: TestClient, key: str, *, roleId: str = ROLE) -> Any:
    return client.post(_role_url(), json={"roleId": roleId, "action": "grant"}, headers={"Idempotency-Key": key})


# O01: БД недоступна / insert намерения падает — ноль внешних эффектов, контролируемая ошибка.
def test_intent_failure_blocks_every_discord_call(monkeypatch) -> None:
    calls = _install_discord(monkeypatch)
    db = FakeDB()
    db["operations"] = FakeCollection("operations", fail_insert=True)
    client = _client(db)
    response = _grant(client, "k-o01")
    assert response.status_code == 503
    assert calls == []  # ни одного Discord-эффекта до устойчивого намерения


def test_missing_db_returns_503_without_effect(monkeypatch) -> None:
    calls = _install_discord(monkeypatch)
    client = _client(db=None)
    assert client.post(_role_url(), json={"roleId": ROLE, "action": "grant"}).status_code == 503
    assert calls == []


# O02 (unit-уровень unique _id): повтор ключа не создаёт второй документ.
def test_same_key_never_creates_second_document(monkeypatch) -> None:
    _install_discord(monkeypatch)
    client = _client()
    db = client.app.state.db  # type: ignore[attr-defined]
    first = _grant(client, "k-o02")
    second = _grant(client, "k-o02")
    assert first.status_code == 200 and second.status_code == 200
    docs = list(db["operations"].find({}))
    assert len(docs) == 1


# O03: один ключ, другой payload — конфликт, без второго эффекта.
def test_same_key_different_payload_conflicts(monkeypatch) -> None:
    calls = _install_discord(monkeypatch)
    client = _client()
    assert _grant(client, "k-o03", roleId=ROLE).status_code == 200
    other_role = "150000000000000009"
    conflict = client.post(
        _role_url(), json={"roleId": other_role, "action": "grant"}, headers={"Idempotency-Key": "k-o03"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error"] == "idempotency_conflict"
    assert len(calls) == 1  # второй эффект не отправлен


# O04: повтор после succeeded — сохранённый результат, effect count не растёт.
def test_replay_after_success_returns_saved_result_without_effect(monkeypatch) -> None:
    calls = _install_discord(monkeypatch)
    client = _client()
    first = _grant(client, "k-o04").json()
    assert first["ok"] is True and first["state"] == "succeeded"
    replay = _grant(client, "k-o04").json()
    assert replay["replayed"] is True and replay["ok"] is True
    assert replay["operationId"] == first["operationId"]
    assert replay["result"]["discordStatus"] == 200
    assert len(calls) == 1


def test_failed_replay_does_not_reexecute(monkeypatch) -> None:
    calls = _install_discord(monkeypatch, status=403)
    client = _client()
    first = _grant(client, "k-fail")
    assert first.status_code == 502 and first.json()["detail"]["operationId"]
    replay = _grant(client, "k-fail")
    assert replay.status_code == 502
    assert replay.json()["detail"]["replayed"] is True
    assert len(calls) == 1  # «new attempt → новый key», повтор не стреляет повторно


# O05: обрыв транспорта после отправки — unknown, не ложный failed; повтор не переигрывает вслепую.
def test_transport_drop_after_send_leaves_unknown(monkeypatch) -> None:
    calls = _install_discord(monkeypatch, raises=ServerDisconnectedError("boom"))
    client = _client()
    response = _grant(client, "k-o05")
    assert response.status_code == 504
    detail = response.json()["detail"]
    assert detail["state"] == "unknown"
    op = operations.get_operation(client.app.state.db, GUILD, detail["operationId"])
    assert op["state"] == "unknown"
    # повтор того же key — 504 unknown, Discord не дёрнут второй раз
    replay = _grant(client, "k-o05")
    assert replay.status_code == 504
    assert len(calls) == 0  # ни одного завершённого изменяющего вызова


def test_connection_not_established_is_retryable_failed(monkeypatch) -> None:
    _install_discord(monkeypatch, raises=ClientConnectorError("no route"))
    client = _client()
    response = _grant(client, "k-o05b")
    assert response.status_code == 504
    op = operations.get_operation(client.app.state.db, GUILD, response.json()["detail"]["operationId"])
    assert op["state"] == "failed"  # эффект доказанно не произошёл
    assert op["error"]["classification"] == "connection_not_established"


# O07: takeover истёкшего lease для неидемпотентного kind — без слепого повтора.
def test_stale_lease_non_idempotent_takeover_refuses_blind_retry(monkeypatch) -> None:
    calls = _install_discord(monkeypatch)
    db = FakeDB()
    client = _client(db)
    key = "k-o07"
    body = {"content": "hello"}
    url = f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message"
    doc = {
        "_id": operations.operation_id(GUILD, "0", "bot.message", key),
        "guildId": GUILD,
        "actorUserId": "0",
        "kind": "bot.message",
        "arguments": {"channelId": CHANNEL, "length": len("hello"), "content": "hello"},
        "requestHash": operations.canonical_hash("bot.message", {"channelId": CHANNEL, "length": 5, "content": "hello"}),
        "idempotencyKey": key,
        "batchId": None,
        "state": "executing",
        "attempts": 1,
        "fenceVersion": 1,
        "leaseOwner": "dead-worker",
        "leaseExpiresAt": datetime.now(UTC) - timedelta(seconds=5),
        "createdAt": datetime.now(UTC),
        "updatedAt": datetime.now(UTC),
        "finishedAt": None,
        "result": None,
        "error": None,
        "auditError": False,
    }
    db["operations"].docs.append(doc)
    response = client.post(url, json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 504
    assert response.json()["detail"]["error"] == "outcome_unproven"
    assert calls == []  # неидемпотентный эффект вслепую не повторён
    fresh = operations.get_operation(db, GUILD, doc["_id"])
    assert fresh["state"] == "unknown"


def test_stale_lease_idempotent_takeover_reexecutes(monkeypatch) -> None:
    calls = _install_discord(monkeypatch)
    db = FakeDB()
    client = _client(db)
    key = "k-o07b"
    arguments = {"userId": USER, "roleId": ROLE, "action": "grant"}
    doc = {
        "_id": operations.operation_id(GUILD, "0", "bot.role.grant", key),
        "guildId": GUILD,
        "actorUserId": "0",
        "kind": "bot.role.grant",
        "arguments": arguments,
        "requestHash": operations.canonical_hash("bot.role.grant", arguments),
        "idempotencyKey": key,
        "batchId": None,
        "state": "executing",
        "attempts": 1,
        "fenceVersion": 1,
        "leaseOwner": "dead-worker",
        "leaseExpiresAt": datetime.now(UTC) - timedelta(seconds=5),
        "createdAt": datetime.now(UTC),
        "updatedAt": datetime.now(UTC),
        "finishedAt": None,
        "result": None,
        "error": None,
        "auditError": False,
    }
    db["operations"].docs.append(doc)
    response = _grant(client, key)
    assert response.status_code == 200  # state-idempotent — повтор безопасен
    assert len(calls) == 1
    assert db["operations"].docs[0]["state"] == "succeeded"


def test_active_lease_second_claimer_gets_in_progress(monkeypatch) -> None:
    _install_discord(monkeypatch)
    db = FakeDB()
    client = _client(db)
    key = "k-active"
    arguments = {"userId": USER, "roleId": ROLE, "action": "grant"}
    doc = {
        "_id": operations.operation_id(GUILD, "0", "bot.role.grant", key),
        "guildId": GUILD,
        "actorUserId": "0",
        "kind": "bot.role.grant",
        "arguments": arguments,
        "requestHash": operations.canonical_hash("bot.role.grant", arguments),
        "idempotencyKey": key,
        "batchId": None,
        "state": "executing",
        "attempts": 1,
        "fenceVersion": 1,
        "leaseOwner": "live-worker",
        "leaseExpiresAt": datetime.now(UTC) + timedelta(seconds=60),
        "createdAt": datetime.now(UTC),
        "updatedAt": datetime.now(UTC),
        "finishedAt": None,
        "result": None,
        "error": None,
        "auditError": False,
    }
    db["operations"].docs.append(doc)
    response = _grant(client, key)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "operation_in_progress"


# O08: audit-проекция упала — факт операции остаётся, успех не маскируется.
def test_audit_projection_failure_marked_not_hidden(monkeypatch) -> None:
    _install_discord(monkeypatch)
    db = FakeDB()
    db["web_audit_logs"] = FakeCollection("web_audit_logs", fail_insert=True)
    client = _client(db)
    response = _grant(client, "k-o08")
    assert response.status_code == 200  # эффект реально исполнен
    status = client.get(f"/api/guild/{GUILD}/bot/operations/{response.json()['operationId']}")
    assert status.status_code == 200
    body = status.json()
    assert body["state"] == "succeeded"
    assert body["audited"] is False  # след потери проекции виден, а не потерян


def test_audit_entry_carries_operation_id(monkeypatch) -> None:
    _install_discord(monkeypatch)
    client = _client()
    op_id = _grant(client, "k-audit").json()["operationId"]
    rows = list(client.app.state.db["web_audit_logs"].find({"action": "bot.role"}))
    assert rows and rows[0]["operationId"] == op_id
    assert rows[0]["ok"] is True


# T08.9: защищённый status-endpoint + guild-scope; O09 — чужая операция не различима от несуществующей.
def test_status_endpoint_guild_scoped(monkeypatch) -> None:
    _install_discord(monkeypatch)
    client = _client()
    op_id = _grant(client, "k-scope").json()["operationId"]
    own = client.get(f"/api/guild/{GUILD}/bot/operations/{op_id}")
    assert own.status_code == 200 and own.json()["operationId"] == op_id
    foreign = client.get(f"/api/guild/{GUILD_B}/bot/operations/{op_id}")
    assert foreign.status_code == 404  # утечки результата другой гильдии нет


# O10: батч из нескольких каналов — отдельные child-операции и явные статусы.
def test_batch_children_have_own_statuses(monkeypatch) -> None:
    _install_discord(monkeypatch)
    client = _client()
    headers = {"Idempotency-Key": "k-batch-a", "X-Batch-Id": "batch-1"}
    r1 = client.post(f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message", json={"content": "one"}, headers=headers)
    r2 = client.post(
        f"/api/guild/{GUILD}/bot/channel/{CHANNEL_B}/message",
        json={"content": "two"},
        headers={"Idempotency-Key": "k-batch-b", "X-Batch-Id": "batch-1"},
    )
    assert r1.status_code == 200 and r2.status_code == 200
    batch = client.get(f"/api/guild/{GUILD}/bot/operations?batchId=batch-1").json()
    assert batch["complete"] is True and batch["pending"] == 0
    assert {v["state"] for v in batch["operations"]} == {"succeeded"}
    assert len({v["operationId"] for v in batch["operations"]}) == 2


# O09 (ops-уровень): та же строка key у двух актёров — разные операции, авторизация не обходится.
def test_scope_includes_actor() -> None:
    db = FakeDB()
    doc_a, created_a = operations.create_intent(
        db, guild_id=GUILD, actor={"userId": "u-a", "userName": "A"}, kind="bot.role.grant",
        arguments={"x": 1}, idempotency_key="shared-key",
    )
    doc_b, created_b = operations.create_intent(
        db, guild_id=GUILD, actor={"userId": "u-b", "userName": "B"}, kind="bot.role.grant",
        arguments={"x": 1}, idempotency_key="shared-key",
    )
    assert created_a == "created" and created_b == "created"
    assert doc_a["_id"] != doc_b["_id"]
    assert len(db["operations"].docs) == 2


def test_unknown_kind_rejected_before_any_write() -> None:
    db = FakeDB()
    with pytest.raises(Exception) as caught:
        operations.create_intent(
            db, guild_id=GUILD, actor={"userId": "u", "userName": "u"}, kind="bot.launch-missiles",
            arguments={}, idempotency_key="k",
        )
    assert caught.value.status_code == 422  # type: ignore[attr-defined]
    assert db["operations"].docs == []


def test_duplicate_detection_recognizes_real_pymongo_error() -> None:
    from pymongo.errors import DuplicateKeyError as RealDuplicate  # type: ignore[import-not-found]

    assert operations._is_duplicate(RealDuplicate("E11000 duplicate key error"))
    assert operations._is_duplicate(DuplicateKeyError("x"))
    assert not operations._is_duplicate(RuntimeError("x"))


def test_canonical_hash_stable_across_key_order() -> None:
    h1 = operations.canonical_hash("bot.message", {"a": 1, "b": {"c": 2, "d": [1, 2]}})
    h2 = operations.canonical_hash("bot.message", {"b": {"d": [1, 2], "c": 2}, "a": 1})
    assert h1 == h2


def test_classify_transport_names() -> None:
    assert operations.classify_transport(ClientConnectorError("x")) == ("connection_not_established", True)
    assert operations.classify_transport(ServerDisconnectedError("x")) == ("connection_lost_after_send", False)
    assert operations.classify_transport(ValueError("x")) == ("transport_error", False)
