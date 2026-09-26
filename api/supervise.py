"""T12: надзор фоновых задач web-API + измеритель event-loop lag.

Семантика та же, что в боте (voice_tracker/supervise.py): «вечный» цикл под
надзором наблюдаем (структурная строка лога + счётчики в снапшоте) и
перезапускается с экспоненциальным backoff и полным джиттером; критичная задача,
упавшая unhealthy_after раз подряд (каждый раз быстрее healthy_run_seconds),
снимает readiness. Наружу — только имена типов ошибок, никогда содержимое
исключений (в них бывают URI/credentials — R06).
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger("api.supervise")

DEFAULT_BACKOFF_INITIAL_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 60.0
DEFAULT_UNHEALTHY_AFTER = 3
DEFAULT_HEALTHY_RUN_SECONDS = 120.0


def backoff_seconds(
    attempt: int,
    *,
    initial: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
    cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
) -> float:
    base = min(cap, initial * (2 ** max(attempt - 1, 0)))
    return random.uniform(base / 2.0, base)


class TaskHandle:
    __slots__ = (
        "name",
        "critical",
        "restarts",
        "consecutive_failures",
        "last_error_type",
        "running",
        "_task",
    )

    def __init__(self, name: str, critical: bool) -> None:
        self.name = name
        self.critical = critical
        self.restarts = 0
        self.consecutive_failures = 0
        self.last_error_type: str | None = None
        self.running = False
        self._task: asyncio.Task[None] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "critical": self.critical,
            "running": self.running,
            "restarts": self.restarts,
            "consecutiveFailures": self.consecutive_failures,
            "lastErrorType": self.last_error_type,
        }


class Supervisor:
    def __init__(
        self,
        *,
        initial_backoff: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
        backoff_cap: float = DEFAULT_BACKOFF_CAP_SECONDS,
        unhealthy_after: int = DEFAULT_UNHEALTHY_AFTER,
        healthy_run_seconds: float = DEFAULT_HEALTHY_RUN_SECONDS,
    ) -> None:
        self._initial = initial_backoff
        self._cap = backoff_cap
        self._unhealthy_after = max(1, int(unhealthy_after))
        self._healthy_run = healthy_run_seconds
        self._handles: dict[str, TaskHandle] = {}
        self._closed = False

    def spawn(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        *,
        critical: bool = False,
    ) -> TaskHandle:
        if self._closed:
            raise RuntimeError("supervisor is shut down")
        if name in self._handles:
            raise RuntimeError(f"duplicate supervised task name {name!r}")
        handle = TaskHandle(name, critical)
        self._handles[name] = handle
        handle._task = asyncio.create_task(self._run_loop(handle, factory), name=f"supervise:{name}")
        return handle

    def beat(self, name: str) -> None:
        handle = self._handles.get(name)
        if handle is not None:
            handle.consecutive_failures = 0

    async def _run_loop(self, handle: TaskHandle, factory: Callable[[], Awaitable[None]]) -> None:
        attempt = 0
        while True:
            handle.running = True
            started = time.monotonic()
            try:
                await factory()
                raise RuntimeError("supervised loop returned")
            except asyncio.CancelledError:
                handle.running = False
                raise
            except Exception as exc:
                handle.running = False
                handle.restarts += 1
                attempt += 1
                if time.monotonic() - started >= self._healthy_run:
                    handle.consecutive_failures = 1
                else:
                    handle.consecutive_failures += 1
                handle.last_error_type = type(exc).__name__
                logger.warning(
                    "supervise event=task_failed task=%s critical=%s attempt=%s consecutive=%s error=%s",
                    handle.name,
                    handle.critical,
                    attempt,
                    handle.consecutive_failures,
                    handle.last_error_type,
                )
            if self._closed:
                return
            await asyncio.sleep(backoff_seconds(attempt, initial=self._initial, cap=self._cap))

    def unhealthy_tasks(self) -> list[str]:
        return [
            h.name
            for h in self._handles.values()
            if h.critical and h.consecutive_failures >= self._unhealthy_after
        ]

    def snapshot(self) -> list[dict[str, Any]]:
        return [h.snapshot() for h in self._handles.values()]

    async def shutdown(self, timeout: float = 5.0) -> None:
        self._closed = True
        tasks = [h._task for h in self._handles.values() if h._task is not None]
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            logger.warning("supervise event=shutdown_straggler task=%s", task.get_name())
        for task in done:
            if task.cancelled():
                continue
            exc = task.exception()
            if exc is not None:
                logger.warning(
                    "supervise event=shutdown_error task=%s error=%s",
                    task.get_name(),
                    type(exc).__name__,
                )


class LoopMonitor:
    """Event-loop lag: drift реального сна против запрошенного. Зависший/забитый
    sync-кодом loop виден снаружи, а не только по таймаутам запросов."""

    def __init__(self, interval_seconds: float = 1.0) -> None:
        self.interval = interval_seconds
        self.lag_seconds = 0.0
        self.samples = 0

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            await asyncio.sleep(self.interval)
            drift = (time.monotonic() - started) - self.interval
            self.lag_seconds = round(max(0.0, drift), 3)
            self.samples += 1

    def snapshot(self) -> dict[str, Any]:
        return {"lagSeconds": self.lag_seconds, "samples": self.samples}
