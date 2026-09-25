from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp

from .config import WebConfig

log = logging.getLogger(__name__)

API = "https://discord.com/api/v10"
_BOT_GUILD_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_BOT_GUILD_TTL = 60.0
_GUILD_RESOURCE_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
_GUILD_RESOURCE_TTL = 300.0

# T16 (п.1, L06): без явного таймаута aiohttp ждёт ответ до 5 минут — подвешенный
# Discord превращал бы запросы в висячие корутины. Подключение — быстрый RTT,
# полный ответ/загрузка — с запасом на 50 МБ пачку файлов.
_TIMEOUT_META = aiohttp.ClientTimeout(total=15.0, connect=8.0)
_TIMEOUT_UPLOAD = aiohttp.ClientTimeout(total=90.0, connect=8.0)


class DiscordError(RuntimeError):
    def __init__(self, op: str, status: int, body: str) -> None:
        super().__init__(f"discord {op} failed: {status}")
        self.op = op
        self.status = status
        self.body = body


async def bot_request(
    cfg: WebConfig,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    reason: str | None = None,
    files: list[tuple[str, bytes, str]] | None = None,
) -> tuple[int, Any]:
    """Bot-token call to Discord REST; returns (status, payload). No exception on 4xx.

    files: список (имя, байты, content_type) — тогда запрос уходит multipart,
    а json_body кладётся в payload_json (так требует POST /channels/{id}/messages).
    """
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    if reason:
        headers["X-Audit-Log-Reason"] = quote(reason)
    kwargs: dict[str, Any]
    if files:
        form = aiohttp.FormData()
        if json_body:
            form.add_field("payload_json", json.dumps(json_body))
        for index, (filename, blob, content_type) in enumerate(files):
            form.add_field(
                f"files[{index}]",
                blob,
                filename=filename,
                content_type=content_type or "application/octet-stream",
            )
        kwargs = {"data": form}
    else:
        kwargs = {"json": json_body}
    async with aiohttp.ClientSession(
        headers=headers, timeout=_TIMEOUT_UPLOAD if files else _TIMEOUT_META
    ) as session:
        async with session.request(method, f"{API}{path}", **kwargs) as response:
            try:
                payload = await response.json()
            except Exception:
                payload = {"raw": (await response.text())[:300]}
            return response.status, payload


async def _get_json(url: str, headers: dict[str, str], *, op: str) -> Any:
    async with aiohttp.ClientSession(headers=headers, timeout=_TIMEOUT_META) as session:
        async with session.get(url) as response:
            if response.status >= 400:
                raise DiscordError(op, response.status, await response.text())
            return await response.json()


