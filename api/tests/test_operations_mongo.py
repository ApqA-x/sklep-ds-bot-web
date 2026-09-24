"""T08 O02/O07 на реальной Mongo: конкурентная дедупликация и lease/fencing.

Отличия от unit-слоя (test_operations.py), которые требует честный стенд:
duplicate-key по _id — настоящий pymongo-исключение, claim/finish — настоящий
CAS (filter+fence), два потока — настоящие.
"""
from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402

from api import operations  # noqa: E402
from stand_guard import guard_db_name, guard_mongo_uri  # noqa: E402

pytestmark = pytest.mark.integration

URI = "mongodb://127.0.0.1:27099"
GUILD = "170000000000000000"


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
    name = f"voice_tracker_t08_test_{uuid.uuid4().hex[:8]}"
    guard_db_name(name)
    database = client[name]
    yield database
    client.drop_database(name)
    client.close()


def _actor(user: str = "u-1") -> dict[str, str]:
    return {"userId": user, "userName": user}


# O02: два конкурента с одной областью ключа — ровно одна created, один документ, один эффект-кандидат.
def test_concurrent_same_key_single_document(db: Any) -> None:
    results: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        _doc, state = operations.create_intent(
            db,
            guild_id=GUILD,
            actor=_actor(),
            kind="bot.role.grant",
            arguments={"userId": "160000000000000001", "roleId": "150000000000000000"},
            idempotency_key="k-race",
        )
        with lock:
            results.append(state)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["created", "existing"]
    assert db[operations.COLL_OPERATIONS].count_documents({}) == 1


# claim под конкурентом: второй claim активной операции — None (один исполнитель).
def test_concurrent_claim_single_winner(db: Any) -> None:
    doc, _ = operations.create_intent(
        db, guild_id=GUILD, actor=_actor(), kind="bot.move",
        arguments={"userId": "1", "channelId": "2"}, idempotency_key="k-claim",
    )
    winners: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def worker(idx: int) -> None:
        barrier.wait()
        if operations.claim(db, doc["_id"]) is not None:
            with lock:
                winners.append(idx)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(winners) == 1


# O07: просроченный worker не может финализировать попытку нового worker (fence CAS).
def test_fenced_worker_cannot_finish_new_attempt(db: Any) -> None:
    doc, _ = operations.create_intent(
        db, guild_id=GUILD, actor=_actor(), kind="bot.role.grant",
        arguments={"userId": "1", "roleId": "2"}, idempotency_key="k-fence",
    )
    claimed_a, mode_a = operations.claim(db, doc["_id"])
    assert mode_a == "fresh"
    # активный lease A: второй исполнитель не проходит
    assert operations.claim(db, doc["_id"]) is None
    # имитируем зависание A: lease истёк, B делает takeover
    db[operations.COLL_OPERATIONS].update_one(
        {"_id": doc["_id"]}, {"$set": {"leaseExpiresAt": datetime.now(UTC) - timedelta(seconds=1)}}
    )
    claimed_b, mode_b = None, None
    pair = operations.claim(db, doc["_id"])
    assert pair is not None
    claimed_b, mode_b = pair
    assert mode_b == "takeover"
    # A проснулся и пытается финализировать своей (устаревшей) fence-парой
    assert operations.finish(db, doc["_id"], claimed_a, "succeeded", result={"discordStatus": 200}) is False
    fresh = db[operations.COLL_OPERATIONS].find_one({"_id": doc["_id"]})
    assert fresh["state"] == "executing"  # финал A отбит fence'ом
    assert operations.finish(db, doc["_id"], claimed_b, "succeeded", result={"discordStatus": 200}) is True
    fresh = db[operations.COLL_OPERATIONS].find_one({"_id": doc["_id"]})
    assert fresh["state"] == "succeeded"
    assert fresh["attempts"] == 2


# O06: «краш» после effect без финала — документ остаётся executing/lease, status показывает неопределённость.
def test_crash_before_finish_visible_as_unproven(db: Any) -> None:
    doc, _ = operations.create_intent(
        db, guild_id=GUILD, actor=_actor(), kind="bot.kick",
        arguments={"userId": "1", "reason": "r"}, idempotency_key="k-crash",
    )
    operations.claim(db, doc["_id"])
    # worker умер; lease ещё активен — никто не может перехватить
    assert operations.claim(db, doc["_id"]) is None
    mid = db[operations.COLL_OPERATIONS].find_one({"_id": doc["_id"]})
    assert mid["state"] == "executing" and mid["finishedAt"] is None
    # lease истёк — неидемпотентный kind перехватывается только в unknown (без слепого повтора)
    db[operations.COLL_OPERATIONS].update_one(
        {"_id": doc["_id"]}, {"$set": {"leaseExpiresAt": datetime.now(UTC) - timedelta(seconds=1)}}
    )
    claimed, mode = operations.claim(db, doc["_id"])
    assert mode == "takeover"
    operations.finish(db, doc["_id"], claimed, "unknown", error={"classification": "lease_expired_unproven"})
    final = operations.get_operation(db, GUILD, doc["_id"])
    assert final["state"] == "unknown"
    assert operations.public_view(final)["state"] == "unknown"  # status читает факт без переоценки


# batch (O10): children с одним batchId — выборка только своей гильдии.
def test_batch_children_guild_scoped(db: Any) -> None:
    for i, kind in enumerate(("bot.message", "bot.message")):
        arguments = {"channelId": str(140000000000000000 + i), "length": 2, "content": "hi"}
        operations.create_intent(
            db, guild_id=GUILD, actor=_actor(), kind=kind,
            arguments=arguments, idempotency_key=f"k-b{i}", batch_id="batch-1",
        )
    other_doc, _ = operations.create_intent(
        db, guild_id="170000000000000777", actor=_actor(), kind="bot.message",
        arguments={"channelId": "9", "length": 2, "content": "hi"}, idempotency_key="k-b0", batch_id="batch-1",
    )
    mine = operations.list_batch(db, GUILD, "batch-1")
    assert len(mine) == 2
    assert all(d["guildId"] == GUILD for d in mine)
    assert other_doc["_id"] not in [d["_id"] for d in mine]
