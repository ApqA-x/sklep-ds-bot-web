"""T11 H01–H04 на реальной Mongo стенда: keyset-обход (sentAt, messageId).

Что требует честного стенда по сравнению с unit-слоем (test_chat_cursor.py):
настоящий BSON-sort равных sentAt с разной дробной частью секунды, naive-datetime
из tz_aware=False клиента (кодирование курсора без локального сдвига — регресс
ISO-строки и TZ), лексикография 19-значных snowflake в реальном движке.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pymongo = pytest.importorskip("pymongo")
from pymongo import MongoClient  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import queries
from api.config import WebConfig
from api.main import create_app
from stand_guard import guard_db_name, guard_mongo_uri  # noqa: E402

pytestmark = pytest.mark.integration

URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")
GUILD = "170000000000000000"
GUILD2 = "170000000000000001"
CH = "140000000000000000"
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


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
    name = f"voice_tracker_t11_test_{uuid.uuid4().hex[:8]}"
    guard_db_name(name)
    yield client[name]
    client.drop_database(name)
    client.close()


def _snow(n: int) -> str:
    return str(1800000000000000000 + n)


def _seed(db, specs: list[tuple[int, int, str]]) -> None:
    """specs: (msg_i, ms_offset, guild) — messageId=snowflake(i), одна метка на i."""
    db[queries.COLL_CHAT].insert_many([
        {
            "_id": _snow(i),
            "guildId": guild,
            "channelId": CH,
            "messageId": _snow(i),
            "authorUserId": "160000000000000000",
            "authorName": "alice",
            "content": f"msg {i}",
            "sentAt": T0 + timedelta(milliseconds=ms),
        }
        for i, ms, guild in specs
    ])


def _walk(db, *, sort: str, limit: int, guild: str = GUILD, channel_ids: list[str] | None = None) -> list[str]:
    seen: list[str] = []
    cursor = ""
    for _ in range(100):
        page = queries.chat_messages(db, guild, channel_ids, None, limit,
                                     cursor=cursor, sort=sort)
        seen.extend(item["content"] for item in page["items"])
        if not page["hasMore"] or not page["nextCursor"]:
            return seen
        cursor = page["nextCursor"]
    raise AssertionError("traversal did not terminate")


def test_h01_h02_equal_sentat_traversal_complete_on_real_mongo(db) -> None:
    # группы из трёх одинаковых BSON-datetime: ISO-лексикография на таком падала,
    # BSON-sort + (sentAt,messageId) обязан дать полный обход без дублей в обе стороны
    _seed(db, [(i, (i // 3) * 1000, GUILD) for i in range(15)])
    for sort in ("asc", "desc"):
        contents = _walk(db, sort=sort, limit=4)
        assert len(contents) == 15 and len(set(contents)) == 15, sort


def test_display_page_chronological_with_mixed_fractional_seconds(db) -> None:
    # точная регрессия ISO-бага: msg с ms=0 и msg с ms=500 в одной странице desc
    _seed(db, [(0, 0, GUILD), (1, 500, GUILD)])
    page = queries.chat_messages(db, GUILD, None, None, 5, sort="desc")
    assert [i["content"] for i in page["items"]] == ["msg 0", "msg 1"]  # хронологически
    page = queries.chat_messages(db, GUILD, None, None, 5, sort="asc")
    assert [i["content"] for i in page["items"]] == ["msg 0", "msg 1"]


def test_h01_boundary_cursor_survives_naive_datetime_roundtrip(db) -> None:
    # pymongo без tz_aware отдаёт naive: кодирование границы не должно сдвигать её
    # на локальную зону машины — иначе вторая страница теряет/дублирует записи
    _seed(db, [(i, i * 1000, GUILD) for i in range(6)])
    first = queries.chat_messages(db, GUILD, None, None, 2, sort="desc")
    second = queries.chat_messages(db, GUILD, None, None, 2, sort="desc",
                                   cursor=first["nextCursor"])
    got = [i["content"] for i in first["items"]] + [i["content"] for i in second["items"]]
    assert sorted(got) == ["msg 2", "msg 3", "msg 4", "msg 5"]  # ровно next-2 за boundary


def test_h05_big_snowflake_string_over_real_mongo(db) -> None:
    big = 2**63 + 11
    ids = [str(big), str(big + 1), str(big + 2)]
    db[queries.COLL_CHAT].insert_many([
        {"_id": mid, "guildId": GUILD, "channelId": CH, "messageId": mid,
         "authorUserId": "1", "authorName": "a", "content": f"m{mid}",
         "sentAt": T0 + timedelta(milliseconds=700)}
        for mid in ids
    ])
    seen: list[str] = []
    cursor = ""
    for _ in range(10):
        page = queries.chat_messages(db, GUILD, None, None, 2, cursor=cursor, sort="desc")
        for item in page["items"]:
            seen.append(item["messageId"])
        if not page["hasMore"] or not page["nextCursor"]:
            break
        cursor = page["nextCursor"]
    assert sorted(seen) == sorted(ids)  # без потери точности, тип — строка (H05)
    assert all(isinstance(x, str) for x in seen)


def test_h04_cross_guild_and_broken_cursor_are_400(db) -> None:
    _seed(db, [(0, 10, GUILD), (1, 20, GUILD), (2, 10, GUILD2), (3, 20, GUILD2)])
    cfg = WebConfig(mongo_uri="", mongo_db="")
    client = TestClient(create_app(config=cfg, db=db))
    body = client.get(f"/api/guild/{GUILD}/chat", params={"limit": 1}).json()
    cursor = body["nextCursor"]
    assert cursor
    # курсор чужой гильдии — 400 и никаких чужих записей (H04)
    r = client.get(f"/api/guild/{GUILD2}/chat", params={"limit": 1, "cursor": cursor})
    assert r.status_code == 400
    r = client.get(f"/api/guild/{GUILD}/chat", params={"limit": 1, "cursor": "{{{не-b64"})
    assert r.status_code == 400
    r = client.get(f"/api/guild/{GUILD}/chat",
                   params={"limit": 1, "cursor": cursor, "before": "2026-09-01T00:00:01Z"})
    assert r.status_code == 400  # переходный контракт не смешивается молча
