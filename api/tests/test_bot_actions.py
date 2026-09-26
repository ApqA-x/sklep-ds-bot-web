from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api import auth as auth_module
from api import audit_sync
from api import bot as bot_module
from api import discord_api, mutations
from api.config import WebConfig
from api.main import create_app
from fakes import FakeDB

GUILD = "170000000000000000"
USER = "160000000000000001"
ROLE = "150000000000000000"
CHANNEL = "140000000000000000"  # текстовый (type 0)
VOICE = "140000000000000009"  # голосовой (type 2) — цели move


def _meta_payload(path: str):
    """Метаданные ресурсов для GET-проверок T04: всё принадлежит нашей гильдии."""
    if path.startswith("/channels/"):
        cid = path.split("/")[2]
        return {"id": cid, "guild_id": GUILD, "type": 2 if cid == VOICE else 0}
    if path.startswith("/invites/"):
        return {"code": path.rsplit("/", 1)[1], "guild_id": GUILD}
    if path.endswith("/roles") and path.startswith("/guilds/"):
        return [{"id": ROLE, "name": "role"}]
    return {"id": USER}


def _make_fake(recorded: list[dict], *, mutation_status: int = 200):
    async def fake_request(cfg, method, path, *, json_body=None, reason=None, files=None):
        if method != "GET":
            # записываем только изменяющие вызовы — проверки (GET) не считаются мутациями
            recorded.append(
                {"method": method, "path": path, "json": json_body, "reason": reason, "files": files}
            )
            if mutation_status != 200:
                return mutation_status, {"message": "Missing Permissions"}
            return 200, {"id": "ok"}
        return 200, _meta_payload(path)

    return fake_request


@pytest.fixture()
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    recorded: list[dict] = []
    monkeypatch.setattr(discord_api, "bot_request", _make_fake(recorded))
    bot_module._reset_rate_buckets()
    auth_module._clear_recheck_cache()
    return recorded


