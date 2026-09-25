"""T11 H06–H08 на реальной Mongo стенда: checkpoint backfill и unique-контракты.

Unit-слой (test_audit_state.py) проверяет алгоритм на фейках; здесь — то, что
фейк имитировать не может: настоящий duplicate-key по unique (guildId, entryId)
и unique (guildId) из манифеста (M4), два настоящих потока в один sync,
перехват просроченного lease CAS'ом.
"""
from __future__ import annotations

import asyncio
import os
import threading
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

from api import audit_sync, discord_api, schema_contract
from stand_guard import guard_db_name, guard_mongo_uri  # noqa: E402

pytestmark = pytest.mark.integration

URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")
GUILD = "170000000000000000"
BASE = 1_800_000_000_000_000_000
CFG = SimpleNamespace(discord_token="t")


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
    audit_sync._guild_locks.clear()
    guard_mongo_uri(URI)
    if not _server_up():
        pytest.skip(f"test mongo not reachable at {URI}")
    client = MongoClient(URI, serverSelectionTimeoutMS=3000)
    name = f"voice_tracker_t11audit_test_{uuid.uuid4().hex[:8]}"
    guard_db_name(name)
    yield client[name]
    client.drop_database(name)
    client.close()


class FakeDiscord:
    def __init__(self, ids: list[int]) -> None:
        self.ids = sorted(ids)
        self.lock = threading.Lock()
        self.requests: list[str] = []

    async def request(self, cfg, method, path, **kw):
        with self.lock:
            self.requests.append(path)
        query = urllib.parse.parse_qs(path.split("?", 1)[1] if "?" in path else "")
        limit = int(query.get("limit", ["100"])[0])
        before = int(query["before"][0]) if "before" in query else None
        pool = [i for i in self.ids if before is None or i < before]
        page = sorted(pool, reverse=True)[:limit]
        return 200, {"audit_log_entries": [{"id": str(i), "action_type": 1} for i in page], "users": []}


def _sync(db, server, *, scan_pages=None):
    orig = discord_api.bot_request
    discord_api.bot_request = server.request
    try:
        kw = {} if scan_pages is None else {"scan_pages": scan_pages}
        return asyncio.run(audit_sync.sync_guild(db, CFG, GUILD, **kw))
    finally:
        discord_api.bot_request = orig


def _manifest_spec(name: str) -> dict:
    manifest = schema_contract.load_manifest()
    return next(s for s in manifest["indexes"] if s["name"] == name)


# ---------------------------------------------------------------- M4 контракт


def test_m4_unique_from_manifest_holds_one_state_doc_per_guild(db) -> None:
    spec = _manifest_spec("discord_audit_state_guildId_unique")
    assert spec["owner"] == "runner" and spec["unique"] is True
    keys = [tuple(k) for k in spec["keys"]]
    db[spec["collection"]].create_index(keys, name=spec["name"], unique=True)
    audit_sync.load_state(db, GUILD)  # upsert создаёт документ состояния
    with pytest.raises(DuplicateKeyError):  # второй документ на гильдию невозможен (H08)
        db[spec["collection"]].insert_one({"guildId": GUILD, "freshCursor": "999"})
    # другая гильдия — ок
    db[spec["collection"]].insert_one({"guildId": "170000000000000001", "freshCursor": "9"})
    assert db[spec["collection"]].count_documents({}) == 2


# ---------------------------------------------------------------- H06/H07


def test_h06_backfill_resumes_from_saved_cursor_on_real_mongo(db) -> None:
    ids = list(range(BASE, BASE + 520))
    server = FakeDiscord(ids)
    first = _sync(db, server, scan_pages=2)  # окно + 2 history-страницы
    assert first["ok"] and first["backfillComplete"] is False
    state = db[audit_sync.COLL_AUDIT_STATE].find_one({"guildId": GUILD})
    checkpoint = state["backfillCursor"]
    before_reqs = len(server.requests)
    second = _sync(db, server, scan_pages=50)
    scan_paths = server.requests[before_reqs:]
    assert any(p.endswith(f"before={checkpoint}") for p in scan_paths), \
        "второй проход обязан продолжить с сохранённой точки"
    assert second["backfillComplete"] is True
    stored = [d["entryId"] for d in db[audit_sync.COLL_DISCORD_AUDIT].find({"guildId": GUILD})]
    assert sorted(stored) == sorted(str(i) for i in ids)
    assert len(stored) == len(set(stored))


def test_h07_crash_after_store_before_checkpoint_dedups_on_resume(db, monkeypatch) -> None:
    ids = list(range(BASE, BASE + 350))
    server = FakeDiscord(ids)
    real = audit_sync._write_state
    n = {"c": 0}

    def crash_on_second(db_, guild, patch, *, owner=""):
        n["c"] += 1
        if n["c"] == 2:  # первая scan-страница уже upsert'нута — падение до чекпоинта
            raise RuntimeError("boom")
        return real(db_, guild, patch, owner=owner)

    monkeypatch.setattr(audit_sync, "_write_state", crash_on_second)
    with pytest.raises(RuntimeError):
        _sync(db, server, scan_pages=5)
    monkeypatch.setattr(audit_sync, "_write_state", real)

    res = _sync(db, server, scan_pages=50)
    assert res["ok"] and res["backfillComplete"] is True
    stored = [d["entryId"] for d in db[audit_sync.COLL_DISCORD_AUDIT].find({"guildId": GUILD})]
    assert sorted(stored) == sorted(str(i) for i in ids)  # ни потери, ни дубля (H07)
    assert len(stored) == len(set(stored))
    # lease после «краша» освобождён (finally) и следующая синхронизация его берёт
    assert db[audit_sync.COLL_AUDIT_STATE].find_one({"guildId": GUILD})["leaseOwner"] == ""


# ---------------------------------------------------------------- H08


def test_h08_two_threads_same_entries_single_docs(db) -> None:
    # общий unique (guildId, entryId) из M3 — фундамент dedup'а обоих работников
    spec = _manifest_spec("discord_audit_guildId_entryId_unique")
    db[spec["collection"]].create_index([tuple(k) for k in spec["keys"]],
                                        name=spec["name"], unique=True)
    entries = [{"id": str(BASE + i), "action_type": 1} for i in range(60)]
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker(tag: int) -> None:
        try:
            barrier.wait(timeout=10)
            for e in entries:
                doc = audit_sync._to_doc(GUILD, e)
                db[audit_sync.COLL_DISCORD_AUDIT].update_one(
                    {"guildId": GUILD, "entryId": doc["entryId"]},
                    {"$setOnInsert": doc}, upsert=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert db[audit_sync.COLL_DISCORD_AUDIT].count_documents({"guildId": GUILD}) == 60


def test_h08_expired_lease_stolen_active_not(db) -> None:
    audit_sync.load_state(db, GUILD)
    live = db[audit_sync.COLL_AUDIT_STATE].find_one_and_update(
        {"guildId": GUILD},
        {"$set": {"leaseOwner": "other:1",
                  "leaseExpiresAt": datetime.now(timezone.utc) + timedelta(minutes=5)}},
        return_document=True)
    assert live is not None
    server = FakeDiscord([BASE])
    res = _sync(db, server)
    assert res.get("skipped") == "lease-held" and server.requests == []

    db[audit_sync.COLL_AUDIT_STATE].update_one(
        {"guildId": GUILD},
        {"$set": {"leaseExpiresAt": datetime.now(timezone.utc) - timedelta(seconds=1)}})
    res = _sync(db, server, scan_pages=5)
    assert res["ok"] and not res.get("skipped") and res["storedEntries"] == 1
