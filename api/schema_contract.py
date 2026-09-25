"""T10: web-сторона контракта индексов.

Канонический manifest живёт в bot-репозитории (voice_tracker/schema.py);
api/schema_manifest.json — его точная копия (checksum проверяется тестом
test_schema_contract). Startup web'а: после своего ensure_web_indexes
(идемпотентное создание) проверяет фактические индексы по спецификации,
а не по имени (DB01/DB02): эквивалентный индекс под другим именем принимается,
несовпадение unique/partial/TTL при тех же ключах — ошибка старта, молча не чинится.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).with_name("schema_manifest.json")


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def manifest_checksum() -> str:
    return hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()


class SchemaIncompatible(RuntimeError):
    pass


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _flags_match(actual: dict, spec: dict) -> list[str]:
    diffs: list[str] = []
    if bool(actual.get("unique", False)) != bool(spec.get("unique")):
        diffs.append(f"unique: actual={bool(actual.get('unique', False))} expected={bool(spec.get('unique'))}")
    if bool(actual.get("sparse", False)) != bool(spec.get("sparse")):
        diffs.append(f"sparse: actual={bool(actual.get('sparse', False))} expected={bool(spec.get('sparse'))}")
    actual_partial = actual.get("partialFilterExpression")
    spec_partial = spec.get("partial")
    if (actual_partial is None) != (spec_partial is None) or (
        actual_partial is not None and _canon(actual_partial) != _canon(spec_partial)
    ):
        diffs.append(f"partialFilterExpression: actual={_canon(actual_partial)} expected={_canon(spec_partial)}")
    actual_ttl = actual.get("expireAfterSeconds")
    if actual_ttl != spec.get("ttl"):
        diffs.append(f"expireAfterSeconds: actual={actual_ttl} expected={spec.get('ttl')}")
    return diffs


def _actual_keys(index_doc: dict) -> list[list[int | str]]:
    return [[field, direction] for field, direction in index_doc.get("key", {}).items()]


def verify_web_schema(db: Any, owners: tuple[str, ...] = ("web", "shared")) -> dict:
    """Read-only сверка; None = окружение без list_indexes (unit-фейки) — пропуск."""
    manifest = load_manifest()
    specs = [s for s in manifest["indexes"] if s.get("owner") in owners]
    grouped: dict[str, list[dict]] = {}
    for spec in specs:
        coll = spec["collection"]
        if coll in grouped:
            continue
        collection = db[coll]
        lister = getattr(collection, "list_indexes", None)
        if lister is None:
            return {"skipped": True}
        try:
            grouped[coll] = [dict(doc) for doc in lister()]
        except Exception as exc:
            if "NamespaceNotFound" in exc.__class__.__name__ or "ns not found" in str(exc).lower():
                grouped[coll] = []
            else:
                raise
    incompatible: list[str] = []
    missing: list[str] = []
    accepted_alias: list[str] = []
    for spec in specs:
        candidates = [d for d in grouped[spec["collection"]]
                      if _actual_keys(d) == [list(k) for k in spec["keys"]]]
        exact = [d for d in candidates if not _flags_match(d, spec)]
        if exact:
            if exact[0].get("name") != spec["name"]:
                accepted_alias.append(f"{spec['collection']}.{spec['name']}~({exact[0].get('name')})")
            continue
        if candidates:
            diffs = _flags_match(candidates[0], spec)
            incompatible.append(
                f"{spec['collection']}{[k[0] for k in spec['keys']]} name={candidates[0].get('name')}: "
                + ", ".join(diffs))
        else:
            missing.append(f"{spec['collection']}.{spec['name']}")
    if incompatible:
        raise SchemaIncompatible(
            "web-часть контракта индексов нарушена (нужен план миграции через runner): "
            + "; ".join(incompatible))
    return {"ok": not missing, "missing": missing, "acceptedAlias": accepted_alias,
            "schemaVersion": manifest["schemaVersion"]}
