"""T12: readiness-проверки с ограниченным ожиданием + диагностический снапшот.

Разделение контрактов (п.1 плана):
- liveness (`/api/healthz`) — процесс обслуживает запросы, зависит только от loop;
- readiness (`/api/readyz`) — готовности выполнять работу: Mongo пингуется в
  пределах timeout клиента, схема web-контракта сходится, media mount на месте
  (если настроен), критичные supervised-циклы не умерли nasмерть.

Отказ внешнего Discord в readiness не входит (п.2): обычный rate limit не должен
снимать readiness и провоцировать рестарты; его честный статус —
/api/audit/discord/status.

Наружу отдаются только булевы исходы, имена типов ошибок и счётчики — никогда
URI/credentials/содержимое (R06).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("api.readiness")

MEDIA_CHECK_NAME = "media"
SCHEMA_RECHECK_INTERVAL_S = 60.0
DIAGNOSTICS_INTERVAL_S = 60.0


def mongo_ping(mongo_client: Any) -> dict[str, Any]:
    if mongo_client is None:
        return {"ok": False, "error": "not-configured"}
    try:
        mongo_client.admin.command("ping")
        return {"ok": True}
    except Exception as exc:  # serverSelectionTimeoutMS ограничивает ожидание
        return {"ok": False, "error": type(exc).__name__}


def check_media(media_dir: str, min_free_bytes: int = 0) -> dict[str, Any]:
    if not media_dir:
        return {"configured": False, "ok": True}
    try:
        ok = os.path.isdir(media_dir) and os.access(media_dir, os.R_OK | os.X_OK)
    except OSError:
        ok = False
    out: dict[str, Any] = {"configured": True, "ok": bool(ok)}
    if ok:
        # T16 (L05): место под архив кончается — предупреждаем заранее. На
        # готовность не влияем: чтение/отдача существующего архива работают,
        # а ограничение новых загрузок — ответственность писателя (bot gateway).
        try:
            free = shutil.disk_usage(media_dir).free
            out["freeMB"] = free // (1024 * 1024)
            if min_free_bytes > 0:
                out["quotaLow"] = free < min_free_bytes
        except OSError as exc:
            out["diskError"] = type(exc).__name__
    # путь наружу не отдаём
    return out


def check_schema(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return {"ok": True, "checked": False}
    ok = bool(report.get("skipped") or report.get("ok"))
    out: dict[str, Any] = {"ok": ok, "checked": True}
    missing = report.get("missing")
    if missing:
        out["missingCount"] = len(missing)
    return out


def evaluate(app: Any) -> tuple[bool, dict[str, Any]]:
    """(ready, payload) — payload пригоден и для 200, и для 503."""
    cfg = app.state.config
    checks = {
        "mongo": mongo_ping(getattr(app.state, "mongo", None)),
        "schema": check_schema(getattr(app.state, "schema_report", None)),
        "media": check_media(cfg.media_dir, getattr(cfg, "media_min_free_bytes", 0)),
    }
    supervisor = getattr(app.state, "supervisor", None)
    unhealthy = supervisor.unhealthy_tasks() if supervisor is not None else []
    reasons: list[str] = []
    for name in ("mongo", "schema", "media"):
        if not checks[name]["ok"]:
            reasons.append(name)
    reasons.extend(f"task:{task}" for task in unhealthy)
    payload: dict[str, Any] = {
        "status": "ready" if not reasons else "not_ready",
        "checks": checks,
        "unhealthyTasks": unhealthy,
    }
    if checks["media"].get("quotaLow"):
        # предупреждение, не отказ: готовность сохраняется (см. check_media)
        payload["warnings"] = ["media disk quota low"]
    monitor = getattr(app.state, "loop_monitor", None)
    if monitor is not None:
        payload["loop"] = monitor.snapshot()
    return (not reasons), payload


def audit_overview(db: Any) -> dict[str, Any]:
    """Одна агрегирующая строка по discord_audit_state для лога диагностики."""
    from . import audit_sync
    from .limits import timeout_kwargs

    now = datetime.now(timezone.utc)
    guilds = 0
    complete = 0
    denied = 0
    errored = 0
    oldest_success_age: float | None = None
    oldest_backfill_age: float | None = None
    try:
        for doc in db[audit_sync.COLL_AUDIT_STATE].find(
            {},
            {
                "guildId": 1,
                "backfillComplete": 1,
                "accessDenied": 1,
                "lastError": 1,
                "lastSuccessAt": 1,
                "updatedAt": 1,
            },
            **timeout_kwargs(),
        ):
            guilds += 1
            if doc.get("backfillComplete"):
                complete += 1
            else:
                touched = audit_sync._as_utc(doc.get("updatedAt"))
                if touched is not None:
                    age = (now - touched).total_seconds()
                    if oldest_backfill_age is None or age > oldest_backfill_age:
                        oldest_backfill_age = age
            if doc.get("accessDenied"):
                denied += 1
            if doc.get("lastError"):
                errored += 1
            last = audit_sync._as_utc(doc.get("lastSuccessAt"))
            if last is None:
                oldest_success_age = float("inf")
            else:
                age = (now - last).total_seconds()
                if oldest_success_age != float("inf") and (
                    oldest_success_age is None or age > oldest_success_age
                ):
                    oldest_success_age = age
    except Exception as exc:  # noqa: BLE001 — диагностика не должна ронять цикл
        return {"error": type(exc).__name__}
    return {
        "guilds": guilds,
        "backfillComplete": complete,
        "accessDenied": denied,
        "withLastError": errored,
        "oldestSuccessAgeS": None if oldest_success_age is None else (
            "never" if oldest_success_age == float("inf") else int(oldest_success_age)
        ),
        "oldestIncompleteAgeS": None if oldest_backfill_age is None else int(oldest_backfill_age),
    }


def media_disk(media_dir: str) -> dict[str, Any]:
    if not media_dir or not os.path.isdir(media_dir):
        return {}
    try:
        usage = shutil.disk_usage(media_dir)
    except OSError as exc:
        return {"error": type(exc).__name__}
    return {"mediaFreeMB": usage.free // (1024 * 1024)}


async def diagnostics_loop(app: Any) -> None:
    """Раз в минуту — одна структурированная строка с состоянием всех зависимостей
    и журнала аудита: по ней видно деградацию, не роясь в тысячах строк лога."""
    while True:
        await asyncio.sleep(DIAGNOSTICS_INTERVAL_S)
        # T16 п.3: evaluate/audit_overview/media_disk — синхронные Mongo-чтения и
        # statvfs диска; в event loop их держать нельзя (тот же процесс обслуживает
        # HTTP) — уходят в thread (L06).
        def _collect() -> tuple[bool, dict[str, Any], dict[str, Any]]:
            ready, payload = evaluate(app)
            fields: dict[str, Any] = {"ready": ready}
            db = getattr(app.state, "db", None)
            if db is not None:
                fields.update(audit_overview(db))
                fields.update(media_disk(app.state.config.media_dir))
            return ready, payload, fields

        _, _, fields = await asyncio.to_thread(_collect)
        supervisor = getattr(app.state, "supervisor", None)
        if supervisor is not None:
            fields["tasks"] = supervisor.snapshot()
        summary = " ".join(f"{k}={v}" for k, v in fields.items() if v not in (None, {}, []))
        logger.info("diagnostics %s", summary)
