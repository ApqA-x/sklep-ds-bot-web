from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import pytest

from api import queries
from fakes import FakeCollection, FakeDB


def _dt(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    queries._leaderboard_cache.clear()
    queries._invites_cache.clear()


# --- pipeline builders ---


def test_leaderboard_pipeline_period_and_all() -> None:
    cutoff = _dt(1)
    pipeline = queries.build_leaderboard_pipeline("123", cutoff, 50)
    assert pipeline[0] == {"$match": {"guildId": "123", "joinedAt": {"$gte": cutoff}}}
    assert pipeline[1] == {"$sort": {"joinedAt": 1}}
    group = pipeline[2]["$group"]
    assert group["_id"] == "$userId"
    assert group["userName"] == {"$last": "$userName"}
    assert group["totalMs"] == {"$sum": "$durationMs"}
    assert pipeline[3] == {"$sort": {"totalMs": -1}}
    assert pipeline[4] == {"$limit": 50}

    all_time = queries.build_leaderboard_pipeline("123", None, 10)
    assert all_time[0]["$match"] == {"guildId": "123"}


def test_invites_by_inviter_pipeline_requires_inviter() -> None:
    pipeline = queries.build_invites_by_inviter_pipeline("9", _dt(2), 50)
    match = pipeline[0]["$match"]
    assert match["guildId"] == "9"
    assert match["inviterUserId"] == {"$type": "string"}
    assert match["joinedAt"] == {"$gte": _dt(2)}
    assert pipeline[-1] == {"$limit": 50}


def test_member_search_pipeline_escapes_regex() -> None:
    pipeline = queries.build_member_search_pipeline("9", "an.na*m", _dt(3), 10)
    pattern = pipeline[0]["$match"]["userName"]["$regex"]
    assert pattern == re.escape("an.na*m")
    assert re.match(pattern, "an.na*m") is not None
    assert re.match(pattern, "annaXm") is None
    assert pipeline[-2] == {"$sort": {"lastSeen": -1}}


def test_user_daily_pipeline_groups_by_date() -> None:
    pipeline = queries.build_user_daily_pipeline("9", "42", _dt(1))
    group = pipeline[1]["$group"]
    assert group["_id"] == {"$dateToString": {"format": "%Y-%m-%d", "date": "$joinedAt"}}
    assert group["ms"] == {"$sum": "$durationMs"}


# --- TTL cache ---


def test_ttl_cache_expiry() -> None:
    cache = queries.TTLCache(ttl_seconds=0.05)
    cache.set(("k",), [1])
    assert cache.get(("k",)) == [1]
    time.sleep(0.06)
    assert cache.get(("k",)) is None


# --- executors ---


def test_leaderboard_executor_converts_and_caches() -> None:
    db = FakeDB()
    db[queries.COLL_PARTICIPANTS] = FakeCollection(
        queries.COLL_PARTICIPANTS,
        aggregate_results=[
            [{"_id": "777", "userName": "Vasya", "totalMs": 500, "appearances": 3},
             {"_id": "888", "userName": None, "totalMs": 100, "appearances": 1}]
        ],
    )
    items, cached = queries.leaderboard(db, "123", "7d", 50)
    assert cached is False
    assert items == [
        {"userId": "777", "userName": "Vasya", "totalMs": 500, "appearances": 3},
        {"userId": "888", "userName": "unknown", "totalMs": 100, "appearances": 1},
    ]
    items2, cached2 = queries.leaderboard(db, "123", "7d", 50)
    assert cached2 is True
    assert items2 == items
    # второй вызов не должен ходить в базу
    assert len(db[queries.COLL_PARTICIPANTS].calls) == 1


def test_active_sessions_groups_participants() -> None:
    db = FakeDB()
    db[queries.COLL_SESSIONS] = FakeCollection(queries.COLL_SESSIONS, docs=[
        {"_id": "s1", "guildId": "1", "channelId": "c1", "status": "active", "startedAt": _dt(5, 9)},
    ])
    db[queries.COLL_PARTICIPANTS] = FakeCollection(queries.COLL_PARTICIPANTS, docs=[
        {"sessionId": "s1", "userId": "u1", "userName": "A", "joinedAt": _dt(5, 10), "durationMs": 60, "active": True},
        {"sessionId": "s1", "userId": "u2", "userName": "B", "joinedAt": _dt(5, 11), "durationMs": 30, "active": True},
        {"sessionId": "s1", "userId": "u3", "userName": "C", "joinedAt": _dt(5, 9), "durationMs": 10, "active": False},
        {"sessionId": "other", "userId": "u9", "userName": "Z", "joinedAt": _dt(5, 12), "durationMs": 1, "active": True},
    ])
    result = queries.active_sessions(db, "1")
    assert len(result) == 1
    session = result[0]
    assert session["id"] == "s1"
    assert [p["userId"] for p in session["participants"]] == ["u1", "u2"]
    assert session["startedAt"] == "2026-09-05T09:00:00Z"


def test_sessions_history_pagination_and_total() -> None:
    db = FakeDB()
    docs = [
        {"_id": f"s{i}", "guildId": "1", "channelId": "c", "status": "closed",
         "startedAt": _dt(1, i), "endedAt": _dt(1, i + 1), "summaryMessage": "x" if i == 0 else None}
        for i in range(3)
    ]
    db[queries.COLL_SESSIONS] = FakeCollection(queries.COLL_SESSIONS, docs=docs)
    page1 = queries.sessions_history(db, "1", 1, 2)
    assert page1["total"] == 3
    assert [s["id"] for s in page1["items"]] == ["s2", "s1"]
    page2 = queries.sessions_history(db, "1", 2, 2)
    assert [s["id"] for s in page2["items"]] == ["s0"]
    assert page2["items"][0]["hasSummary"] is True


def test_session_detail_guild_isolation() -> None:
    db = FakeDB()
    db[queries.COLL_SESSIONS] = FakeCollection(queries.COLL_SESSIONS, docs=[
        {"_id": "s1", "guildId": "2", "channelId": "c", "status": "closed", "startedAt": _dt(1)},
    ])
    assert queries.session_detail(db, "1", "s1") is None
    found = queries.session_detail(db, "2", "s1")
    assert found is not None and found["id"] == "s1"


def test_user_profile_none_for_unknown_and_full_for_known() -> None:
    db = FakeDB()
    assert queries.user_profile(db, "1", "555", "30d") is None

    db[queries.COLL_PARTICIPANTS] = FakeCollection(queries.COLL_PARTICIPANTS,
        docs=[{"guildId": "1", "userId": "42", "userName": "New", "joinedAt": _dt(9)}],
        aggregate_results=[
            [{"_id": None, "totalMs": 900, "appearances": 2}],
            [{"_id": "2026-09-08", "ms": 400}, {"_id": "2026-09-09", "ms": 500}],
        ])
    db[queries.COLL_ROLE_STATE] = FakeCollection(queries.COLL_ROLE_STATE,
        docs=[{"guildId": "1", "userId": "42", "roleIds": ["r1", 2]}])
    db[queries.COLL_NICKNAME_HISTORY] = FakeCollection(queries.COLL_NICKNAME_HISTORY,
        docs=[{"guildId": "1", "userId": "42", "nickname": "New", "previousNickname": "Old",
               "changedAt": _dt(8), "source": "discord"}])
    db[queries.COLL_JOIN_STATE] = FakeCollection(queries.COLL_JOIN_STATE,
        docs=[{"guildId": "1", "userId": "42", "inviteCode": "abc", "inviteType": "regular",
               "inviterUserId": "7", "inviterName": "Inv", "attributionStatus": "exact", "joinedAt": _dt(1)}])

    profile = queries.user_profile(db, "1", "42", "30d")
    assert profile is not None
    assert profile["userName"] == "New"
    assert profile["totalMs"] == 900
    assert profile["appearances"] == 2
    assert profile["daily"] == [{"date": "2026-09-08", "ms": 400}, {"date": "2026-09-09", "ms": 500}]
    assert profile["roleIds"] == ["r1", "2"]
    assert profile["nicknames"][0]["changedAt"] == "2026-09-08T00:00:00Z"
    assert profile["join"]["inviterName"] == "Inv"
    assert profile["join"]["joinedAt"] == "2026-09-01T00:00:00Z"


def test_invites_overview_sections_and_cache() -> None:
    db = FakeDB()
    db[queries.COLL_JOIN_ATTRIBUTIONS] = FakeCollection(queries.COLL_JOIN_ATTRIBUTIONS,
        docs=[{"guildId": "1", "userId": "u", "joinedAt": _dt(4), "inviteCode": "inv",
               "inviteType": "regular", "inviterUserId": "i", "inviterName": "N",
               "attributionStatus": "exact", "source": "snapshot"}],
        aggregate_results=[[{"_id": "i", "userName": "N", "count": 4}]])
    db[queries.COLL_INVITE_CATALOG] = FakeCollection(queries.COLL_INVITE_CATALOG,
        docs=[{"guildId": "1", "code": "inv", "channelId": "c", "inviteType": "regular",
               "createdByUserId": "i", "createdByName": "N", "createdAt": _dt(2),
               "deletedAt": None, "lastSeenAt": _dt(4), "source": "live_event"}])
    first = queries.invites_overview(db, "1", "30d")
    assert first["attributions"][0]["inviteCode"] == "inv"
    assert first["catalog"][0]["lastSeenAt"] == "2026-09-04T00:00:00Z"
    assert first["byInviter"] == [{"userId": "i", "userName": "N", "count": 4}]
    second = queries.invites_overview(db, "1", "30d")
    assert second is first


def test_settings_document_maps_id_and_iso() -> None:
    db = FakeDB()
    db[queries.COLL_GUILD_SETTINGS] = FakeCollection(queries.COLL_GUILD_SETTINGS, docs=[
        {"_id": "1", "trackingMode": "all", "trustedUserIds": ["5"], "updatedAt": _dt(3)},
    ])
    doc = queries.settings_document(db, "1")
    assert doc["guildId"] == "1"
    assert doc["updatedAt"] == "2026-09-03T00:00:00Z"
    assert "_id" not in doc
    assert queries.settings_document(db, "404") is None


def test_search_members_converts() -> None:
    db = FakeDB()
    db[queries.COLL_PARTICIPANTS] = FakeCollection(queries.COLL_PARTICIPANTS,
        aggregate_results=[[{"_id": "9", "userName": "Nina", "lastSeen": _dt(9)}]])
    items = queries.search_members(db, "1", "ni", 10)
    assert items == [{"userId": "9", "userName": "Nina"}]


def test_ensure_web_indexes_names_and_errors() -> None:
    db = FakeDB()
    db[queries.COLL_PARTICIPANTS] = FakeCollection(queries.COLL_PARTICIPANTS)
    db[queries.COLL_SESSIONS] = FakeCollection(queries.COLL_SESSIONS, fail_create_index=True)
    result = queries.ensure_web_indexes(db)
    assert result["created"] == [
        "voice_session_participants.web_guildId_joinedAt",
        "web_audit_logs.web_audit_guildId_at",
    ]
    assert result["errors"] == ["voice_sessions.web_guildId_status_endedAt: RuntimeError"]
    keys, kw = db[queries.COLL_PARTICIPANTS].calls[0][2], db[queries.COLL_PARTICIPANTS].calls[0][3]
    assert keys == [("guildId", 1), ("joinedAt", 1)]
    assert kw["name"] == "web_guildId_joinedAt"
