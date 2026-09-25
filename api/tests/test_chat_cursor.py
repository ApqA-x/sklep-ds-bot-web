"""T11 H01–H05: keyset-пагинация чата (sentAt, messageId).

Кривые equal-sentAt границы — то, из-за чего timestamp-курсор терял записи; на
фейке проверяются лексикографические пары и контракт курсора, настоящая Mongo —
в test_chat_cursor_mongo.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from api import queries
from api.config import WebConfig
from api.main import create_app
from fakes import FakeCollection, FakeDB

GUILD = "170000000000000000"
GUILD2 = "170000000000000001"
CH1 = "140000000000000000"
USER = "160000000000000000"

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _dt(ms: int) -> datetime:
    return T0 + timedelta(milliseconds=ms)


def _snowflake(n: int) -> str:
    # 19-значный «снефлейк»: одинаковая длина → лексикографический порядок = числовой
    return str(1800000000000000000 + n)


def _doc(msg_i: int, *, guild: str = GUILD, channel: str = CH1, ms: int = 0) -> dict:
    sid = _snowflake(msg_i)
    return {
        "_id": sid,
        "guildId": guild,
        "channelId": channel,
        "messageId": sid,
        "authorUserId": USER,
        "authorName": "alice",
        "content": f"msg {msg_i}",
        "sentAt": _dt(ms),
    }


def _db(docs: list[dict]) -> FakeDB:
    db = FakeDB()
    db[queries.COLL_CHAT] = FakeCollection(queries.COLL_CHAT, docs=docs)
    return db


def _page(db: FakeDB, *, sort: str, limit: int, cursor: str = "", guild: str = GUILD, **kw):
    return queries.chat_messages(db, guild, kw.pop("channel_ids", None), None, limit,
                                 cursor=cursor, sort=sort, **kw)


def _walk(db: FakeDB, *, sort: str, limit: int, guild: str = GUILD, **kw) -> list[str]:
    """Полный обход страниц keyset-курсором: возвращает content по порядку отдачи."""
    seen: list[str] = []
    cursor = ""
    for _ in range(100):  # защитка от бесконечного цикла в тесте
        page = _page(db, sort=sort, limit=limit, cursor=cursor, guild=guild, **kw)
        seen.extend(item["content"] for item in page["items"])
        if not page["hasMore"] or not page["nextCursor"]:
            return seen
        cursor = page["nextCursor"]
    raise AssertionError("cursor traversal did not terminate")


# ------------------------------------------------------------------- H01 / H02


def test_h01_equal_sentat_all_records_exactly_once() -> None:
    # три сообщения с одинаковым sentAt, размер страницы 2
    docs = [_doc(i, ms=500) for i in range(3)] + [_doc(9, ms=400)]
    for sort in ("asc", "desc"):
        contents = _walk(_db(docs), sort=sort, limit=2)
        assert sorted(contents) == ["msg 0", "msg 1", "msg 2", "msg 9"]
        assert len(contents) == 4  # без дублей (H01)


def test_h02_full_traversal_both_directions_stable() -> None:
    # десятки одинаковых sentAt + обычные; обход полон и детерминирован в обе стороны
    docs = []
    for i in range(30):
        docs.append(_doc(i, ms=(i // 3) * 100))  # по 3 на каждую метку времени
    db = _db(docs)
    asc = _walk(db, sort="asc", limit=4)
    assert len(asc) == 30 and len(set(asc)) == 30
    assert [int(c.split()[-1]) for c in asc] == sorted(int(c.split()[-1]) for c in asc)
    desc = _walk(db, sort="desc", limit=4)
    # каждая страница отдаётся хронологически, но страницы при desc идут от новых к старым
    assert sorted(desc) == sorted(asc) and len(desc) == 30 and len(set(desc)) == 30


def test_boundary_pages_are_disjoint() -> None:
    # страница не пересекается со следующей даже на равных sentAt
    docs = [_doc(i, ms=700) for i in range(5)]
    db = _db(docs)
    first = _page(db, sort="desc", limit=2)
    assert first["hasMore"] and first["nextCursor"]
    second = _page(db, sort="desc", limit=2, cursor=first["nextCursor"])
    ids1 = {i["messageId"] for i in first["items"]}
    ids2 = {i["messageId"] for i in second["items"]}
    assert ids1.isdisjoint(ids2)


# ------------------------------------------------------------------- H03


def test_h03_new_message_between_pages_follows_declared_contract() -> None:
    # desc: курсор ведёт от первой страницы назад по времени; прилетевшее НОВОЕ
    # сообщение не появляется в середине обхода (контракт: свежее — через первый
    # запрос), но существующие boundary-записи не теряются
    docs = [_doc(i, ms=i * 1000) for i in range(6)]  # 0..5, метки уникальны
    db = _db(docs)
    first = _page(db, sort="desc", limit=2)  # msg 4, msg 5 (внутри страницы — хронологически)
    assert [i["content"] for i in first["items"]] == ["msg 4", "msg 5"]
    db[queries.COLL_CHAT].docs.append(_doc(6, ms=6500))  # новое — позже всего
    rest = _page(db, sort="desc", limit=10, cursor=first["nextCursor"])
    contents = [i["content"] for i in rest["items"]]
    assert contents == ["msg 0", "msg 1", "msg 2", "msg 3"]  # ничего не потеряно (H03)
    assert "msg 6" not in contents  # и не влезло в середину
    # первый запрос его видит
    fresh = _page(db, sort="desc", limit=2)
    assert fresh["items"][-1]["content"] == "msg 6"


def test_h03_deletion_at_boundary_does_not_skip_records() -> None:
    docs = [_doc(i, ms=(i % 2) * 1000) for i in range(6)]  # равные пары меток
    db = _db(docs)
    first = _page(db, sort="desc", limit=4)  # msg 5, 3, 1 (ms 1000) + msg 4 (ms 0)
    cursor = first["nextCursor"]
    # удаление уже показанного сообщения не должно сдвигать следующую границу
    db[queries.COLL_CHAT].docs = [d for d in db[queries.COLL_CHAT].docs if d["content"] != "msg 5"]
    rest = _page(db, sort="desc", limit=10, cursor=cursor)
    assert {i["content"] for i in rest["items"]} == {"msg 2", "msg 0"}


# ------------------------------------------------------------------- H04


def test_h04_cursor_bound_to_filters_and_guild() -> None:
    docs = [_doc(i, ms=i * 100) for i in range(6)] + [_doc(20, guild=GUILD2, ms=150)]
    db = _db(docs)
    good = _page(db, sort="desc", limit=2)
    cursor = good["nextCursor"]
    # смена sort — курсор несовместим
    with pytest.raises(queries.ChatCursorError):
        _page(db, sort="asc", limit=2, cursor=cursor)
    # чужая guild — fingerprint не совпадает → отказ, а не чужой обход
    with pytest.raises(queries.ChatCursorError):
        _page(db, sort="desc", limit=2, cursor=cursor, guild=GUILD2)
    # смена фильтра — тоже отказ (H04: «сбрасывать и явно отвергать несовместимый»)
    with pytest.raises(queries.ChatCursorError):
        _page(db, sort="desc", limit=2, cursor=cursor, user_id=USER)


def test_h04_malformed_and_operator_payloads_rejected() -> None:
    db = _db([_doc(i, ms=i) for i in range(3)])
    import base64
    import json as _json

    def enc(payload: dict) -> str:
        raw = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    valid = _page(db, sort="desc", limit=1)["nextCursor"]
    payload = _json.loads(base64.urlsafe_b64decode(valid + "=="))
    bad_variants = [
        "not-base64-$$$",
        enc({"v": 1}),  # мусор
        enc({**payload, "t": {"$ne": None}}),  # operator в времени
        enc({**payload, "m": {"$gt": ""}}),  # operator в id
        enc({**payload, "m": "1; dropDatabase()"}),  # не-cifry
        enc({**payload, "extra": 1}),  # неизвестное поле
        enc({**payload, "v": 99}),  # не та версия
    ]
    for bad in bad_variants:
        with pytest.raises(queries.ChatCursorError):
            _page(db, sort="desc", limit=1, cursor=bad)


def test_h04_guild_filter_applies_to_both_or_branches() -> None:
    # $or-ветки границы не могут вытащить чужие guildId/каналы
    docs = [
        _doc(1, ms=100),
        _doc(2, ms=100),
        _doc(3, ms=100, guild=GUILD2),
        _doc(4, ms=100, channel="140000000000000001"),
    ]
    db = _db(docs)
    first = _page(db, sort="desc", limit=1, channel_ids=[CH1])  # msg 2
    assert first["hasMore"] and first["nextCursor"]
    rest = _page(db, sort="desc", limit=10, cursor=first["nextCursor"], channel_ids=[CH1])
    got = {i["messageId"] for i in rest["items"]} | {i["messageId"] for i in first["items"]}
    assert got == {_snowflake(1), _snowflake(2)}


# ------------------------------------------------------------------- H05


def test_h05_big_snowflake_no_precision_loss() -> None:
    # snowflake далеко за 2^53 — строка проходит курсор без округлений
    big = 2**63 + 7
    docs = [_doc(i, ms=50) for i in range(3)]
    for n, d in enumerate(docs):
        d["messageId"] = str(big + n)
        d["_id"] = d["messageId"]
    db = _db(docs)
    contents = _walk(db, sort="desc", limit=2)
    assert sorted(contents) == ["msg 0", "msg 1", "msg 2"]


def test_h05_legacy_before_still_works_and_mixing_is_400() -> None:
    db = _db([_doc(i, ms=i * 1000) for i in range(5)])
    # переходный контракт: старый before живёт (страница отдаётся хронологически)
    page = queries.chat_messages(db, GUILD, None, _dt(3000), 10)
    assert [i["content"] for i in page["items"]] == ["msg 0", "msg 1", "msg 2"]
    assert page["nextBefore"] == "2026-09-01T00:00:00Z"
    # и не смешивается молча
    with pytest.raises(queries.ChatCursorError):
        queries.chat_messages(db, GUILD, None, _dt(3000), 10,
                              cursor=_page(db, sort="desc", limit=2)["nextCursor"])


# ------------------------------------------------------------------- endpoint


def _client(db: FakeDB) -> TestClient:
    cfg = WebConfig(mongo_uri="", mongo_db="")
    return TestClient(create_app(config=cfg, db=db))


def test_endpoint_cursor_contract() -> None:
    db = _db([_doc(i, ms=i * 100) for i in range(5)])
    client = _client(db)
    body = client.get(f"/api/guild/{GUILD}/chat", params={"limit": 2, "sort": "desc"}).json()
    assert body["hasMore"] is True and body["nextCursor"]
    p2 = client.get(f"/api/guild/{GUILD}/chat",
                    params={"limit": 2, "sort": "desc", "cursor": body["nextCursor"]}).json()
    assert {i["content"] for i in p2["items"]} == {"msg 2", "msg 1"}
    # last page: no cursor offered
    p3 = client.get(f"/api/guild/{GUILD}/chat",
                    params={"limit": 9, "sort": "desc", "cursor": p2["nextCursor"]}).json()
    assert p3["hasMore"] is False and p3["nextCursor"] is None


def test_endpoint_invalid_cursor_is_safe_400() -> None:
    client = _client(_db([_doc(0)]))
    r = client.get(f"/api/guild/{GUILD}/chat", params={"cursor": "!!!broken!!!"})
    assert r.status_code == 400
    r = client.get(f"/api/guild/{GUILD}/chat",
                   params={"cursor": "e3siOiJ4In0", "before": "2026-09-01T00:00:00Z"})
    assert r.status_code == 400  # XOR переходного контракта — тоже 400


def test_fingerprint_normalizes_channel_order_and_duplicates() -> None:
    fp1 = queries.chat_filter_fingerprint(GUILD, [CH1, "140000000000000001"], None, None, None, None, "desc")
    fp2 = queries.chat_filter_fingerprint(GUILD, ["140000000000000001", CH1, CH1], None, None, None, None, "desc")
    assert fp1 == fp2


def test_encode_cursor_naive_datetime_treated_as_utc() -> None:
    # реальный pymongo (tz_aware=False) отдаёт naive — курсор не должен зависеть от
    # локальной зоны тестовой машины/прода (урок T09)
    fp = queries.chat_filter_fingerprint(GUILD, None, None, None, None, None, "desc")
    aware = queries.encode_chat_cursor(_dt(1500), _snowflake(1), "desc", fp)
    naive = queries.encode_chat_cursor(_dt(1500).replace(tzinfo=None), _snowflake(1), "desc", fp)
    assert aware == naive