async def oauth_token(cfg: WebConfig, code: str, redirect_uri: str) -> str:
    data = {
        "client_id": cfg.discord_client_id,
        "client_secret": cfg.discord_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    async with aiohttp.ClientSession(timeout=_TIMEOUT_META) as session:
        async with session.post(f"{API}/oauth2/token", data=data) as response:
            if response.status >= 400:
                raise DiscordError("oauth_token", response.status, await response.text())
            payload = await response.json()
    return str(payload["access_token"])


async def oauth_user_guilds(access_token: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {access_token}"}
    user = await _get_json(f"{API}/users/@me", headers, op="oauth_me")
    guilds = await _get_json(f"{API}/users/@me/guilds", headers, op="oauth_guilds")
    return user, guilds


async def fetch_bot_guilds(cfg: WebConfig) -> list[dict[str, Any]]:
    if not cfg.discord_token:
        return []
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    if cfg.discord_application_id:
        try:
            rows = await _get_json(
                f"{API}/applications/{cfg.discord_application_id}/guilds", headers, op="bot_guilds"
            )
            if rows:
                return rows
        except DiscordError as err:
            log.warning("application guild list unavailable (%s), falling back to bot guilds", err)
    return await _get_json(f"{API}/users/@me/guilds", headers, op="bot_guilds") or []


async def bot_guilds(cfg: WebConfig) -> list[dict[str, Any]]:
    key = cfg.discord_application_id or cfg.mongo_db
    cached = _BOT_GUILD_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _BOT_GUILD_TTL:
        return cached[1]
    guilds = await fetch_bot_guilds(cfg)
    _BOT_GUILD_CACHE[key] = (time.monotonic(), guilds)
    return guilds


async def _guild_resource_rows(cfg: WebConfig, guild_id: str, kind: str) -> list[dict[str, Any]]:
    key = (guild_id, kind)
    cached = _GUILD_RESOURCE_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _GUILD_RESOURCE_TTL:
        return cached[1]
    if not cfg.discord_token:
        return []
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    rows = await _get_json(f"{API}/guilds/{guild_id}/{kind}", headers, op=kind) or []
    _GUILD_RESOURCE_CACHE[key] = (time.monotonic(), rows)
    return rows


def _rows_to_names(rows: list[dict[str, Any]]) -> dict[str, str]:
    return {str(row["id"]): str(row.get("name") or "") for row in rows if row.get("id")}


async def guild_channel_names(cfg: WebConfig, guild_id: str) -> dict[str, str]:
    return _rows_to_names(await _guild_resource_rows(cfg, guild_id, "channels"))


async def guild_role_names(cfg: WebConfig, guild_id: str) -> dict[str, str]:
    return _rows_to_names(await _guild_resource_rows(cfg, guild_id, "roles"))


async def guild_roles_raw(cfg: WebConfig, guild_id: str) -> list[dict[str, Any]]:
    return await _guild_resource_rows(cfg, guild_id, "roles")


async def guild_channels_raw(cfg: WebConfig, guild_id: str) -> list[dict[str, Any]]:
    return await _guild_resource_rows(cfg, guild_id, "channels")


async def bot_user_id(cfg: WebConfig) -> str | None:
    """The bot's own user id (== application id); the bot user object as fallback."""
    if cfg.discord_application_id:
        return cfg.discord_application_id
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    me = await _get_json(f"{API}/users/@me", headers, op="bot_me")
    return str(me.get("id") or "") or None


async def bot_top_role_position(cfg: WebConfig, guild_id: str) -> int | None:
    """Position of the bot's highest role in a guild, or None when it cannot be determined."""
    if not cfg.discord_token:
        return None
    key = (guild_id, "topRolePosition")
    cached = _GUILD_RESOURCE_CACHE.get(key)
    if cached is not None and time.monotonic() - cached[0] < _GUILD_RESOURCE_TTL:
        return cached[1]
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    try:
        me_id = await bot_user_id(cfg)
        member = await _get_json(f"{API}/guilds/{guild_id}/members/{me_id}", headers, op="bot_member")
    except DiscordError as err:
        log.warning("bot member lookup failed in guild %s (%s); treating all unmanaged roles as assignable", guild_id, err)
        _GUILD_RESOURCE_CACHE[key] = (time.monotonic(), None)
        return None
    bot_role_ids = {str(rid) for rid in member.get("roles", [])}
    rows = await _guild_resource_rows(cfg, guild_id, "roles")
    top = max((int(row.get("position") or 0) for row in rows if str(row.get("id")) in bot_role_ids), default=0)
    _GUILD_RESOURCE_CACHE[key] = (time.monotonic(), top)
    return top


def build_role_options(
    rows: list[dict[str, Any]], guild_id: str, bot_top_position: int | None
) -> list[dict[str, Any]]:
    """Assignable-role picker list: no @everyone, no managed roles, below the bot's top role."""
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: int(r.get("position") or 0), reverse=True):
        rid = str(row.get("id") or "")
        if rid == "" or rid == str(guild_id):
            continue
        managed = bool(row.get("managed") or row.get("tags"))
        position = int(row.get("position") or 0)
        assignable = not managed and (bot_top_position is None or position < bot_top_position)
        out.append(
            {
                "id": rid,
                "name": str(row.get("name") or rid),
                "color": int(row.get("color") or 0),
                "position": position,
                "managed": managed,
                "assignable": assignable,
            }
        )
    return out


VOICE_CHANNEL_TYPES = (2, 13)
# бот пишет сообщения (саммари/activity-карточки) в текстовые и анонсовые каналы
TEXT_CHANNEL_TYPES = (0, 5)


def _channels_of_types(rows: list[dict[str, Any]], types: tuple[int, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: (int(r.get("position") or 0), str(r.get("id") or ""))):
        if int(row.get("type") or 0) not in types:
            continue
        cid = str(row.get("id") or "")
        if cid == "":
            continue
        out.append({"id": cid, "name": str(row.get("name") or cid), "type": int(row.get("type"))})
    return out


def build_voice_channels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _channels_of_types(rows, VOICE_CHANNEL_TYPES)


def build_text_channels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _channels_of_types(rows, TEXT_CHANNEL_TYPES)


DISCORD_EPOCH_MS = 1420070400000

# AuditLogEvent -> читаемое имя (полный набор не нужен: остальное показываем кодом)
AUDIT_ACTION_NAMES: dict[int, str] = {
    1: "Изменение сервера",
    10: "Создание канала",
    11: "Изменение канала",
    12: "Удаление канала",
    13: "Создание прав канала",
    14: "Изменение прав канала",
    15: "Удаление прав канала",
    20: "Кик участника",
    21: "Пачка кика (prune)",
    22: "Бан участника",
    23: "Разбан участника",
    24: "Изменение участника",
    25: "Изменение ролей участника",
    26: "Перемещение в голосе",
    27: "Отключение от голоса",
    28: "Добавление бота",
    30: "Создание роли",
    31: "Изменение роли",
    32: "Удаление роли",
    40: "Создание инвайта",
    41: "Изменение инвайта",
    42: "Удаление инвайта",
    50: "Создание вебхука",
    51: "Изменение вебхука",
    52: "Удаление вебхука",
    60: "Создание эмодзи",
    61: "Изменение эмодзи",
    62: "Удаление эмодзи",
    72: "Удаление сообщения",
    73: "Массовое удаление сообщений",
    74: "Закрепление сообщения",
    75: "Открепление сообщения",
    80: "Создание интеграции",
    81: "Изменение интеграции",
    82: "Удаление интеграции",
    83: "Создание сцены",
    84: "Изменение сцены",
    85: "Удаление сцены",
    90: "Создание стикера",
    91: "Изменение стикера",
    92: "Удаление стикера",
    110: "Создание треда",
    111: "Изменение треда",
    112: "Удаление треда",
    121: "Права слэш-команд",
    140: "Создание события",
    141: "Изменение события",
    142: "Удаление события",
    144: "Автомод: блокировка сообщения",
    145: "Автомод: флаг в канал",
    146: "Автомод: мьют участника",
    147: "Автомод: создание правила",
    148: "Автомод: изменение правила",
    149: "Автомод: удаление правила",
}


def snowflake_to_iso(value: str) -> str:
    try:
        ms = (int(value) >> 22) + DISCORD_EPOCH_MS
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


# Действия, у которых target_id — это id участника (у них нет отдельного target_user_id)
USER_TARGET_ACTIONS = {20, 22, 23, 24, 25, 26, 27, 28}


def build_audit_log_entries(rows: list[dict[str, Any]], users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Discord GET /guilds/{id}/audit-logs -> компактные строки для экрана «Аудит»."""
    names = {str(u.get("id") or ""): str(u.get("username") or "") for u in users if u.get("id")}
    out: list[dict[str, Any]] = []
    for row in rows:
        entry_id = str(row.get("id") or "")
        if not entry_id:
            continue
        action_type = int(row.get("action_type") or 0)
        actor_id = str(row.get("user_id") or "")
        target_id = str(row.get("target_id") or "")
        target_user_id = str(row.get("target_user_id") or "")
        if not target_user_id and action_type in USER_TARGET_ACTIONS:
            target_user_id = target_id
        options = row.get("options") or {}
        out.append(
            {
                "id": entry_id,
                "at": snowflake_to_iso(entry_id),
                "actionType": action_type,
                "action": AUDIT_ACTION_NAMES.get(action_type, f"Действие {action_type}"),
                "actorUserId": actor_id,
                "actorName": names.get(actor_id, ""),
                "targetUserId": target_user_id,
                "targetUserName": names.get(target_user_id, ""),
                "targetId": target_id,
                "channelId": str(options.get("channel_id") or ""),
                "count": options.get("count"),
                "deleteMessageDays": options.get("delete_message_days"),
                "reason": str(row.get("reason") or ""),
                "changes": [
                    {"key": str(c.get("key") or ""), "new": c.get("new_value"), "old": c.get("old_value")}
                    for c in (row.get("changes") or [])
                    if isinstance(c, dict)
                ],
                "options": {str(k): str(v) for k, v in options.items()},
            }
        )
    return out


CDN = "https://cdn.discordapp.com"


def default_avatar_url(user_id: str) -> str:
    idx = (int(user_id) >> 22) % 6 if user_id.isdigit() else 0
    return f"{CDN}/embed/avatars/{idx}.png"


def avatar_url(guild_id: str, user_id: str, avatar: str | None, kind: str = "global") -> str:
    """URL аватарки: kind=guild — серверный вариант, иначе глобальная."""
    if not avatar:
        return default_avatar_url(user_id)
    ext = "gif" if avatar.startswith("a_") else "png"
    if kind == "guild" and guild_id:
        return f"{CDN}/guilds/{guild_id}/users/{user_id}/avatars/{avatar}.{ext}?size=256"
    return f"{CDN}/avatars/{user_id}/{avatar}.{ext}?size=256"


def banner_url(user_id: str, banner: str | None) -> str | None:
    if not banner:
        return None
    return f"{CDN}/banners/{user_id}/{banner}.png?size=600"


async def get_member(cfg: WebConfig, guild_id: str, user_id: str) -> dict[str, Any] | None:
    """Raw guild member via bot token; None when the user is not a member."""
    if not cfg.discord_token:
        return None
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    try:
        return await _get_json(f"{API}/guilds/{guild_id}/members/{user_id}", headers, op="member")
    except DiscordError as err:
        if err.status == 404:
            return None
        raise


async def member_permissions(cfg: WebConfig, guild_id: str, user_id: str) -> int | None:
    """Computed permissions bitfield of a member via bot token, or None if not a member.

    Kept for callers that tolerate tri-state None; auth gates must use
    resolve_permissions(), which separates "denied" from "unknown".
    """
    try:
        status, perms = await resolve_permissions(cfg, guild_id, user_id)
    except PermissionUnavailable:
        raise
    return perms if status == "allowed" else None


ADMINISTRATOR = 1 << 3

# guild_id -> (monotonic_ts, {"owner_id": str, "everyone": int, "roles": {id: perms}})
_PERM_CONTEXT_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
_PERM_CONTEXT_TTL = 60.0


class PermissionUnavailable(RuntimeError):
    """Fresh membership/role state could not be determined; callers must not allow."""


class _GuildGone(Exception):
    pass


async def _perm_context(cfg: WebConfig, guild_id: str) -> dict[str, object]:
    cached = _PERM_CONTEXT_CACHE.get(guild_id)
    if cached is not None and time.monotonic() - cached[0] < _PERM_CONTEXT_TTL:
        return cached[1]
    if not cfg.discord_token:
        raise PermissionUnavailable("bot token not configured")
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    try:
        guild = await _get_json(f"{API}/guilds/{guild_id}", headers, op="perm_guild")
    except DiscordError as err:
        if err.status == 404:
            raise _GuildGone(guild_id) from err
        raise PermissionUnavailable(f"guild lookup failed: {err}") from err
    except aiohttp.ClientError as err:
        raise PermissionUnavailable(f"guild lookup network error: {err}") from err
    try:
        rows = await _get_json(f"{API}/guilds/{guild_id}/roles", headers, op="perm_roles") or []
    except DiscordError as err:
        raise PermissionUnavailable(f"role lookup failed: {err}") from err
    except aiohttp.ClientError as err:
        raise PermissionUnavailable(f"role lookup network error: {err}") from err
    everyone = 0
    roles: dict[str, int] = {}
    for row in rows:
        rid = str(row.get("id") or "")
        try:
            bits = int(row.get("permissions") or 0)
        except (TypeError, ValueError):
            bits = 0
        if rid == guild_id:
            everyone = bits
        elif rid:
            roles[rid] = bits
    ctx = {"owner_id": str(guild.get("owner_id") or ""), "everyone": everyone, "roles": roles}
    _PERM_CONTEXT_CACHE[guild_id] = (time.monotonic(), ctx)
    return ctx


async def resolve_permissions(cfg: WebConfig, guild_id: str, user_id: str) -> tuple[str, int]:
    """Current guild permissions of a user, computed from owner flag and roles.

    Returns ("allowed", bitfield) or ("denied", 0). Raises PermissionUnavailable
    when Discord could not be consulted (timeout/429/5xx/bot access) — the caller
    must surface 503, never fall back to an older grant.
    """
    try:
        ctx = await _perm_context(cfg, guild_id)
    except _GuildGone:
        return ("denied", 0)
    if ctx["owner_id"] == user_id:
        return ("allowed", ADMINISTRATOR)
    headers = {"Authorization": f"Bot {cfg.discord_token}"}
    try:
        member = await _get_json(
            f"{API}/guilds/{guild_id}/members/{user_id}", headers, op="perm_member"
        )
    except DiscordError as err:
        if err.status == 404:
            return ("denied", 0)
        raise PermissionUnavailable(f"member lookup failed: {err}") from err
    except aiohttp.ClientError as err:
        raise PermissionUnavailable(f"member lookup network error: {err}") from err
    roles = ctx["roles"]  # type: ignore[index]
    perms = int(ctx["everyone"])  # type: ignore[arg-type]
    for rid in member.get("roles") or []:
        perms |= roles.get(str(rid), 0)
    return ("allowed", perms)
