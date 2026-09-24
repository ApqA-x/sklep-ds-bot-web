"""G01-G08 (T04): каждая операция привязана к авторизованной гильдии.

Панель знает гильдии A и B (allowlist); C — реальная гильдия бота вне allowlist.
Мутации Discord пишутся только после GET-проверки принадлежности ресурса.
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from api import auth as auth_module
from api import bot as bot_module
from api import discord_api, mutations
from api.config import WebConfig
from api.main import create_app
from fakes import FakeDB

A = "170000000000000000"  # allowlisted, admin пользователь — участник
B = "170000000000000001"  # allowlisted (для проверки чужих ресурсов)
C = "170000000000000002"  # есть у бота, НО вне allowlist (D01)
USER = "160000000000000001"
ADMINISTRATOR = 1 << 3

A_TEXT = "140000000000000000"
A_VOICE = "140000000000000001"
A_THREAD = "140000000000000002"
B_TEXT = "140000000000000003"
A_ROLE = "150000000000000000"
A_MANAGED_ROLE = "150000000000000001"
B_ROLE = "150000000000000002"

CHANNELS = {
    A_TEXT: {"id": A_TEXT, "guild_id": A, "type": 0},
    A_VOICE: {"id": A_VOICE, "guild_id": A, "type": 2},
    A_THREAD: {"id": A_THREAD, "guild_id": A, "type": 11},
    B_TEXT: {"id": B_TEXT, "guild_id": B, "type": 0},
}
GUILD_ROLES = {
    A: [{"id": A, "name": "@everyone"}, {"id": A_ROLE}, {"id": A_MANAGED_ROLE, "managed": True}],
    B: [{"id": B, "name": "@everyone"}, {"id": B_ROLE}],
}
INVITES = {"codeA": {"code": "codeA", "guild_id": A}, "codeB": {"code": "codeB", "guild_id": B}}


@pytest.fixture()
def scope(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Клиент залогиненного админа A/B/C с реестром ресурсов и журналом вызовов."""
    calls: list[dict] = {"get": [], "mutation": []}

    async def fake_request(cfg, method, path, *, json_body=None, reason=None, files=None):
        entry = {"method": method, "path": path, "json": json_body}
        if method == "GET":
            calls["get"].append(entry)
            if path.startswith("/channels/"):
                row = CHANNELS.get(path.split("/")[2])
                return (200, row) if row else (404, {"message": "Unknown Channel"})
            if path.startswith("/invites/"):
                row = INVITES.get(path.rsplit("/", 1)[1])
                return (200, row) if row else (404, {"message": "Unknown Invite"})
            if path.startswith("/guilds/") and path.endswith("/roles"):
                gid = path.split("/")[2]
                return (200, GUILD_ROLES[gid]) if gid in GUILD_ROLES else (404, {})
            if path.startswith("/guilds/") and "/members/" in path:
                return 200, {"user": {"id": USER}}
            return 200, {}
        calls["mutation"].append(entry)
        return 200, {"id": "ok"}

    async def fake_token(cfg, code, redirect_uri):
        return "t"

    async def fake_user_guilds(access_token):
        return {"id": USER, "username": "boss"}, []

    async def fake_resolve_permissions(cfg, guild_id, user_id):
        if guild_id in {A, B, C}:
            return ("allowed", ADMINISTRATOR)
        return ("denied", 0)

    monkeypatch.setattr(discord_api, "bot_request", fake_request)
    monkeypatch.setattr(discord_api, "oauth_token", fake_token)
    monkeypatch.setattr(discord_api, "oauth_user_guilds", fake_user_guilds)
    monkeypatch.setattr(discord_api, "resolve_permissions", fake_resolve_permissions)
    monkeypatch.setattr(discord_api, "bot_guilds", lambda cfg: _async_list([{ "id": A, "name": "A" }, {"id": B, "name": "B"}, {"id": C, "name": "C"}]))
    bot_module._reset_rate_buckets()
    auth_module._clear_recheck_cache()

    db = FakeDB()
    db["guild_settings"].docs.append({"_id": A, "trustedUserIds": [], "updatedAt": None})
    cfg = WebConfig(
        mongo_uri="",
        mongo_db="",
        discord_token="bot-token-fake",
        discord_client_id="cid",
        discord_client_secret="secret",
        discord_redirect_uri="https://example.invalid/api/auth/callback",
        web_session_secret="s" * 32,
        web_public_url="https://example.invalid",
        guild_allowlist=frozenset({A, B}),
    )
    client = TestClient(
        create_app(config=cfg, db=db), base_url="https://example.invalid", follow_redirects=False
    )
    login = client.get("/api/auth/login")
    oauth_state = re.search(r"[&?]state=([^&]+)", login.headers["location"]).group(1)
    client.get(f"/api/auth/callback?code=abc&state={oauth_state}")
    return {"client": client, "db": db, "calls": calls}


async def _async_list(value):
    return value


def _mutations(calls: dict) -> list[str]:
    return [f"{c['method']} {c['path']}" for c in calls["mutation"]]


# G01: канал из гильдии B под URL гильдии A — запрет до любой мутации Discord.
def test_g01_foreign_channel_message_denied(scope: dict) -> None:
    response = scope["client"].post(f"/api/guild/{A}/bot/channel/{B_TEXT}/message", json={"content": "hi"})
    assert response.status_code == 403
    assert _mutations(scope["calls"]) == []
    assert scope["db"][mutations.COLL_AUDIT].docs == []


