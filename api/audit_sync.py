"""Копия журнала аудита Discord в базе дашборда.

Discord хранит audit log ~45 дней и не больше 100 000 записей, поэтому нам нужна
собственная постоянная копия: раз в минуту тянем новые записи (после последней
сохранённой) и дозаргруем историю при первом запуске. Идемпотентно: upsert по
{guildId, entryId} через $setOnInsert — записи аудита неизменны.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .queries import COLL_GUILD_SETTINGS

COLL_DISCORD_AUDIT = "discord_audit_logs"
SYNC_INTERVAL_S = 60
INITIAL_PAGES = 5  # первая подтяжка истории: до 500 записей
FRESH_PAGES = 10

log = logging.getLogger("audit_sync")


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
        "syncedAt": datetime.now(timezone.utc),
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


def newest_stored(db: Any, guild: str) -> tuple[str, int]:
    """(entryId самой свежей сохранённой записи, всего записей в базе)."""
    total = int(db[COLL_DISCORD_AUDIT].count_documents({"guildId": guild}))
    if total == 0:
        return "", 0
    doc = next(
        iter(db[COLL_DISCORD_AUDIT].find({"guildId": guild}, sort=[("at", -1)], limit=1)),
        None,
    )
    return (str(doc["entryId"]) if doc else ""), total


async def sync_guild(db: Any, cfg: Any, guild: str) -> dict[str, Any]:
    """Один проход синхронизации. Возвращает {ok, discordStatus, inserted}."""
    from . import discord_api

    if not cfg.discord_token:
        return {"ok": False, "discordStatus": 0, "inserted": 0}
    last_id, _total = newest_stored(db, guild)
    inserted = 0
    if not last_id:
        before = ""
        for _page in range(INITIAL_PAGES):
            path = f"/guilds/{guild}/audit-logs?limit=100" + (f"&before={before}" if before else "")
            status, payload = await discord_api.bot_request(cfg, "GET", path)
            if status != 200:
                if status == 403:
                    log.info("audit sync guild=%s: нет доступа к журналу (403)", guild)
                else:
                    log.warning("audit sync guild=%s: discord status %s", guild, status)
                return {"ok": status == 200, "discordStatus": status, "inserted": inserted}
            entries = _entries(payload)
            inserted += _store(db, guild, entries)
            if len(entries) < 100:
                break
            before = str(entries[-1]["id"])
        return {"ok": True, "discordStatus": 200, "inserted": inserted}
    for _page in range(FRESH_PAGES):
        status, payload = await discord_api.bot_request(cfg, "GET", f"/guilds/{guild}/audit-logs?limit=100&after={last_id}")
        if status != 200:
            log.warning("audit sync guild=%s: discord status %s", guild, status)
            return {"ok": False, "discordStatus": status, "inserted": inserted}
        entries = _entries(payload)
        if not entries:
            break
        inserted += _store(db, guild, entries)
        newest = max(entries, key=lambda e: int(e["id"]))
        if str(newest["id"]) == last_id:
            break
        last_id = str(newest["id"])
    return {"ok": True, "discordStatus": 200, "inserted": inserted}


def known_guilds(db: Any) -> list[str]:
    guilds: list[str] = []
    for doc in db[COLL_GUILD_SETTINGS].find({}, {"guildId": 1}):
        gid = str(doc.get("guildId") or doc.get("_id") or "")
        if gid and gid not in guilds:
            guilds.append(gid)
    return guilds


async def run_loop(app: Any) -> None:
    """Фоновая задача lifespan: раз в SYNC_INTERVAL_S обновляем журнал по всем гильдиям."""
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
