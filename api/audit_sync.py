"""Копия журнала аудита Discord с честной полнотой архива (T11).

Discord отдаёт audit log ~45 дней и не больше 100 000 записей — постоянная локальная
копия в discord_audit_logs. Полнота хранится отдельно в discord_audit_state
(по одному документу на гильдию, unique(guildId) — индекс M4 runner'а):

- freshCursor    — максимальный сохранённый entryId («всё не старше здесь — наше»);
- backfillCursor — позиция (entryId для before=), откуда история ещё не проверена;
- backfillComplete — True ТОЛЬКО когда сканирование упёрлось в пустую страницу
  Discord; частичная страница (<100) полноту не объявляет (H06),
- lastSuccessAt / lastError{status,at} / accessDenied — наблюдаемость для UI,
- leaseOwner / leaseExpiresAt — координация между web-worker'ами (H08).

Каждый проход: (1) свежее окно — верхние 100 доступных записей; если окно полное и
его низ не смыкается с freshCursor, между ними возможен пропуск (>100 событий/мин),
и скан возобновляется от низа окна; (2) scan-проход before= назад до пустой страницы
или до page-budget тика — то есть прерванный backfill продолжается с checkpoint,
а не начинается заново и не ограничивается первыми 500 строками (H06). Курсор
передвигается только после устойчивого сохранения всей страницы: upsert до записи
курсора идемпотентен ($setOnInsert + unique (guildId, entryId) из M3), поэтому сбой
между «страница сохранена» и «checkpoint записан» приводит лишь к повторному
чтению той же страницы без потерь и дублей (H07).

Доступность `after=` у audit-logs не документирована, поэтому свежее чтение опирается
только на негомотированное верхнее окно + смычку, а не на неизвестный параметр.

Координация: per-guild asyncio.Lock внутри процесса + Mongo lease (CAS по
discord_audit_state) между процессами; фоновый тик, застигнутый ручной синхронизацией,
получает skipped без гонки. 403 помечает accessDenied, 429/прочие — lastError;
tight retry нет: следующий тик не раньше SYNC_INTERVAL_S (H09).
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any

from .queries import COLL_GUILD_SETTINGS

COLL_DISCORD_AUDIT = "discord_audit_logs"
COLL_AUDIT_STATE = "discord_audit_state"
SYNC_INTERVAL_S = 60
PAGE_LIMIT = 100
SCAN_PAGES_PER_TICK = 20  # бюджет history-страниц за один проход (окно считается отдельно)
MANUAL_SCAN_PAGES = 60  # явная кнопка «обновить» тянет заметно больше фона
LEASE_TTL_S = 300
STALE_AFTER_S = SYNC_INTERVAL_S * 5

log = logging.getLogger("audit_sync")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Any) -> datetime | None:
    # BSON datetime под tz_aware=False приходит naive (урок T09) — нормализуем
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return None


def _sid(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _iso(value: Any) -> str | None:
    dt = _as_utc(value)
    return dt.isoformat() if dt else None


def _to_doc(guild: str, entry: dict[str, Any]) -> dict[str, Any]:
    at: datetime | None = None
    if entry.get("at"):
        try:
            at = datetime.fromisoformat(str(entry["at"]).replace("Z", "+00:00"))
        except ValueError:
            at = None
    return {
        "guildId": guild,
        "entryId": str(entry["id"]),
        "at": at,
        "actionType": int(entry.get("actionType") or 0),
        "action": str(entry.get("action") or ""),
        "actorUserId": str(entry.get("actorUserId") or ""),
        "actorName": str(entry.get("actorName") or ""),
        "targetUserId": str(entry.get("targetUserId") or ""),
        "targetUserName": str(entry.get("targetUserName") or ""),
        "targetId": str(entry.get("targetId") or ""),
        "channelId": str(entry.get("channelId") or ""),
        "count": entry.get("count"),
        "deleteMessageDays": entry.get("deleteMessageDays"),
        "reason": str(entry.get("reason") or ""),
        "changes": entry.get("changes") or [],
        "options": entry.get("options") or {},
        "syncedAt": _now(),
    }


def _entries(payload: Any) -> list[dict[str, Any]]:
    from . import discord_api

    body = payload if isinstance(payload, dict) else {}
    return discord_api.build_audit_log_entries(
        list(body.get("audit_log_entries") or []), list(body.get("users") or [])
    )


def _store(db: Any, guild: str, entries: list[dict[str, Any]]) -> int:
    coll = db[COLL_DISCORD_AUDIT]
    new = 0
    for entry in entries:
        doc = _to_doc(guild, entry)
        res = coll.update_one(
            {"guildId": guild, "entryId": doc["entryId"]},
            {"$setOnInsert": doc},
            upsert=True,
        )
        if res.upserted_id is not None:
            new += 1
    return new


# ------------------------------------------------------------------ состояние


def _scan_bounds(db: Any, guild: str) -> tuple[str, str]:
    """(max entryId, min entryId) уже сохранённого — bootstrap для установок, где
    состояние заводилось старым кодом. Полный проход один раз на гильдию."""
    head = low = 0
    for doc in db[COLL_DISCORD_AUDIT].find({"guildId": guild}, {"entryId": 1}):
        v = _sid(doc.get("entryId"))
        if v:
            head = max(head, v)
            low = v if not low else min(low, v)
    return (str(head) if head else "", str(low) if low else "")


def load_state(db: Any, guild: str) -> dict[str, Any]:
    """Документ состояния; при отсутствии — bootstrap из фактических данных.
    backfillComplete=False всегда для старых установок: прежний код тянул максимум
    5 страниц, «полный архив» по ним не доказан (H06)."""
    coll = db[COLL_AUDIT_STATE]
    doc = coll.find_one({"guildId": guild})
    if doc:
        return doc
    head, low = _scan_bounds(db, guild)
    state = {
        "guildId": guild,
        "freshCursor": head,
        "backfillCursor": low,
        "backfillComplete": False,
        "accessDenied": False,
        "lastSuccessAt": None,
        "lastError": None,
        "leaseOwner": "",
        "leaseExpiresAt": None,
    }
    coll.update_one({"guildId": guild}, {"$setOnInsert": state}, upsert=True)
    return coll.find_one({"guildId": guild}) or state


def _write_state(db: Any, guild: str, patch: dict[str, Any], *, owner: str = "") -> None:
    fields = dict(patch)
    fields["updatedAt"] = _now()
    if owner:
        # heartbeat: держатель продлевает lease на каждом сохранённом чекпоинте
        fields["leaseOwner"] = owner
        fields["leaseExpiresAt"] = _now() + timedelta(seconds=LEASE_TTL_S)
    db[COLL_AUDIT_STATE].update_one({"guildId": guild}, {"$set": fields})


def _owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _acquire_lease(db: Any, guild: str, owner: str) -> bool:
    """CAS-перехват: свободен / истёк / принадлежит нам (после краша без release).
    return_document=True — это pymongo ReturnDocument.AFTER (без импорта драйвера)."""
    now = _now()
    taken = db[COLL_AUDIT_STATE].find_one_and_update(
        {"guildId": guild,
         "$or": [{"leaseOwner": owner},
                 {"leaseExpiresAt": {"$lte": now}},
                 {"leaseExpiresAt": None}]},
        {"$set": {"leaseOwner": owner, "leaseExpiresAt": now + timedelta(seconds=LEASE_TTL_S)}},
        return_document=True,
    )
    return bool(taken) and taken.get("leaseOwner") == owner


def _release_lease(db: Any, guild: str, owner: str) -> None:
    db[COLL_AUDIT_STATE].update_one(
        {"guildId": guild, "leaseOwner": owner},
        {"$set": {"leaseOwner": "", "leaseExpiresAt": _now()}},
    )


# ------------------------------------------------------------------ синхронизация

_guild_locks: dict[str, asyncio.Lock] = {}


def _lock_for(guild: str) -> asyncio.Lock:
    lock = _guild_locks.get(guild)
    if lock is None:
        lock = _guild_locks.setdefault(guild, asyncio.Lock())
    return lock


async def sync_guild(db: Any, cfg: Any, guild: str, *, scan_pages: int = SCAN_PAGES_PER_TICK) -> dict[str, Any]:
    """Один скоординированный проход для гильдии (общий путь фона и ручной кнопки)."""
    if not cfg.discord_token:
        return {"ok": False, "discordStatus": 0, "inserted": 0}
    async with _lock_for(guild):
        return await _sync_locked(db, cfg, guild, scan_pages)


async def _sync_locked(db: Any, cfg: Any, guild: str, scan_pages: int) -> dict[str, Any]:
    from . import discord_api

    # состояние существует до захвата lease: CAS-апдейт не создаёт документов
    load_state(db, guild)
    owner = _owner_id()
    if not _acquire_lease(db, guild, owner):
        return {"ok": True, "skipped": "lease-held", "discordStatus": 200, "inserted": 0,
                **status_snapshot(db, guild)}
    try:
        return await _run(db, cfg, guild, owner, scan_pages, discord_api)
    finally:
        _release_lease(db, guild, owner)


async def _run(db: Any, cfg: Any, guild: str, owner: str, scan_pages: int, discord_api: Any) -> dict[str, Any]:
    state = load_state(db, guild)
    head = _sid(state.get("freshCursor"))
    cursor = _sid(state.get("backfillCursor"))
    complete = bool(state.get("backfillComplete"))
    inserted = 0

    async def fetch(path: str) -> tuple[int, Any]:
        return await discord_api.bot_request(cfg, "GET", f"/guilds/{guild}/audit-logs{path}")

    async def fail(status: int) -> dict[str, Any]:
        patch: dict[str, Any] = {"lastError": {"status": int(status), "at": _now()}}
        if status == 403:
            patch["accessDenied"] = True
            log.info("audit sync guild=%s: нет доступа к журналу (403)", guild)
        else:
            log.warning("audit sync guild=%s: discord status %s", guild, status)
        _write_state(db, guild, patch)
        return {"ok": False, "discordStatus": int(status), "inserted": inserted,
                **status_snapshot(db, guild)}

    # 1) свежее окно: верх доступной истории (без after= — параметр не документирован)
    status, payload = await fetch(f"?limit={PAGE_LIMIT}")
    if status != 200:
        return await fail(status)
    entries = _entries(payload)
    # успешный ответ Discord сам по себе факт успеха: снимаем accessDenied/lastError,
    # иначе гильдия с пустым журналом навечно осталась бы «отстающей»
    patch: dict[str, Any] = {"lastSuccessAt": _now(), "accessDenied": False, "lastError": None}
    if entries:
        inserted += _store(db, guild, entries)  # вся страница сохранена — теперь можно чекпоинт (H07)
        ids = [_sid(e.get("id")) for e in entries]
        wmin, wmax = min(ids), max(ids)
        patch["freshCursor"] = str(max(head, wmax))
        if len(entries) == PAGE_LIMIT and wmin > head:
            # окно не смыкается с сохранённым: между head и wmin возможны пропущенные
            # (>100 новых событий с прошлого прохода) — скан возобновляется от низа окна
            patch["backfillCursor"] = str(wmin)
            patch["backfillComplete"] = False
            cursor, complete = wmin, False
        elif not cursor:
            patch["backfillCursor"] = str(wmin)
            cursor = wmin
        head = max(head, wmax)
    elif not head and not cursor:
        # пустое верхнее окно при пустой базе: записей нет вообще — сканировать нечего,
        # полнота доказана тем же сигналом, что и в scan-проходе (пустая страница)
        complete = True
        patch["backfillComplete"] = True
    # head = максимальный сохранённый entryId; инвариант: (backfillCursor, head] сохранён,
    # ниже backfillCursor — неизвестно (перескан после reopen идемпотентен через dedup)
    _write_state(db, guild, patch, owner=owner)

    # 2) scan/backfill: от checkpoint назад, пока Discord отдаёт непустые страницы
    pages = 0
    while not complete and cursor and pages < scan_pages:
        status, payload = await fetch(f"?limit={PAGE_LIMIT}&before={cursor}")
        if status != 200:
            return await fail(status)
        pages += 1
        entries = _entries(payload)
        if not entries:
            # единственный честный сигнал «досканили до низа доступной истории» (H06)
            complete = True
            _write_state(db, guild, {
                "backfillComplete": True, "lastSuccessAt": _now(),
                "accessDenied": False, "lastError": None,
            }, owner=owner)
            break
        inserted += _store(db, guild, entries)
        new_min = min(_sid(e.get("id")) for e in entries)
        patch = {"backfillCursor": str(new_min), "lastSuccessAt": _now(),
                 "accessDenied": False, "lastError": None}
        if new_min >= cursor:
            # before= обязан отдавать строго более старых; нет движения → стоп без
            # объявления полноты (защита от зацикливания на аномальном ответе)
            _write_state(db, guild, patch, owner=owner)
            break
        cursor = new_min
        _write_state(db, guild, patch, owner=owner)

    return {"ok": True, "discordStatus": 200, "inserted": inserted,
            **status_snapshot(db, guild)}


# ------------------------------------------------------------------ наблюдаемость


def status_snapshot(db: Any, guild: str) -> dict[str, Any]:
    """Честная сводка для UI/эндпоинта: полнота, задержка, ошибки, доступ."""
    doc = db[COLL_AUDIT_STATE].find_one({"guildId": guild}) or {}
    total = int(db[COLL_DISCORD_AUDIT].count_documents({"guildId": guild}))
    last = _as_utc(doc.get("lastSuccessAt"))
    age = (_now() - last).total_seconds() if last else None
    access_denied = bool(doc.get("accessDenied"))
    error = doc.get("lastError") if isinstance(doc.get("lastError"), dict) else None
    return {
        "backfillComplete": bool(doc.get("backfillComplete")),
        "freshCursor": str(doc.get("freshCursor") or ""),
        "backfillCursor": str(doc.get("backfillCursor") or ""),
        "accessDenied": access_denied,
        "lastSuccessAt": _iso(doc.get("lastSuccessAt")),
        "lastError": ({"status": int(_sid(error.get("status"))), "at": _iso(error.get("at"))}
                      if error else None),
        "storedEntries": total,
        "secondsSinceSuccess": round(age, 1) if age is not None else None,
        "syncStale": bool(not access_denied and (age is None or age > STALE_AFTER_S)),
        "syncIntervalS": SYNC_INTERVAL_S,
    }


def known_guilds(db: Any) -> list[str]:
    guilds: list[str] = []
    for doc in db[COLL_GUILD_SETTINGS].find({}, {"guildId": 1}):
        gid = str(doc.get("guildId") or doc.get("_id") or "")
        if gid and gid not in guilds:
            guilds.append(gid)
    return guilds


async def run_loop(app: Any, on_cycle: Any = None) -> None:
    """Фоновая задача lifespan: раз в SYNC_INTERVAL_S скоординированно обновляем журнал.

    T12: on_cycle() вызывается после каждого завершённого цикла — supervisor
    помечает прогресс (beat), поэтому «цикл крутится» отличим от «цикл умер».
    """
    while True:
        await asyncio.sleep(SYNC_INTERVAL_S)
        try:
            db = getattr(app.state, "db", None)
            cfg = getattr(app.state, "config", None)
            if db is None or cfg is None or not cfg.discord_token:
                continue
            for guild in known_guilds(db):
                await sync_guild(db, cfg, guild)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("discord audit sync loop failed", exc_info=True)
        if on_cycle is not None:
            on_cycle()
