"""T11 H06–H09: честная полнота копии журнала Discord.

Backfill возобновляется с checkpoint (а не «первые 500 и всё»), сбой до чекпоинта
не теряет и не дублирует записи, lease координирует работников, 403/429
наблюдаемы и не вызывают tight retry. Настоящий unique-индекс — в
test_audit_state_mongo.py.
"""
from __future__ import annotations

import asyncio
import urllib.parse
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api import audit_sync, discord_api
from fakes import FakeDB

GUILD = "170000000000000000"
OTHER = "170000000000000001"
BASE = 1_800_000_000_000_000_000
CFG = SimpleNamespace(discord_token="t")


class FakeDiscord:
    """Эмуляция GET /guilds/{id}/audit-logs: limit + before (строго более старые),
    как в документированном контракте Discord."""

    def __init__(self, ids: list[int], *, status: int = 200) -> None:
        self.ids = sorted(ids)
        self.status = status
        self.requests: list[str] = []

    async def request(self, cfg, method, path, **kw):
        self.requests.append(path)
        if self.status != 200:
            return self.status, {"message": "nope"}
        query = urllib.parse.parse_qs(path.split("?", 1)[1] if "?" in path else "")
        limit = int(query.get("limit", ["100"])[0])
        before = int(query["before"][0]) if "before" in query else None
        pool = [i for i in self.ids if before is None or i < before]
        page = sorted(pool, reverse=True)[:limit]
        return 200, {
            "audit_log_entries": [{"id": str(i), "action_type": 1} for i in page],
            "users": [],
        }


@pytest.fixture()
def db() -> FakeDB:
    audit_sync._guild_locks.clear()
    return FakeDB()


def _sync(db, server, *, scan_pages=None):
    discord_api_original = discord_api.bot_request
    discord_api.bot_request = server.request
    try:
        kw = {} if scan_pages is None else {"scan_pages": scan_pages}
        return asyncio.run(audit_sync.sync_guild(db, CFG, GUILD, **kw))
    finally:
        discord_api.bot_request = discord_api_original


def _state(db) -> dict:
    return db[audit_sync.COLL_AUDIT_STATE].find_one({"guildId": GUILD}) or {}


def _stored_ids(db, guild=GUILD) -> list[str]:
    return sorted(d["entryId"] for d in db[audit_sync.COLL_DISCORD_AUDIT].docs
                  if d["guildId"] == guild)


# ------------------------------------------------------------------- H06


def test_h06_backfill_resumes_from_checkpoint_and_only_then_claims_complete(db) -> None:
    ids = list(range(BASE, BASE + 1550))  # больше «первых 500» ровно потому, что нельзя
    server = FakeDiscord(ids)

    first = _sync(db, server, scan_pages=5)  # окно + 5 history-страниц за тик
    assert first["ok"] is True
    assert first["backfillComplete"] is False  # 600 из 1550 — полнота не объявлена (H06)
    cursor = _state(db)["backfillCursor"]
    assert cursor, "checkpoint обязан сохраниться"

    second = _sync(db, server, scan_pages=5)
    # возобновление с сохранённой точки, а не сначала: у первого скан-запроса второго
    # тика before равен checkpoint из состояния
    resumes = [p for p in server.requests if p.endswith(f"before={cursor}")]
    assert resumes, "второй проход обязан начать скан с checkpoint, а не с верха"
    assert second["backfillComplete"] is False

    final = _sync(db, server, scan_pages=50)  # добиваем до пустой страницы
    assert final["ok"] and final["backfillComplete"] is True
    stored = _stored_ids(db)
    assert stored == sorted(str(i) for i in ids)  # всё и ровно по разу
    assert len(stored) == len(set(stored))


def test_h06_partial_top_page_still_requires_empty_confirmation(db) -> None:
    ids = list(range(BASE, BASE + 250))
    server = FakeDiscord(ids)
    res = _sync(db, server, scan_pages=1)  # окно(100)+1 скан-страница(100) → осталось 50
    assert res["backfillComplete"] is False
    res = _sync(db, server, scan_pages=10)
    # следующая страница короткая (<100) — полноту НЕ объявляем, только пустая страница
    assert res["backfillComplete"] is True  # её подтверждает последний пустой ответ в этом тике
    assert _stored_ids(db) == sorted(str(i) for i in ids)


def test_h06_bootstrap_old_install_does_not_claim_complete(db) -> None:
    # установка без документа состояния, но с записями от прежнего кода (5 страниц)
    head = BASE + 499
    for i in range(BASE, head + 1):
        db[audit_sync.COLL_DISCORD_AUDIT].update_one(
            {"guildId": GUILD, "entryId": str(i)}, {"$setOnInsert": {"guildId": GUILD, "entryId": str(i)}}, upsert=True)
    st = audit_sync.load_state(db, GUILD)
    assert st["backfillComplete"] is False  # «первые 500» ≠ полный архив (H06)
    assert st["freshCursor"] == str(head)
    assert st["backfillCursor"] == str(BASE)
    server = FakeDiscord(list(range(BASE - 500, BASE + 500)))
    res = _sync(db, server, scan_pages=20)
    assert res["backfillComplete"] is True
    assert _stored_ids(db) == sorted(str(i) for i in range(BASE - 500, BASE + 500))


# ------------------------------------------------------------------- H07