# G02: invite для чужого канала — запрет до внешнего POST.
def test_g02_foreign_channel_invite_denied(scope: dict) -> None:
    response = scope["client"].post(f"/api/guild/{A}/bot/invite", json={"channelId": B_TEXT})
    assert response.status_code == 403
    assert not [m for m in _mutations(scope["calls"]) if m.startswith("POST")]


# G03: удаление invite чужой гильдии — запрет до внешнего DELETE, без успеха в аудите.
def test_g03_foreign_invite_delete_denied(scope: dict) -> None:
    response = scope["client"].delete(f"/api/guild/{A}/bot/invite/codeB")
    assert response.status_code == 403
    assert _mutations(scope["calls"]) == []
    assert scope["db"][mutations.COLL_AUDIT].docs == []


# G04: чужие resource ID в настройках отвергаются сервером и не сохраняются.
def test_g04_settings_reject_foreign_ids(scope: dict) -> None:
    response = scope["client"].patch(f"/api/guild/{A}/settings", json={"summaryChannelId": B_TEXT, "expectedRevision": 0})
    assert response.status_code == 403
    response = scope["client"].patch(f"/api/guild/{A}/settings", json={"autoRoleId": B_ROLE, "expectedRevision": 0})
    assert response.status_code == 403
    settings_calls = [c for c in scope["calls"]["mutation"]]
    assert settings_calls == []  # мутаций Discord не было
    doc = scope["db"]["guild_settings"].docs[0]
    assert "summaryChannelId" not in doc and "autoRoleId" not in doc


# G05: гильдия C вне allowlist — прямые URL, guild list и whoami доступа не дают.
def test_g05_guild_outside_allowlist_invisible(scope: dict) -> None:
    client = scope["client"]
    assert client.get(f"/api/guild/{C}/audit").status_code == 404
    assert client.patch(f"/api/guild/{C}/settings", json={"trackingMode": "none"}).status_code == 404
    assert (
        client.post(f"/api/guild/{C}/bot/member/{USER}/timeout", json={"mute": True, "seconds": 60}).status_code
        == 404
    )
    guilds = client.get("/api/guilds").json()["guilds"]
    assert {g["guildId"] for g in guilds} <= {A, B}
    whoami = client.get("/api/auth/whoami").json()
    assert {g["guildId"] for g in whoami["access"]} <= {A, B}


# G06: тип канала, @everyone и managed role — контролируемая ошибка, не обход по форме ID.
def test_g06_type_and_role_policies(scope: dict) -> None:
    client = scope["client"]
    # переместить в текстовый канал нельзя
    assert (
        client.post(f"/api/guild/{A}/bot/member/{USER}/move", json={"channelId": A_TEXT}).status_code == 422
    )
    # сообщение в голосовой канал — не textish
    assert client.post(f"/api/guild/{A}/bot/channel/{A_VOICE}/message", json={"content": "x"}).status_code == 422
    # тред текста — можно
    assert client.post(f"/api/guild/{A}/bot/channel/{A_THREAD}/message", json={"content": "x"}).status_code == 200
    # роль @everyone и managed роль — отказ
    assert (
        client.post(f"/api/guild/{A}/bot/member/{USER}/roles", json={"roleId": A, "action": "grant"}).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/guild/{A}/bot/member/{USER}/roles", json={"roleId": A_MANAGED_ROLE, "action": "grant"}
        ).status_code
        == 422
    )
    assert _mutations(scope["calls"]) == ["POST /channels/{}/messages".format(A_THREAD)]
    # несуществующий канал — 404 из проверки, до мутации
    assert client.post(f"/api/guild/{A}/bot/channel/140000000000000099/message", json={"content": "x"}).status_code == 404
    assert len(scope["calls"]["mutation"]) == 1


# G07: ресурс пропал между проверкой и мутацией — ошибка Discord видна, успех не рисуется.
def test_g07_mutation_error_not_swallowed(scope: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    async def vanish(cfg, method, path, *, json_body=None, reason=None, files=None):
        if method == "GET":
            return 200, CHANNELS.get(path.split("/")[2], {"guild_id": A, "type": 0})
        return 404, {"message": "Unknown Channel"}

    monkeypatch.setattr(discord_api, "bot_request", vanish)
    bot_module._reset_rate_buckets()
    response = scope["client"].post(f"/api/guild/{A}/bot/channel/{A_TEXT}/message", json={"content": "hi"})
    assert response.status_code == 502
    audit = scope["db"][mutations.COLL_AUDIT].docs[-1]
    assert audit["ok"] is False and audit["after"]["discordStatus"] == 404


# G08: валидные действия в своей гильдии проходят; аудит фиксирует проверенную гильдию.
def test_g08_positive_path(scope: dict) -> None:
    client = scope["client"]
    assert (
        client.post(f"/api/guild/{A}/bot/member/{USER}/roles", json={"roleId": A_ROLE, "action": "grant"}).status_code
        == 200
    )
    assert client.post(f"/api/guild/{A}/bot/member/{USER}/move", json={"channelId": A_VOICE}).status_code == 200
    assert client.post(f"/api/guild/{A}/bot/invite", json={"channelId": A_TEXT}).status_code == 200
    assert client.delete(f"/api/guild/{A}/bot/invite/codeA").status_code == 200
    assert _mutations(scope["calls"]) == [
        f"PUT /guilds/{A}/members/{USER}/roles/{A_ROLE}",
        f"PATCH /guilds/{A}/members/{USER}",
        f"POST /channels/{A_TEXT}/invites",
        f"DELETE /invites/codeA",
    ]
    audits = client.app.state.db[mutations.COLL_AUDIT].docs
    assert all(row["guildId"] == A for row in audits)
    assert len(audits) == 4
