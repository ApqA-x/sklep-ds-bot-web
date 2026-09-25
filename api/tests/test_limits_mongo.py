"""T16 L01/L04 на реальной Mongo стенда: maxTimeMS действительно исполняется.

Unit-фейки терпят любые kwargs (**kw) — только живой сервер доказывает, что
1) командное camelCase-поле maxTimeMS принимается aggregate/count_documents
   (snake_case сервер отвергает IDLUnknownField — этот баг фейки прятали),
2) превышение бюджета превращается драйвером в ExecutionTimeout,
3) приложение отображает его в предсказуемый 503 query_timeout + Retry-After,
   а не в зависшую загрузку UI (L04).

Медленная выборка строится детерминированно: self-$lookup без локального
индекса — внешние документы сканируют коллекцию целиком → O(K²) работы при
линейном выходе (без $unwind).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pymongo = pytest.importorskip("pymongo")
from fastapi.testclient import TestClient  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from api import limits, queries
from api.config import WebConfig
from api.main import create_app
from stand_guard import guard_db_name, guard_mongo_uri  # noqa: E402

pytestmark = pytest.mark.integration

URI = os.environ.get("TEST_MONGO_URI", "mongodb://127.0.0.1:27099")
GUILD = "170000000000000000"
CH = "140000000000000000"
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
K = 4000  # (K/2) × K ≈ 8M сканов: на стенде полный проход 1.5 s ≫ бюджета 100 ms (замер 2026-09-25)


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
    client = MongoClient(URI, serverSelectionTimeoutMS=3000, socketTimeoutMS=120_000)
    name = f"voice_tracker_t16_test_{uuid.uuid4().hex[:8]}"
    guard_db_name(name)
    db = client[name]
    db["chat_messages"].insert_many([
        {
            "guildId": GUILD,
            "channelId": CH,
            "messageId": str(1800000000000000000 + i),
            "authorUserId": str(1600000000000000000 + (i % 40)),
            "authorName": f"u{i % 40}",
            "content": f"m{i}",
            "sentAt": T0 + timedelta(minutes=i),
            "deletedAt": None,
        }
        for i in range(K)
    ])
    yield db
    client.drop_database(name)
    client.close()


def _slow_pipeline() -> list[dict]:
    return [
        {"$match": {"guildId": GUILD}},
        {"$limit": K // 2},
        {
            "$lookup": {
                "from": "chat_messages",
                "localField": "authorUserId",
                "foreignField": "guildId",  # selectivity ≈ K → квадратичный скан
                "as": "join",
            }
        },
        # без $unwind: стоимость квадратичная, выход линейный — время теста предсказуемо
        {"$group": {"_id": "$authorUserId", "total": {"$sum": {"$size": "$join"}}}},
        {"$sort": {"total": -1}},
        {"$limit": 5},
    ]


def test_maxtimems_enforced_by_real_server(db) -> None:
    with pytest.raises(pymongo.errors.ExecutionTimeout):
        list(db["chat_messages"].aggregate(_slow_pipeline(), maxTimeMS=5))


def test_big_budget_lets_same_query_finish(db) -> None:
    # анти-дрейф: таймаут — следствие бюджета, а не сломавшейся выборки
    rows = list(db["chat_messages"].aggregate(_slow_pipeline(), maxTimeMS=60_000))
    assert rows


def test_count_documents_accepts_command_maxtimems(db) -> None:
    assert db["chat_messages"].count_documents({"guildId": GUILD}, maxTimeMS=60_000) == K
    with pytest.raises(pymongo.errors.ExecutionTimeout):
        db["chat_messages"].count_documents({"guildId": GUILD}, maxTimeMS=1)


def test_event_loop_stays_responsive_during_slow_db(db, monkeypatch) -> None:
    # L06/п.3: sync pymongo из async-хендлеров уходит в asyncio.to_thread. Здесь
    # ровно эта форма вызова (production-функция queries.chat_leaderboard в
    # потоке) — loop обязан продолжать тикать, пока поток ждёт медленную Mongo.
    # Если кто-вернёт такой вызов инлайном в event loop, тикеры замрут и тест
    # упадёт: единственный «тяжёлый» запрос стопил бы healthz/readiness всего
    # сервиса.
    import asyncio

    monkeypatch.setattr(queries, "build_chat_leaderboard_pipeline",
                        lambda *a, **k: _slow_pipeline())
    queries._chat_leaderboard_cache.clear()
    client = MongoClient(URI, serverSelectionTimeoutMS=3000, socketTimeoutMS=120_000)
    live_db = client[db.name]
    try:
        async def scenario():
            ticks = 0
            stopping = False

            async def ticker():
                nonlocal ticks
                while not stopping:
                    await asyncio.sleep(0.005)
                    ticks += 1

            task = asyncio.create_task(ticker())
            loop = asyncio.get_running_loop()
            started = loop.time()
            await asyncio.to_thread(
                queries.chat_leaderboard, live_db, GUILD, "30d", 10, 1, ""
            )
            elapsed = loop.time() - started
            stopping = True
            await task
            return ticks, elapsed

        ticks, elapsed = asyncio.run(scenario())
        assert elapsed > 0.3, "запрос должен быть ощутимо медленным, иначе тест ничего не доказывает"
        # свободный loop оттикал бы ~elapsed/5ms; жёсткий порог 30% отсекает
        # флуктуации планировщика Windows, но ловит полную блокировку (≈0%)
        assert ticks >= (elapsed / 0.005) * 0.3, f"loop блокировался: ticks={ticks}, elapsed={elapsed:.2f}s"
    finally:
        queries._chat_leaderboard_cache.clear()
        client.close()


def test_api_maps_real_timeout_to_503(db, monkeypatch) -> None:
    # маршрут/кэш-ключ/драйвер/сервер/обработчик — настоящие; замедляется только
    # пайплайн (его выбор — предмет test_queries, а не контракта 503)
    monkeypatch.setattr(queries, "build_chat_leaderboard_pipeline",
                        lambda *a, **k: _slow_pipeline())
    queries._chat_leaderboard_cache.clear()
    cfg = WebConfig(mongo_uri="", mongo_db="", query_max_time_ms=100)
    client = MongoClient(URI, serverSelectionTimeoutMS=3000, socketTimeoutMS=120_000)
    try:
        app = create_app(config=cfg, db=client[db.name])
        test = TestClient(app)
        try:
            response = test.get(
                f"/api/guild/{GUILD}/chat-leaderboard", params={"period": "30d", "limit": 10}
            )
        finally:
            queries._chat_leaderboard_cache.clear()
            limits.set_query_max_time_ms(limits.QUERY_MAX_TIME_MS_DEFAULT)
        assert response.status_code == 503, response.text[:200]
        assert response.headers.get("retry-after") == "5"
        body = response.json()
        assert body["error"] == "query_timeout"
        assert "budget" in body["detail"]
    finally:
        client.close()