def test_h07_crash_after_page_store_before_checkpoint_loses_nothing(db, monkeypatch) -> None:
    ids = list(range(BASE, BASE + 450))
    server = FakeDiscord(ids)
    real = audit_sync._write_state
    writes = {"n": 0}

    def crash_second(db_, guild, patch, *, owner=""):
        writes["n"] += 1
        if writes["n"] == 2:  # первая scan-страница уже upsert'нута — падение до чекпоинта
            raise RuntimeError("boom")
        return real(db_, guild, patch, owner=owner)

    monkeypatch.setattr(audit_sync, "_write_state", crash_second)
    with pytest.raises(RuntimeError):
        _sync(db, server, scan_pages=5)
    st = _state(db)
    # курсор не перескочил несохранённую границу: записан только чекпоинт окна
    assert st["backfillCursor"] == str(BASE + 350)  # низ топ-окна = min(350..449)
    assert st["backfillComplete"] is False

    monkeypatch.setattr(audit_sync, "_write_state", real)
    res = _sync(db, server, scan_pages=50)
    assert res["ok"] and res["backfillComplete"] is True
    stored = _stored_ids(db)
    assert stored == sorted(str(i) for i in ids)  # повторная страница не породила дублей
    assert len(stored) == len(set(stored))


def test_h07_cursor_never_advances_ahead_of_stored_page(db, monkeypatch) -> None:
    server = FakeDiscord(list(range(BASE, BASE + 250)))
    real_store = audit_sync._store

    def store_then_die(db_, guild, entries):
        n = real_store(db_, guild, entries)
        if len(db_[audit_sync.COLL_DISCORD_AUDIT].docs) >= 200:
            raise RuntimeError("die mid-sync")
        return n

    monkeypatch.setattr(audit_sync, "_store", store_then_die)
    with pytest.raises(RuntimeError):
        _sync(db, server, scan_pages=10)
    st = _state(db)
    # состояние либо на чекпоинте окна, либо пустое — курсор не впереди сохранённого
    stored = _stored_ids(db)
    assert st["freshCursor"] == "" or int(st["freshCursor"]) <= max(int(x) for x in stored)


# ------------------------------------------------------------------- H08


def test_h08_active_lease_blocks_second_worker_without_requests(db) -> None:
    ids = list(range(BASE, BASE + 150))
    server = FakeDiscord(ids)
    audit_sync.load_state(db, GUILD)
    db[audit_sync.COLL_AUDIT_STATE].update_one(
        {"guildId": GUILD},
        {"$set": {"leaseOwner": "other-host:42",
                  "leaseExpiresAt": datetime.now(timezone.utc) + timedelta(minutes=5)}},
    )
    res = _sync(db, server)
    assert res["ok"] is True and res.get("skipped") == "lease-held"
    assert server.requests == []  # ни одного запроса Discord, tight retry нет (H08)
    assert res["backfillComplete"] is False  # и полноты не обещаем

    # истёкший lease перехватывается
    db[audit_sync.COLL_AUDIT_STATE].update_one(
        {"guildId": GUILD},
        {"$set": {"leaseExpiresAt": datetime.now(timezone.utc) - timedelta(minutes=1)}},
    )
    res = _sync(db, server, scan_pages=10)
    assert res["ok"] and res["backfillComplete"] is True
    assert len(_stored_ids(db)) == 150


def test_h08_concurrent_manual_and_background_do_not_duplicate(db) -> None:
    ids = list(range(BASE, BASE + 320))
    server = FakeDiscord(ids)
    orig = discord_api.bot_request
    discord_api.bot_request = server.request

    async def both():
        return await asyncio.gather(
            audit_sync.sync_guild(db, CFG, GUILD, scan_pages=10),
            audit_sync.sync_guild(db, CFG, GUILD, scan_pages=10),
        )

    try:
        a, b = asyncio.run(both())  # in-process — per-guild asyncio.Lock сериализует
    finally:
        discord_api.bot_request = orig
    assert a["ok"] and b["ok"]
    stored = _stored_ids(db)
    assert stored == sorted(str(i) for i in ids)
    assert len(stored) == len(set(stored))  # unique-ключ (guildId, entryId) + upsert = одна запись
    assert sorted([a["inserted"], b["inserted"]]) == [0, 320]  # второй проход не умножает записи
    # lease освобождён
    assert _state(db)["leaseOwner"] == ""


# ------------------------------------------------------------------- H09


def test_h09_403_access_denied_is_surfaced_not_polled(db) -> None:
    server = FakeDiscord([], status=403)
    res = _sync(db, server)
    assert res["ok"] is False and res["discordStatus"] == 403
    assert server.requests == [f"/guilds/{GUILD}/audit-logs?limit=100"]  # один запрос, без ретраев
    snap = audit_sync.status_snapshot(db, GUILD)
    assert snap["accessDenied"] is True
    assert snap["lastError"] and snap["lastError"]["status"] == 403
    assert snap["syncStale"] is False  # 403 — «нет доступа», а не «отстаёт»
    assert snap["backfillComplete"] is False  # ложного «полного архива» нет (H09)


def test_h09_429_backoff_and_recovery(db) -> None:
    ids = list(range(BASE, BASE + 120))
    server = FakeDiscord(ids, status=429)
    res = _sync(db, server)
    assert res["ok"] is False and res["discordStatus"] == 429
    assert len(server.requests) == 1  # tight retry запрещён
    snap = audit_sync.status_snapshot(db, GUILD)
    assert snap["accessDenied"] is False and snap["syncStale"] is True  # ни одного успеха — отстаёт
    # восстановление: следующий тик добирает историю
    server.status = 200
    res = _sync(db, server, scan_pages=10)
    assert res["ok"] and res["backfillComplete"] is True
    snap = audit_sync.status_snapshot(db, GUILD)
    assert snap["lastError"] is None and snap["syncStale"] is False
    assert len(_stored_ids(db)) == 120