@pytest.fixture()
def files_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    recorded: list[dict] = []
    monkeypatch.setattr(discord_api, "bot_request", _make_fake(recorded))
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
        "files": None,
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
        f"/api/guild/{GUILD}/bot/member/{USER}/move", json={"channelId": VOICE}
    ).status_code == 200
    assert calls[-1] == {
        "method": "PATCH",
        "path": f"/guilds/{GUILD}/members/{USER}",
        "json": {"channel_id": VOICE},
        "reason": None,
        "files": None,
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
    recorded: list[dict] = []
    # GET-проверки T04 проходят, сама мутация получает 403 от Discord
    monkeypatch.setattr(discord_api, "bot_request", _make_fake(recorded, mutation_status=403))
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
        "files": None,
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


def test_user_card_collects_avatar_history(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_member(cfg, guild_id, user_id):
        return {
            "nick": "Ниндзя",
            "joined_at": "2026-01-01T00:00:00+00:00",
            "avatar": "guildhash",
            "roles": [ROLE],
            "user": {
                "username": "ninja",
                "global_name": "Ninja",
                "avatar": "globalhash",
                "banner": "bhash",
                "accent_color": 8421504,
            },
        }

    monkeypatch.setattr(discord_api, "get_member", fake_get_member)
    client = _client()
    body = client.get(f"/api/guild/{GUILD}/users/{USER}/card").json()
    assert body["source"] == "discord"
    assert body["nick"] == "Ниндзя" and body["username"] == "ninja" and body["globalName"] == "Ninja"
    assert "/guilds/" in body["avatarUrl"] and body["avatarUrl"].endswith("guildhash.png?size=256")
    assert body["bannerUrl"].endswith("bhash.png?size=600")
    assert body["accentColor"] == 8421504
    assert {a["hash"]: a["kind"] for a in body["avatars"]} == {"guildhash": "guild", "globalhash": "global"}

    # повторный визит не плодит дубли в истории
    again = client.get(f"/api/guild/{GUILD}/users/{USER}/card").json()
    assert len(again["avatars"]) == 2


def test_user_card_unavailable_defaults() -> None:
    client = _client(discord_token="")
    body = client.get(f"/api/guild/{GUILD}/users/{USER}/card").json()
    assert body["source"] == "unavailable"
    assert body["nick"] is None and body["avatars"] == []
    assert "embed/avatars" in body["avatarUrl"]  # силуэт по снефлейку
    assert client.get(f"/api/guild/{GUILD}/users/bad/card").status_code == 422


def test_snowflake_to_iso() -> None:
    # id=1 -> эпоха Discord 2015-01-01T00:00:00Z
    assert discord_api.snowflake_to_iso("1") == "2015-01-01T00:00:00Z"
    assert discord_api.snowflake_to_iso("bad") == ""


SAMPLE_LOG = {
    # id разнесены на 1 мс (шаг 4194304), чтобы сортировка по at была однозначной
    "audit_log_entries": [
        {
            "id": "1300000000000000000",
            "user_id": USER,
            "action_type": 20,
            "target_user_id": "160000000000000002",
            "target_id": "160000000000000002",
            "options": {"channel_id": CHANNEL, "count": "5"},
            "reason": "spam",
        },
        {"id": "1300000000041943040", "action_type": 999},
        {
            # у реальных записей действий с участником нет target_user_id — цель в target_id
            "id": "1300000000083886080",
            "user_id": USER,
            "action_type": 25,
            "target_id": "160000000000000002",
            "changes": [
                {"key": "$+", "new_value": [{"id": ROLE, "name": "Mod"}]},
                {"key": "$-", "new_value": [{"id": "150000000000000001", "name": "Mute"}]},
            ],
        },
        {"action_type": 22},  # без id — пропускаем
    ],
    "users": [{"id": USER, "username": "admin"}, {"id": "160000000000000002", "username": "victim"}],
}


def test_audit_discord_sync_then_read(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_request(cfg, method, path, *, json_body=None, reason=None, files=None):
        calls.append((method, path))
        if "after=" in path:
            return 200, {"audit_log_entries": [], "users": []}
        return 200, SAMPLE_LOG

    monkeypatch.setattr(discord_api, "bot_request", fake_request)
    client = _client()

    body = client.post(f"/api/guild/{GUILD}/audit/discord/sync").json()
    assert body == {"guildId": GUILD, "ok": True, "discordStatus": 200, "inserted": 3}
    assert ("GET", f"/guilds/{GUILD}/audit-logs?limit=100") in calls
    docs = client.app.state.db["discord_audit_logs"].docs
    assert all(d["guildId"] == GUILD for d in docs)

    # повторный sync идемпотентен: всё уже в базе, новых нет
    body = client.post(f"/api/guild/{GUILD}/audit/discord/sync").json()
    assert body["ok"] is True and body["inserted"] == 0

    page = client.get(f"/api/guild/{GUILD}/audit/discord").json()
    assert page["source"] == "discord" and page["total"] == 3
    assert [i["actionType"] for i in page["items"]] == [25, 999, 20]  # sort=desc по at
    kick = page["items"][2]
    assert kick["id"] == "1300000000000000000"
    assert kick["action"] == "Кик участника"
    assert kick["actorName"] == "admin"
    assert kick["targetUserName"] == "victim"
    assert kick["channelId"] == CHANNEL and kick["count"] == "5" and kick["reason"] == "spam"
    assert kick["at"].startswith("2024")  # snowflake -> дата сохранена и отдана iso
    role_entry = page["items"][0]
    assert role_entry["action"] == "Изменение ролей участника"
    assert role_entry["targetUserId"] == "160000000000000002"  # подставлен из target_id
    assert role_entry["targetUserName"] == "victim"
    assert [c["key"] for c in role_entry["changes"]] == ["$+", "$-"]

    # фильтры
    one = client.get(f"/api/guild/{GUILD}/audit/discord?actionType=999").json()
    assert one["total"] == 1 and one["items"][0]["action"] == "Действие 999"
    by_target = client.get(f"/api/guild/{GUILD}/audit/discord?target=160000000000000002").json()
    assert by_target["total"] == 2
    by_actor = client.get(f"/api/guild/{GUILD}/audit/discord?actor={USER}").json()
    assert by_actor["total"] == 2
    small = client.get(f"/api/guild/{GUILD}/audit/discord?page=2&size=2").json()
    assert small["total"] == 3 and len(small["items"]) == 1
    assert client.get(f"/api/guild/{GUILD}/audit/discord?actor=bad").status_code == 422

    # фейсет действий
    coll = client.app.state.db["discord_audit_logs"]
    coll.aggregate_results = [[{"_id": {"type": 20, "action": "Кик участника"}, "n": 2}]]
    facets = client.get(f"/api/guild/{GUILD}/audit/discord/actions").json()
    assert facets["items"] == [{"actionType": 20, "action": "Кик участника", "count": 2}]


def test_audit_discord_403_hint_and_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(cfg, method, path, *, json_body=None, reason=None, files=None):
        return 403, {"message": "Missing Permissions"}

    monkeypatch.setattr(discord_api, "bot_request", forbidden)
    body = _client().post(f"/api/guild/{GUILD}/audit/discord/sync").json()
    assert body["ok"] is False and body["discordStatus"] == 403
    assert "журнал аудита" in body["error"]
    # пустая локальная копия при этом читается без ошибки
    page = _client().get(f"/api/guild/{GUILD}/audit/discord").json()
    assert page["items"] == [] and page["total"] == 0
    assert _client(discord_token="").post(f"/api/guild/{GUILD}/audit/discord/sync").status_code == 503


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


def test_message_multipart_with_files(files_calls: list[dict]) -> None:
    client = _client()
    response = client.post(
        f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message",
        data={"content": "hello"},
        files=[("files", ("a.txt", b"data", "text/plain"))],
    )
    assert response.status_code == 200
    last = files_calls[-1]
    assert last["path"] == f"/channels/{CHANNEL}/messages"
    assert last["json"] == {"content": "hello"}
    assert last["files"] == [("a.txt", b"data", "text/plain")]
    audit = client.app.state.db[mutations.COLL_AUDIT].docs[0]
    assert audit["action"] == "bot.message"
    assert audit["after"]["attachments"] == [{"name": "a.txt", "size": 4}]
    assert audit["after"]["length"] == 5
    assert audit["after"]["content"] == "hello"  # полный текст сообщения в журнале сайта


def test_message_files_only(files_calls: list[dict]) -> None:
    client = _client()
    response = client.post(
        f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message",
        data={},
        files=[("files", ("pic.png", b"\x89PNG", "image/png"))],
    )
    assert response.status_code == 200
    last = files_calls[-1]
    assert last["json"] is None  # без текста payload_json не шлём
    assert last["files"] == [("pic.png", b"\x89PNG", "image/png")]


def test_message_multipart_validation(files_calls: list[dict], monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client()
    url = f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message"
    # пусто: ни текста, ни файлов
    assert client.post(url, data={}).status_code == 422
    # текст слишком длинный
    assert client.post(url, data={"content": "x" * 2001}).status_code == 422
    # слишком много файлов
    monkeypatch.setattr(bot_module, "MAX_FILES", 2)
    many = [("files", (f"{i}.bin", b"x", "application/octet-stream")) for i in range(3)]
    assert client.post(url, data={}, files=many).status_code == 422
    # файл слишком большой
    monkeypatch.setattr(bot_module, "MAX_FILE_BYTES", 3)
    assert client.post(url, data={}, files=[("files", ("big.bin", b"xxxx", "application/octet-stream"))]).status_code == 422
    assert not files_calls  # до Discord ни одна проверка не дошла


def test_message_json_still_works(files_calls: list[dict]) -> None:
    client = _client()
    response = client.post(
        f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message", json={"content": "plain json"}
    )
    assert response.status_code == 200
    assert files_calls[-1] == {
        "method": "POST",
        "path": f"/channels/{CHANNEL}/messages",
        "json": {"content": "plain json"},
        "files": None,
        "reason": None,
    }
    # пустой content по-прежнему 422
    assert client.post(f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message", json={"content": "  "}).status_code == 422


def test_message_json_embed(calls: list[dict]) -> None:
    client = _client()
    response = client.post(
        f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message",
        json={"embed": {"title": "Заголовок", "description": "Описание", "color": 0xFF0000}},
    )
    assert response.status_code == 200
    body = calls[-1]["json"]
    assert body == {"embeds": [{"title": "Заголовок", "description": "Описание", "color": 0xFF0000}]}
    audit = client.app.state.db[mutations.COLL_AUDIT].docs[0]
    assert audit["action"] == "bot.message"
    assert audit["after"]["embed"] == {"title": "Заголовок", "description": "Описание", "color": 0xFF0000}


def test_message_embed_defaults_to_black_and_minimal(calls: list[dict]) -> None:
    client = _client()
    response = client.post(
        f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message", json={"embed": {"title": "только заголовок"}}
    )
    assert response.status_code == 200
    # пустые поля не уходят в Discord, цвет по умолчанию — чёрный
    assert calls[-1]["json"] == {"embeds": [{"color": 0, "title": "только заголовок"}]}


def test_message_embed_with_content_and_author_footer(calls: list[dict]) -> None:
    client = _client()
    response = client.post(
        f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message",
        json={
            "content": "подпись",
            "embed": {"authorName": "Автор", "footerText": "Снизу"},
        },
    )
    assert response.status_code == 200
    assert calls[-1]["json"] == {
        "content": "подпись",
        "embeds": [{"color": 0, "author": {"name": "Автор"}, "footer": {"text": "Снизу"}}],
    }


def test_message_multipart_embed_with_files(files_calls: list[dict]) -> None:
    client = _client()
    embed = json.dumps({"title": "блок", "image": "pic.png", "thumbnail": "thumb.png"})
    response = client.post(
        f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message",
        data={"embed": embed},
        files=[
            ("files", ("pic.png", b"\x89PNG", "image/png")),
            ("files", ("thumb.png", b"\x89PNG", "image/png")),
        ],
    )
    assert response.status_code == 200
    last = files_calls[-1]
    assert last["json"] == {
        "embeds": [
            {
                "color": 0,
                "title": "блок",
                "image": {"url": "attachment://pic.png"},
                "thumbnail": {"url": "attachment://thumb.png"},
            }
        ]
    }
    assert [name for name, _, _ in last["files"]] == ["pic.png", "thumb.png"]


def test_message_embed_missing_attachment(files_calls: list[dict]) -> None:
    client = _client()
    response = client.post(
        f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message",
        json={"embed": {"title": "x", "image": "absent.png"}},
    )
    assert response.status_code == 422
    assert not files_calls


def test_message_embed_validation(calls: list[dict], monkeypatch: pytest.MonkeyPatch) -> None:
    # L02: message-запрос списывает токен до парсинга — поднимаем ёмкость, чтобы
    # 7 невалидных тел проверили именно 422, а не уперлись в rate-limit
    monkeypatch.setattr(bot_module, "_BUCKET_CAPACITY", 20.0)
    client = _client()
    url = f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message"
    # пустой embed без текста
    assert client.post(url, json={"embed": {}}).status_code == 422
    # title > 256
    assert client.post(url, json={"embed": {"title": "x" * 257}}).status_code == 422
    # description > 4000
    assert client.post(url, json={"embed": {"description": "x" * 4001}}).status_code == 422
    # цвет вне диапазона
    assert client.post(url, json={"embed": {"title": "x", "color": 0x1000000}}).status_code == 422
    assert client.post(url, json={"embed": {"title": "x", "color": -1}}).status_code == 422
    # неизвестное поле
    assert client.post(url, json={"embed": {"fields": []}}).status_code == 422
    assert not calls


# L01: oversize отсекается до парсинга; ложный Content-Length не обманывает счётчик.
def test_l01_oversized_body_rejected_early(
    files_calls: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bot_module, "MAX_BODY_BYTES", 1024)
    client = _client()
    url = f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message"
    # честный Content-Length больше лимита → 413 до чтения тела
    assert client.post(url, data={"content": "x" * 5000}).status_code == 413
    # ложный/отсутствующий Content-Length (chunked): считаем фактические байты
    def chunks():
        for _ in range(10):
            yield b"y" * 300

    response = client.post(
        url,
        content=chunks(),
        headers={
            "content-type": "multipart/form-data; boundary=b",
            "transfer-encoding": "chunked",
        },
    )
    assert response.status_code in (413, 422)
    assert _only_mutations(files_calls) == []


def _only_mutations(recorded: list[dict]) -> list[dict]:
    return recorded  # фикстуры и так пишут только изменяющие вызовы


# L02: burst больших запросов — rate-limit срабатывает до дорогой загрузки тела.
def test_l02_rate_limit_before_body_parse(
    files_calls: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client()
    url = f"/api/guild/{GUILD}/bot/channel/{CHANNEL}/message"
    statuses = [client.post(url, json={"content": "m" + str(i)}).status_code for i in range(8)]
    assert statuses[-1] == 429
    assert statuses.count(200) == 5  # token списан один раз на запрос (billed)
    assert len(files_calls) == 5


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


def test_known_guilds_reads_id_key() -> None:
    # боевые документы guild_settings ключены _id=guildId (пишет бот и PATCH дашборда),
    # guildId-поле — только у старых/ручных записей
    db = FakeDB()
    db["guild_settings"].docs.extend(
        [
            {"_id": GUILD, "updatedAt": None},
            {"_id": "170000000000000001", "guildId": "170000000000000001"},
            {"_id": "", "guildId": ""},
        ]
    )
    assert audit_sync.known_guilds(db) == [GUILD, "170000000000000001"]
