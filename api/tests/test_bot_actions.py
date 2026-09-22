from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import auth as auth_module
from api import bot as bot_module
from api import discord_api, mutations
from api.config import WebConfig
from api.main import create_app
from fakes import FakeDB

GUILD = "170000000000000000"
USER = "160000000000000001"
ROLE = "150000000000000000"
CHANNEL = "140000000000000000"


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    recorded: list[dict] = []

    async def fake_request(cfg, method, path, *, json_body=None, reason=None):
        recorded.append({"method": method, "path": path, "json": json_body, "reason": reason})
        return 200, {"id": "ok"}

    monkeypatch.setattr(discord_api, "bot_request", fake_request)
    bot_module._reset_rate_buckets()
    auth_module._clear_recheck_cache()
    return recorded


def _client(**cfg_overrides) -> TestClient:
    overrides = {"discord_token": "bot-token-fake"}
    overrides.update(cfg_overrides)
    cfg = WebConfig(mongo_uri="", mongo_db="", **overrides)
    return TestClient(create_app(config=cfg, db=FakeDB()))


def test_role_grant_and_revoke(calls: list[dict]) -> None:
    client = _client()
    assert client.post(
        f"/api/guild/{GUILD}/bot/member/{USER}/roles", json={"roleId": ROLE, "action": "grant"}
    ).status_code == 200
    assert calls[-1] == {
        "method": "PUT",
        "path": f"/guilds/{GUILD}/members/{USER}/roles/{ROLE}",
        "json": None,
        "reason": None,
    }
    client.post(f"/api/guild/{GUILD}/bot/member/{USER}/roles", json={"roleId": ROLE, "action": "revoke"})
    assert calls[-1]["method"] == "DELETE"


def test_timeout_and_unmute(calls: list[dict]) -> None:
    client = _client()
    client.post(f"/api/guild/{GUILD}/bot/member/{USER}/timeout", json={"mute": True, "seconds": 3600})
    body = calls[-1]["json"]
    assert body["communication_disabled_until"].endswith("+00:00")
    client.post(f"/api/guild/{GUILD}/bot/member/{USER}/timeout", json={"mute": False})
    assert calls[-1]["json"] == {"communication_disabled_until": None}


def test_invalid_seconds_422(calls: list[dict]) -> None:
    client = _client()
    response = client.post(f"/api/guild/{GUILD}/bot/member/{USER}/timeout", json={"mute": True, "seconds": 5})
    assert response.status_code == 422


