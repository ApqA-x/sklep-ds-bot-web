"""T10 web: копия манифеста синхронна с bot-репо и с WEB_INDEXES."""

from __future__ import annotations

import pytest

from api import queries, schema_contract

MANIFEST_CHECKSUM = "fc126fa18013c90a7a2ac5a3f062c206202fe7e966d1e7fd144e8b2fa91613c4"


def test_manifest_copy_checksum_matches_bot_source_of_truth() -> None:
    # если bot-манифест пересмотрен, но копия в web не обновлена — тест красный
    assert schema_contract.manifest_checksum() == MANIFEST_CHECKSUM


def test_web_indexes_list_matches_manifest_subset() -> None:
    manifest = schema_contract.load_manifest()
    web_specs = {
        (s["collection"], tuple(tuple(k) for k in s["keys"]), s["name"])
        for s in manifest["indexes"]
        if s.get("owner") in ("web", "shared")
    }
    local = {(coll, tuple(keys), name) for coll, keys, name in queries.WEB_INDEXES}
    assert local == web_specs


def test_verify_skips_fakes_without_list_indexes() -> None:
    from fakes import FakeDB

    assert schema_contract.verify_web_schema(FakeDB()) == {"skipped": True}


def _actual_doc(s: dict) -> dict:
    doc = {"name": s["name"], "key": {field: dirn for field, dirn in s["keys"]}}
    if s.get("unique"):
        doc["unique"] = True
    if s.get("sparse"):
        doc["sparse"] = True
    if s.get("partial") is not None:
        doc["partialFilterExpression"] = s["partial"]
    if s.get("ttl") is not None:
        doc["expireAfterSeconds"] = s["ttl"]
    return doc


def test_verify_accepts_equivalent_alias_and_flags_mismatch() -> None:
    class Col:
        def __init__(self, docs):
            self.docs = docs

        def list_indexes(self):
            return iter(self.docs)

    class Db:
        def __init__(self, by_coll):
            self.by_coll = by_coll

        def __getitem__(self, name):
            return Col(self.by_coll.get(name, []))

    manifest = schema_contract.load_manifest()
    ttl_spec = next(s for s in manifest["indexes"] if s["name"] == "operations_createdAt_ttl")
    by_coll: dict[str, list[dict]] = {}
    for s in manifest["indexes"]:
        if s.get("owner") not in ("web", "shared"):
            continue
        by_coll.setdefault(s["collection"], []).append(_actual_doc(s))
    report = schema_contract.verify_web_schema(Db(by_coll))
    assert report["ok"] is True and report["acceptedAlias"] == []

    # alias: переименованный эквивалент принимается
    by_coll["operations"][0]["name"] = "ops_batch_hist"
    report = schema_contract.verify_web_schema(Db(by_coll))
    assert report["ok"] and any("ops_batch_hist" in a for a in report["acceptedAlias"])

    # runner-спецификации: TTL-индекс с верным expireAfterSeconds эквивалентен
    uniq_spec = next(s for s in manifest["indexes"] if s["name"] == "discord_audit_guildId_entryId_unique")
    state_spec = next(s for s in manifest["indexes"] if s["name"] == "discord_audit_state_guildId_unique")
    # T11 (M4): состояние синхронизации аудита — unique(guildId), инвариант H08
    assert state_spec["owner"] == "runner" and state_spec["unique"] is True
    def runner_db(ttl_doc: dict) -> dict[str, list[dict]]:
        return {"operations": [ttl_doc], "discord_audit_logs": [_actual_doc(uniq_spec)],
                "discord_audit_state": [_actual_doc(state_spec)]}

    report = schema_contract.verify_web_schema(Db(runner_db(_actual_doc(ttl_spec))), owners=("runner",))
    assert report["ok"]
    # а с изменённым TTL — несовместимость поднимается
    warped = _actual_doc(ttl_spec)
    warped["expireAfterSeconds"] = 60
    with pytest.raises(schema_contract.SchemaIncompatible):
        schema_contract.verify_web_schema(Db(runner_db(warped)), owners=("runner",))
