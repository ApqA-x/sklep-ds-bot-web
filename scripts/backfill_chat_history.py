#!/usr/bin/env python3
"""Дозагрузка истории chat_messages из Discord REST.

Причина: разовый импорт 2026-09-23 00:25 был ограничен ~100 сообщениями на канал,
а живой writer гейта пишет только с ~2026-09-22 — из-за этого счётчики сообщений
в профилях сильно меньше реальных.

Скрипт идемпотентен: вставляет только отсутствующие документы ($setOnInsert),
никогда не перетирает строки, которые уже ведёт гейт (editedAt/deletedAt).
Схема повторяет voice_tracker.repository.record_chat_message из dsbot.

Токен: --token или переменная окружения DISCORD_TOKEN.

Пример (Windows, локальный mongod на 27017):
  python scripts\\backfill_chat_history.py --guild 997877836872421506
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from pymongo import MongoClient

API = "https://discord.com/api/v10"
DISCORD_EPOCH_MS = 1420070400000
TEXT_CHANNEL_TYPES = {0, 5}  # текстовые + announcement
REQUEST_PAUSE_S = 0.4


def rest_get(token: str, path: str) -> tuple[int, object]:
    """GET Discord REST с обработкой 429 (один повтор по retry_after)."""
    req = urllib.request.Request(
        API + path, headers={"Authorization": f"Bot {token}", "User-Agent": "chat-backfill"}
    )
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")
            if err.code == 429 and attempt == 1:
                try:
                    wait = float(json.loads(body).get("retry_after", 1)) + 0.2
                except Exception:
                    wait = 2.0
                time.sleep(wait)
                continue
            try:
                parsed = json.loads(body)
            except Exception:
                parsed = body[:200]
            return err.code, parsed
        except OSError as err:
            print(f"  network error on {path}: {err}", flush=True)
            if attempt == 1:
                time.sleep(1.0)
                continue
            return 0, str(err)
    return 0, "unreachable"


def sent_at_from_snowflake(message_id: str) -> datetime:
    ms = (int(message_id) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def author_name(author: dict) -> str:
    return str(author.get("global_name") or author.get("username") or "")


def attachments_meta(message: dict) -> list[dict]:
    """Мета вложений в том же формате, что пишет гейт (без скачивания файлов)."""
    out = []
    for a in message.get("attachments") or []:
        filename = str(a.get("filename") or "")
        content_type = str(a.get("content_type") or "")
        lower = filename.lower()
        if content_type.startswith("image/") or lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            kind = "image"
        elif content_type.startswith("video/") or lower.endswith((".mp4", ".mov", ".mkv", ".webm")):
            kind = "video"
        elif content_type.startswith("audio/") or lower.endswith((".mp3", ".ogg", ".wav", ".m4a")):
            kind = "audio"
        else:
            kind = "file"
        out.append(
            {
                "id": str(a.get("id") or ""),
                "filename": filename[:255],
                "contentType": content_type[:127],
                "size": int(a.get("size") or 0),
                "kind": kind,
                "path": "",
                "stored": False,
                "url": str(a.get("proxy_url") or a.get("url") or "")[:512],
            }
        )
    return out


def backfill_channel(db, token: str, guild: str, channel: dict, max_pages: int, dry_run: bool) -> tuple[int, int]:
    cid = str(channel["id"])
    inserted = skipped = 0
    before = ""
    for _page in range(max_pages if max_pages > 0 else 10_000):
        path = f"/channels/{cid}/messages?limit=100" + (f"&before={before}" if before else "")
        status, payload = rest_get(token, path)
        if status != 200:
            print(f"  channel {cid}: HTTP {status} (пропускаем)", flush=True)
            break
        messages = payload if isinstance(payload, list) else []
        for m in messages:
            author = m.get("author") or {}
            if author.get("bot") or m.get("type") != 0:
                continue
            mid = str(m.get("id") or "")
            if not mid:
                continue
            payload_set = {
                "channelId": cid,
                "authorUserId": str(author.get("id") or ""),
                "authorName": author_name(author),
                "content": str(m.get("content") or ""),
                "sentAt": sent_at_from_snowflake(mid),
                "editedAt": None,
                "deletedAt": None,
            }
            meta = attachments_meta(m)
            if meta:
                payload_set["attachments"] = meta
            if dry_run:
                inserted += 1
                continue
            res = db.chat_messages.update_one(
                {"guildId": guild, "messageId": mid},
                {"$setOnInsert": payload_set},
                upsert=True,
            )
            if res.upserted_id is not None:
                inserted += 1
            else:
                skipped += 1
        if len(messages) < 100:
            break
        before = str(messages[-1]["id"])
        time.sleep(REQUEST_PAUSE_S)
    return inserted, skipped


def load_token_from_env_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("DISCORD_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Дозагрузка истории сообщений в chat_messages")
    parser.add_argument("--guild", required=True, help="id сервера (snowflake)")
    parser.add_argument("--token", default=os.environ.get("DISCORD_TOKEN", ""), help="бот-токен Discord")
    parser.add_argument(
        "--env-file",
        default=r"D:\dashboard-mvp\.env",
        help="если токен не передан, прочитать DISCORD_TOKEN отсюда",
    )
    parser.add_argument("--mongo-uri", default="mongodb://127.0.0.1:27017")
    parser.add_argument("--db-name", default="voice_tracker")
    parser.add_argument("--max-pages", type=int, default=0, help="страниц по 100 сообщений на канал (0 = без лимита)")
    parser.add_argument("--channel", default="", help="обработать только этот канал")
    parser.add_argument("--include-threads", action="store_true", help="захватить и активные публичные треды")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать, не писать")
    args = parser.parse_args()

    try:  # Windows-консоль cp1251 не переваривает имена каналов
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not args.token:
        args.token = load_token_from_env_file(args.env_file)
    if not args.token:
        print("нужен токен: --token, переменная DISCORD_TOKEN или --env-file", file=sys.stderr)
        return 2

    guild = args.guild
    db = MongoClient(args.mongo_uri)[args.db_name]
    status, channels = rest_get(args.token, f"/guilds/{guild}/channels")
    if status != 200 or not isinstance(channels, list):
        print(f"не удалось получить список каналов: HTTP {status}", file=sys.stderr)
        return 1
    targets = [c for c in channels if c.get("type") in TEXT_CHANNEL_TYPES]
    if args.include_threads:
        st, threads = rest_get(args.token, f"/guilds/{guild}/threads/active")
        if st == 200 and isinstance(threads, dict):
            targets.extend(threads.get("threads") or [])
    if args.channel:
        targets = [c for c in targets if str(c.get("id")) == args.channel]

    total_ins = total_skip = 0
    for index, channel in enumerate(targets, 1):
        ins, skip = backfill_channel(db, args.token, guild, channel, args.max_pages, args.dry_run)
        total_ins += ins
        total_skip += skip
        ch_id = str(channel["id"])
        print(f"[{index}/{len(targets)}] канал {ch_id}: +{ins} новых, ={skip} уже есть", flush=True)
        time.sleep(REQUEST_PAUSE_S)

    print(f"ИТОГО: вставлено {total_ins}, пропущено (уже в базе) {total_skip}" + (" [dry-run]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
