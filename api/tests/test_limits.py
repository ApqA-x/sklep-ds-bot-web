"""T16 (L01/L03/L04): бюджеты запросов, rate-limit и ограниченная память лимитеров.

L04 — предсказуемые ответы API: 429+Retry-After на flood дорогих чтений,
503+query_timeout на превышение time budget Mongo, 422 на глубокий page-skip.
L03 — состояние бакетов ограничено: ключ может быть IP клиента, словарь не
должен расти вместе с числом уникальных ключей.
"""
from __future__ import annotations

import pytest
from pymongo.errors import ExecutionTimeout

from api import limits
from api.config import WebConfig
from api.main import create_app
from api.queries import TTLCache


# ---------------------------------------------------------------- L03: память


def test_token_bucket_bounds_unique_keys() -> None:
    bucket = limits.TokenBucket(capacity=1000, refill_per_s=1000, max_keys=16)
    for i in range(500):
        bucket.allow(f"key-{i}")
    assert len(bucket._state) <= 16  # рост состояния = DoS по RAM, отсюда потолок
    # LRU: живой ключ переживает десятки новых уникальных, а холодный — нет
    bucket.allow("hot")
    for i in range(50):
        bucket.allow(f"burst-{i}")
        bucket.allow("hot")
    assert "hot" in bucket._state
    assert "key-0" not in bucket._state
    assert len(bucket._state) <= 16


def test_ttl_cache_bounds_unique_keys() -> None:
    cache = TTLCache(ttl_seconds=60, max_entries=8)
    for i in range(100):
        cache.set(f"k{i}", i)
    assert len(cache._entries) <= 8
    # последний записанный обязательно жив
    assert cache.get("k99") == 99


# ---------------------------------------------------------------- L04: 429


def test_expensive_flood_gets_429_with_retry_after() -> None:
    from fastapi.testclient import TestClient

    from api.main import create_app
    from fakes import FakeCollection, FakeDB

    db = FakeDB()
    page = [[{"page": [{"_id": "5", "userName": "U", "totalMs": 1, "appearances": 1}], "total": [{"n": 1}]}]] * 10
    db["voice_session_participants"] = FakeCollection("voice_session_participants", aggregate_results=page)
    guild = "170000000000000000"

    limits.setup_defaults()
    original = limits.bucket("leaderboard")
    limits._buckets["leaderboard"] = limits.TokenBucket(capacity=2, refill_per_s=0.0001)
    try:
        client = TestClient(create_app(config=WebConfig(mongo_uri="", mongo_db=""), db=db))
        first = client.get(f"/api/guild/{guild}/leaderboard", params={"period": "7d", "limit": 10})
        second = client.get(f"/api/guild/{guild}/leaderboard", params={"period": "7d", "limit": 10})
        third = client.get(f"/api/guild/{guild}/leaderboard", params={"period": "7d", "limit": 10})
        assert first.status_code == 200 and second.status_code == 200
        assert third.status_code == 429
        assert int(third.headers["retry-after"]) >= 1
        assert "retry" in third.json()["detail"].lower()
        # остальные маршруты обслуживаются — лимитер точечный
        ok = client.get(f"/api/guild/{guild}/leaderboard", params={"period": "7d", "limit": 10})
        assert ok.status_code == 429  # бакет всё ещё пуст (refill ~0)
        other = client.get(f"/api/guild/{guild}/sessions/active")
        assert other.status_code != 429
    finally:
        limits._buckets["leaderboard"] = original


def test_gate_retry_after_header_is_int() -> None:
    bucket = limits.TokenBucket(capacity=1, refill_per_s=0.5)
    assert bucket.allow("g") is True
    assert bucket.allow("g") is False
    assert bucket.retry_after("g") >= 1


# ---------------------------------------------------------------- L04: 503


def test_execution_timeout_maps_to_503_query_timeout() -> None:
    from fastapi.testclient import TestClient

    class _TimingOutCollection:
        def aggregate(self, *_a, **_k):
            raise ExecutionTimeout("budget blown")

        def find(self, *_a, **_k):
            raise ExecutionTimeout("budget blown")

        def count_documents(self, *_a, **_k):
            raise ExecutionTimeout("budget blown")

    class _TimingOutDB:
        def __getitem__(self, _name):
            return _TimingOutCollection()

    from api import queries

    queries._leaderboard_cache.clear()  # иначе ответ прилетит из кеша предыдущего теста
    client = TestClient(create_app(config=WebConfig(mongo_uri="", mongo_db=""), db=_TimingOutDB()))
    response = client.get(
        "/api/guild/170000000000000000/leaderboard", params={"period": "7d", "limit": 10}
    )
    queries._leaderboard_cache.clear()
    assert response.status_code == 503
    assert response.headers.get("retry-after") == "5"
    body = response.json()
    assert body["error"] == "query_timeout"
    # текст драйвера наружу не утекает
    assert "budget blown" not in response.text


# ---------------------------------------------------------------- бюджет


def test_timeout_kwargs_and_clamp() -> None:
    limits.set_query_max_time_ms(1)
    assert limits.timeout_kwargs() == {"max_time_ms": 100}
    limits.set_query_max_time_ms(10**9)
    assert limits.timeout_kwargs() == {"max_time_ms": 60_000}
    limits.set_query_max_time_ms("junk")
    assert limits.timeout_kwargs() == {"max_time_ms": limits.QUERY_MAX_TIME_MS_DEFAULT}
    limits.set_query_max_time_ms(7000)
    assert limits.timeout_kwargs() == {"max_time_ms": 7000}


@pytest.fixture(autouse=True)
def _restore_budget() -> None:
    default = limits.QUERY_MAX_TIME_MS_DEFAULT
    yield
    limits.set_query_max_time_ms(default)
