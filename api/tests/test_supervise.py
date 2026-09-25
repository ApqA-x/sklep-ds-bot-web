"""T12: надзор фоновых задач web-API (respawn, backoff+jitter, readiness-вентиль)."""
from __future__ import annotations

import asyncio

import pytest

from api.supervise import LoopMonitor, Supervisor, backoff_seconds


def _run(coro):
    return asyncio.run(coro)


def test_backoff_grows_and_caps() -> None:
    samples = [backoff_seconds(i, initial=1.0, cap=8.0) for i in range(1, 12)]
    for attempt, value in enumerate(samples, start=1):
        base = min(8.0, 1.0 * 2 ** (attempt - 1))
        assert base / 2 <= value <= base, (attempt, value)
    assert max(samples) <= 8.0


def test_backoff_has_jitter() -> None:
    values = {backoff_seconds(4) for _ in range(20)}
    assert len(values) > 1  # не детерминированная кривая — иначе restart storm синхронизируется


def test_failing_task_is_respawned_and_marked_unhealthy() -> None:
    async def scenario() -> None:
        sup = Supervisor(initial_backoff=0.01, backoff_cap=0.02, unhealthy_after=3)
        calls = {"n": 0}

        async def dying() -> None:
            calls["n"] += 1
            raise RuntimeError("boom")

        sup.spawn("critical-loop", dying, critical=True)
        await asyncio.sleep(0.5)
        assert calls["n"] >= 3
        assert sup.unhealthy_tasks() == ["critical-loop"]
        snap = sup.snapshot()[0]
        assert snap["lastErrorType"] == "RuntimeError"
        assert snap["restarts"] >= 3
        await sup.shutdown()
        return calls["n"]

    n = _run(scenario())
    assert n >= 3


def test_loop_returning_normally_counts_as_failure() -> None:
    async def scenario() -> None:
        sup = Supervisor(initial_backoff=0.01, backoff_cap=0.02, unhealthy_after=2)

        async def quits() -> None:
            return  # «вечный» цикл завершился сам — это аномалия

        sup.spawn("quitter", quits, critical=True)
        await asyncio.sleep(0.3)
        assert sup.unhealthy_tasks() == ["quitter"]
        await sup.shutdown()

    _run(scenario())


def test_beat_clears_failures_and_noncritical_never_gates() -> None:
    async def scenario() -> None:
        sup = Supervisor(initial_backoff=0.01, backoff_cap=0.02, unhealthy_after=2)

        async def noisy() -> None:
            sup.beat("noisy-loop")
            raise RuntimeError("boom")

        sup.spawn("noisy-loop", noisy, critical=False)
        await asyncio.sleep(0.3)
        assert sup.unhealthy_tasks() == []  # не критично — readiness не снимаем
        # beat() на каждом запуске сбрасывает серию → после падения всегда 1
        assert sup._handles["noisy-loop"].consecutive_failures == 1
        await sup.shutdown()

    _run(scenario())


def test_healthy_long_run_reopens_failure_series() -> None:
    async def scenario() -> None:
        sup = Supervisor(initial_backoff=0.05, backoff_cap=0.05, unhealthy_after=2,
                         healthy_run_seconds=0.05)
        state = {"ticks": 0}
        gate = asyncio.Event()

        async def flapper() -> None:
            state["ticks"] += 1
            if state["ticks"] <= 2:
                raise RuntimeError("early boom")  # серия копится: 1, 2 → unhealthy
            if state["ticks"] == 3:
                await asyncio.sleep(0.2)  # прожил дольше healthy_run
                raise RuntimeError("late boom")  # → серия начинается с 1
            await gate.wait()  # «выздоровел» — крутится вечно

        sup.spawn("flapper", flapper, critical=True)
        handle = sup._handles["flapper"]
        # фаза 1: два быстрых отказа подряд → критичная задача считается мёртвой
        for _ in range(200):
            if sup.unhealthy_tasks() == ["flapper"]:
                break
            await asyncio.sleep(0.02)
        assert sup.unhealthy_tasks() == ["flapper"]
        # фаза 2: поздний отказ после долгой жизни не накапливает серию
        for _ in range(200):
            if state["ticks"] >= 4:
                break
            await asyncio.sleep(0.02)
        assert state["ticks"] >= 4
        assert handle.consecutive_failures <= 1
        assert sup.unhealthy_tasks() == []  # readiness восстановлен самим циклом
        gate.set()
        await sup.shutdown()

    _run(scenario())


def test_shutdown_cancels_awaits_and_blocks_new_spawns() -> None:
    async def scenario() -> None:
        sup = Supervisor()
        state = {"cleaned": False}

        async def worker() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)  # имитация drain-работы
                state["cleaned"] = True

        sup.spawn("worker", worker)
        await asyncio.sleep(0.05)
        await sup.shutdown(timeout=2.0)
        assert state["cleaned"] is True
        assert sup.snapshot()[0]["running"] is False
        with pytest.raises(RuntimeError):
            sup.spawn("late", worker)

    _run(scenario())


def test_duplicate_spawn_rejected() -> None:
    async def scenario() -> None:
        sup = Supervisor()

        async def loop() -> None:
            await asyncio.Event().wait()

        sup.spawn("dup", loop)
        with pytest.raises(RuntimeError):
            sup.spawn("dup", loop)
        await sup.shutdown()

    _run(scenario())


def test_loop_monitor_measures_samples() -> None:
    async def scenario() -> None:
        sup = Supervisor()
        monitor = LoopMonitor(interval_seconds=0.02)
        sup.spawn("monitor", monitor.run)
        await asyncio.sleep(0.15)
        snap = monitor.snapshot()
        assert snap["samples"] >= 2
        assert snap["lagSeconds"] >= 0.0
        await sup.shutdown()

    _run(scenario())
