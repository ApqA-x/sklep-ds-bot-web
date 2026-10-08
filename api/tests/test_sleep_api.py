from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import discord_api
from api.auth import require_guild_admin
from api.config import WebConfig
from api.main import create_app
from fakes import FakeDB


GUILD = "170000000000000000"
USER = "160000000000000001"
OTHER = "160000000000000002"
PATH = f"/api/guild/{GUILD}/sleep/member/{USER}"


def _client(monkeypatch, *, member_status: int = 200, returned_user: str = USER) -> TestClient:
    async def fake_discord(_cfg, method, path, **_kwargs):
        assert method == "GET"
        assert path == f"/guilds/{GUILD}/members/{USER}"
        return member_status, {"user": {"id": returned_user}}

    monkeypatch.setattr(discord_api, "bot_request", fake_discord)
    app = create_app(
        config=WebConfig(mongo_uri="", mongo_db="", discord_token="fake-token"),
        db=FakeDB(),
    )
    return TestClient(app)


def test_set_replay_status_and_cancel(monkeypatch) -> None:
    client = _client(monkeypatch)
    headers = {"idempotency-key": "sleep-request-1"}
    first = client.post(PATH, json={"hours": 2}, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending"
    assert first.json()["replayed"] is False
    due = first.json()["dueAt"]
    assert due
    repeated = client.post(PATH, json={"hours": 2}, headers=headers)
    assert repeated.status_code == 200
    assert repeated.json()["replayed"] is True
    assert repeated.json()["dueAt"] == due
    assert client.get(PATH).json()["status"] == "pending"

    cancelled = client.delete(PATH, headers={"idempotency-key": "sleep-request-2"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["hadActiveTimer"] is True
    assert cancelled.json()["status"] == "cancelled"
    old_replay = client.post(PATH, json={"hours": 2}, headers=headers)
    assert old_replay.status_code == 200
    assert old_replay.json()["replayed"] is True
    assert client.get(PATH).json()["status"] == "cancelled"


@pytest.mark.parametrize("hours", [0, 25, True, "2", 2.5])
def test_invalid_hours_do_not_write(monkeypatch, hours) -> None:
    client = _client(monkeypatch)
    response = client.post(PATH, json={"hours": hours}, headers={"idempotency-key": "sleep-invalid"})
    assert response.status_code == 422
    assert client.app.state.db["voice_sleep_timers"].docs == []


@pytest.mark.parametrize("status,user", [(404, USER), (500, USER), (200, OTHER)])
def test_member_verification_fails_closed(monkeypatch, status, user) -> None:
    client = _client(monkeypatch, member_status=status, returned_user=user)
    response = client.post(PATH, json={"hours": 2}, headers={"idempotency-key": "sleep-member"})
    assert response.status_code in {403, 502}
    assert client.app.state.db["voice_sleep_timers"].docs == []


def test_admin_dependency_and_idempotency_key_required(monkeypatch) -> None:
    client = _client(monkeypatch)
    assert client.post(PATH, json={"hours": 2}).status_code == 422

    async def deny() -> None:
        raise HTTPException(status_code=403, detail="administrator permission required")

    client.app.dependency_overrides[require_guild_admin] = deny
    assert client.get(PATH).status_code == 403
    assert client.post(PATH, json={"hours": 2}, headers={"idempotency-key": "sleep-denied"}).status_code == 403
    assert client.delete(PATH, headers={"idempotency-key": "sleep-denied"}).status_code == 403
    assert client.app.state.db["voice_sleep_timers"].docs == []