def test_move_kick_message_invite(calls: list[dict]) -> None:
    client = _client()
    assert client.post(
        f"/api/guild/{GUILD}/bot/member/{USER}/move", json={"channelId": CHANNEL}
    ).status_code == 200
    assert calls[-1] == {
        "method": "PATCH",
        "path": f"/guilds/{GUILD}/members/{USER}",
        "json": {"channel_id": CHANNEL},
        "reason": None,
    }
    client.post(f"/api/guild/{GUILD}/bot/member/{USER}/kick", json={"reason": "spam"})
    assert calls[-1]["method"] == "DELETE" and calls[-1]["reason"] == "spam"
    client.post(f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message", json={"content": "hello"})
    assert calls[-1]["path"] == f"/channels/{CHANNEL}/messages"
    client.post(f"/api/guild/{GUILD}/bot/invite", json={"channelId": CHANNEL})
    assert calls[-1]["path"] == f"/channels/{CHANNEL}/invites"
    client.delete(f"/api/guild/{GUILD}/bot/invite/abcDEF-1")
    assert calls[-1]["path"] == "/invites/abcDEF-1"


def test_path_injection_rejected(calls: list[dict]) -> None:
    client = _client()
    response = client.post(
        f"/api/guild/{GUILD}/bot/member/12..%2Fx/roles",
        json={"roleId": ROLE, "action": "grant"},
    )
    assert response.status_code in {404, 405, 422}
    response = client.delete(f"/api/guild/{GUILD}/bot/invite/bad*code")
    assert response.status_code == 422
    assert not [c for c in calls if "bad*" in c["path"]]


def test_discord_error_maps_502_and_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail_request(cfg, method, path, *, json_body=None, reason=None):
        return 403, {"message": "Missing Permissions"}

    monkeypatch.setattr(discord_api, "bot_request", fail_request)
    bot_module._reset_rate_buckets()
    client = _client()
    response = client.post(
        f"/api/guild/{GUILD}/bot/member/{USER}/roles", json={"roleId": ROLE, "action": "grant"}
    )
    assert response.status_code == 502
    assert response.json()["detail"]["discordStatus"] == 403
    db = client.app.state.db
    audit = db[mutations.COLL_AUDIT].docs[0]
    assert audit["action"] == "bot.role"
    assert audit["ok"] is False
    assert audit["after"]["discordStatus"] == 403
    assert audit["origin"] == "web"  # инициатор — сайт, Discord лишь транспорт


def test_successful_bot_action_audited_with_web_origin(calls: list[dict]) -> None:
    client = _client()
    assert client.post(
        f"/api/guild/{GUILD}/bot/member/{USER}/roles", json={"roleId": ROLE, "action": "grant"}
    ).status_code == 200
    audit = client.app.state.db[mutations.COLL_AUDIT].docs[0]
    assert audit["origin"] == "web"
    assert audit["ok"] is True


def test_disconnect_member(calls: list[dict]) -> None:
    client = _client()
    assert client.post(f"/api/guild/{GUILD}/bot/member/{USER}/disconnect").status_code == 200
    assert calls[-1] == {
        "method": "PATCH",
        "path": f"/guilds/{GUILD}/members/{USER}",
        "json": {"channel_id": None},
        "reason": None,
    }
    audit = client.app.state.db[mutations.COLL_AUDIT].docs[0]
    assert audit["action"] == "bot.disconnect"
    assert audit["origin"] == "web"


def test_member_state_live_from_discord(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_member(cfg, guild_id, user_id):
        return {
            "roles": [ROLE],
            "communication_disabled_until": "2026-09-22T12:00:00+00:00",
            "voice": {"channel_id": CHANNEL},
        }

    monkeypatch.setattr(discord_api, "get_member", fake_get_member)
    client = _client()
    body = client.get(f"/api/guild/{GUILD}/users/{USER}/member").json()
    assert body["source"] == "discord"
    assert body["roleIds"] == [ROLE]
    assert body["timeoutUntil"] == "2026-09-22T12:00:00+00:00"
    assert body["voiceChannelId"] == CHANNEL


def test_member_state_unavailable_without_token() -> None:
    client = _client(discord_token="")
    body = client.get(f"/api/guild/{GUILD}/users/{USER}/member").json()
    assert body["source"] == "unavailable"
    assert body["roleIds"] is None
    assert client.get(f"/api/guild/{GUILD}/users/bad/member").status_code == 422


def test_missing_bot_token_503() -> None:
    client = _client(discord_token="")  # бот-токен не настроен
    response = client.post(
        f"/api/guild/{GUILD}/bot/member/{USER}/roles", json={"roleId": ROLE, "action": "grant"}
    )
    assert response.status_code == 503


def test_rate_limit_burst(calls: list[dict]) -> None:
    client = _client()
    statuses = [
        client.post(
            f"/api/guild/{GUILD}/bot/member/{USER}/roles", json={"roleId": ROLE, "action": "grant"}
        ).status_code
        for _ in range(8)
    ]
    assert statuses.count(200) == 5
    assert statuses[-1] == 429
    assert len(calls) == 5


def test_write_router_admin_gate_blocks_reads_too() -> None:
    # в dev-режиме write/bot-гейты прозрачны; при включённом auth проверяется админ
    cfg = WebConfig(
        mongo_uri="",
        mongo_db="",
        discord_client_id="cid",
        discord_client_secret="secret",
        discord_redirect_uri="https://example.invalid/api/auth/callback",
        web_session_secret="s" * 32,
        web_public_url="https://example.invalid",
    )
    client = TestClient(create_app(config=cfg, db=FakeDB()), base_url="https://testserver", follow_redirects=False)
    response = client.post(
        f"/api/guild/{GUILD}/bot/member/{USER}/roles", json={"roleId": ROLE, "action": "grant"}
    )
    assert response.status_code == 401
