"""T16: стоимость запроса под контролем — бюджет времени Mongo + rate-limit дорогих чтений.

Разделение ответственности (п.1 плана):
- ``max_time_ms`` ограничивает РАСХОД одного запроса на сервере: «огромный range»
  превращается в предсказуемый 503 вместо зависшего cursor (L04);
- TokenBucket ограниЧАЕТ ЧАСТОТУ дорогих агрегаций на гильдию: повторный flood
  получает 429 + Retry-After, остальные маршруты продолжают обслуживаться (L04);
- дешёвые индексные чтения (лента чата, страница сессий) бакет не проходят —
  им достаточно cap'ов limit/page из валидаторов.

Значения по умолчанию задаются в create_app из WebConfig (WEB_QUERY_MAX_TIME_MS),
но модуль никогда не остаётся с нулевым бюджетом: незаинициализированное
приложение всё равно ограничивает запросы.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from fastapi import HTTPException, Request

QUERY_MAX_TIME_MS_DEFAULT = 5000
_QUERY_MIN = 100
_QUERY_MAX = 60_000

_query_max_time_ms = QUERY_MAX_TIME_MS_DEFAULT


def set_query_max_time_ms(value: int) -> None:
    global _query_max_time_ms
    try:
        ms = int(value)
    except (TypeError, ValueError):
        ms = QUERY_MAX_TIME_MS_DEFAULT
    _query_max_time_ms = max(_QUERY_MIN, min(ms, _QUERY_MAX))


def query_max_time_ms() -> int:
    return _query_max_time_ms


def timeout_kwargs() -> dict[str, int]:
    """kwargs для find/find_one: опция курсора, драйвер сам конвертирует её в
    командное поле maxTimeMS (camelCase курсор не принимает — TypeError)."""
    return {"max_time_ms": _query_max_time_ms}


def command_timeout_kwargs() -> dict[str, int]:
    """kwargs для aggregate/count_documents: это команды, а не курсоры — им нужно
    серверное maxTimeMS-поле; snake_case улетает в BSON как неизвестное поле
    и сервер отвечает IDLUnknownField (проверено на стенде)."""
    return {"maxTimeMS": _query_max_time_ms}


class TokenBucket:
    """Потокобезопасный бакет: sync-хендлеры FastAPI исполняет в threadpool.

    L03: состояние ограничено ``max_keys`` с LRU-вытеснением — ключом может быть
    IP клиента (анонимный fallback), поэтому словарь не должен расти вместе с
    числом уникальных ключей.
    """

    def __init__(self, capacity: float, refill_per_s: float, *, max_keys: int = 1024) -> None:
        self.capacity = float(capacity)
        self.refill = float(refill_per_s)
        self.max_keys = int(max_keys)
        self._state: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def _remember(self, key: str, tokens: float, moment: float) -> None:
        self._state.pop(key, None)  # обновляем свежесть для LRU-порядка dict
        self._state[key] = (tokens, moment)
        while len(self._state) > self.max_keys:
            self._state.pop(next(iter(self._state)))

    def allow(self, key: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            tokens, last = self._state.get(key, (self.capacity, moment))
            tokens = min(self.capacity, tokens + (moment - last) * self.refill)
            if tokens < 1:
                self._remember(key, tokens, moment)
                return False
            self._remember(key, tokens - 1, moment)
            return True

    def retry_after(self, key: str) -> int:
        with self._lock:
            tokens, last = self._state.get(key, (self.capacity, time.monotonic()))
        tokens = min(self.capacity, tokens + (time.monotonic() - last) * self.refill)
        if tokens >= 1:
            return 1
        return max(1, int(round((1 - tokens) / self.refill)))

    def clear(self) -> None:
        with self._lock:
            self._state.clear()


_buckets: dict[str, TokenBucket] = {}
_buckets_lock = threading.Lock()


def configure(name: str, *, capacity: float, refill_per_s: float) -> TokenBucket:
    bucket = TokenBucket(capacity, refill_per_s)
    with _buckets_lock:
        _buckets[name] = bucket
    return bucket


def bucket(name: str) -> TokenBucket:
    with _buckets_lock:
        found = _buckets.get(name)
    if found is None:
        raise KeyError(f"rate bucket not configured: {name}")
    return found


def reset_all() -> None:
    with _buckets_lock:
        for b in _buckets.values():
            b.clear()


# Бюджеты дорогих чтений на гильдию: агрегации по всей коллекции дороже десятков
# индексных выборок, поэтому поток обновлений экрана сбрасывается в 429 раньше,
# чем он успеет съесть очередь запросов.
_EXPENSIVE_READS = (
    ("leaderboard", 12, 2.0),
    ("chat-leaderboard", 12, 2.0),
    # лента — основной экран: индексная выборка дешёвая, но regex-фильтры
    # (type=link/text) сканируют; бакет щедрее, чем у агрегаций
    ("chat", 30, 5.0),
    ("names", 10, 1.0),
    ("invites", 10, 1.0),
    ("audit", 12, 2.0),
    ("audit-sync", 2, 1 / 30),
)


def setup_defaults() -> None:
    """Идемпотентно: create_app может вызываться несколько раз за процесс (тесты),
    накопленное состояние бакетов при этом сохраняется."""
    for name, capacity, rate in _EXPENSIVE_READS:
        with _buckets_lock:
            if name not in _buckets:
                _buckets[name] = TokenBucket(capacity, rate)


def gate(name: str, *, per: str = "guild") -> Callable[[Request], None]:
    """Dependency для дорогих read-эндпоинтов: превышение бюджета — 429+Retry-After."""

    def _dep(request: Request) -> None:
        found = bucket(name)
        if per == "guild":
            key = request.path_params.get("guildId", "")
        else:
            session = getattr(request, "session", None) or {}
            user = str((session.get("discord") or {}).get("userId") or "")
            client = request.client.host if request.client else ""
            key = user or client
        if not found.allow(key):
            raise HTTPException(
                status_code=429,
                detail="too many expensive requests for this guild, retry shortly",
                headers={"Retry-After": str(found.retry_after(key))},
            )

    return _dep
