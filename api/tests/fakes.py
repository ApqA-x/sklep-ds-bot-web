from __future__ import annotations

from typing import Any


def _matches(doc: dict, flt: dict | None) -> bool:
    if not flt:
        return True
    for key, cond in flt.items():
        value = doc.get(key)
        if isinstance(cond, dict):
            for op, want in cond.items():
                if op == "$gte":
                    if value is None or value < want:
                        return False
                elif op == "$lt":
                    if value is None or value >= want:
                        return False
                elif op == "$lte":
                    if value is None or value > want:
                        return False
                elif op == "$in":
                    if value not in want:
                        return False
                elif op == "$ne":
                    if value == want:
                        return False
                else:
                    raise AssertionError(f"FakeCollection: unsupported op {op}")
        else:
            if value != cond:
                return False
    return True


def _sorted(docs: list[dict], sort: Any) -> list[dict]:
    if not sort:
        return docs
    for key, direction in reversed(list(sort)):
        docs = sorted(docs, key=lambda d, k=key: (d.get(k) is None, d.get(k)), reverse=direction == -1)
    return docs


class FakeUpdateResult:
    def __init__(self, matched: int, modified: int, upserted_id: Any = None) -> None:
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_id = upserted_id


class FakeCollection:
    def __init__(
        self,
        name: str,
        docs: list[dict] | None = None,
        aggregate_results: list[list[dict]] | None = None,
        fail_create_index: bool = False,
    ) -> None:
        self.name = name
        self.docs = list(docs or [])
        self.aggregate_results = list(aggregate_results or [])
        self.fail_create_index = fail_create_index
        self.calls: list[tuple] = []

    def find(self, flt: dict | None = None, projection: Any = None, *, sort: Any = None, skip: int = 0, limit: int = 0, **kw):
        self.calls.append(("find", self.name, flt, {"sort": sort, "skip": skip, "limit": limit}))
        result = [dict(doc) for doc in self.docs if _matches(doc, flt)]
        result = _sorted(result, sort)
        if skip:
            result = result[skip:]
        if limit:
            result = result[:limit]
        return iter(result)

    def find_one(self, flt: dict | None = None, **kw):
        for doc in self.find(flt, **kw):
            return doc
        return None

    def count_documents(self, flt: dict | None = None, **kw):
        self.calls.append(("count_documents", self.name, flt))
        return sum(1 for doc in self.docs if _matches(doc, flt))

    def aggregate(self, pipeline: list[dict], **kw):
        self.calls.append(("aggregate", self.name, pipeline))
        if self.aggregate_results:
            return list(self.aggregate_results.pop(0))
        return []

    def create_index(self, keys: Any, **kw):
        self.calls.append(("create_index", self.name, keys, kw))
        if self.fail_create_index:
            raise RuntimeError("index build failed")
        return kw.get("name", "?")

    def _apply_update(self, doc: dict, update: dict, *, inserted: bool) -> None:
        for key, value in (update.get("$set") or {}).items():
            doc[key] = value
        if inserted:
            for key, value in (update.get("$setOnInsert") or {}).items():
                doc.setdefault(key, value)
        for key, value in (update.get("$addToSet") or {}).items():
            current = doc.setdefault(key, [])
            if value not in current:
                current.append(value)
        for key, value in (update.get("$pull") or {}).items():
            current = doc.get(key)
            if isinstance(current, list):
                doc[key] = [item for item in current if item != value]

    def update_one(self, flt: dict, update: dict, upsert: bool = False, **kw):
        self.calls.append(("update_one", self.name, flt, update, upsert))
        for doc in self.docs:
            if _matches(doc, flt):
                self._apply_update(doc, update, inserted=False)
                return FakeUpdateResult(1, 1)
        if upsert:
            new_doc = {k: v for k, v in flt.items() if not isinstance(v, dict)}
            self._apply_update(new_doc, update, inserted=True)
            self.docs.append(new_doc)
            return FakeUpdateResult(0, 0, upserted_id=new_doc.get("_id"))
        return FakeUpdateResult(0, 0)

    def insert_one(self, doc: dict, **kw):
        self.calls.append(("insert_one", self.name, doc))
        self.docs.append(dict(doc))
        return FakeUpdateResult(0, 0, upserted_id=doc.get("_id"))

    def delete_one(self, flt: dict, **kw):
        self.calls.append(("delete_one", self.name, flt))
        for index, doc in enumerate(self.docs):
            if _matches(doc, flt):
                self.docs.pop(index)
                return FakeUpdateResult(1, 0)
        return FakeUpdateResult(0, 0)


class FakeDB(dict):
    def __getitem__(self, name: str) -> FakeCollection:
        if name not in self:
            dict.__setitem__(self, name, FakeCollection(name))
        return dict.__getitem__(self, name)

    def collection(self, name: str) -> FakeCollection:
        return self[name]

    def call_log(self) -> list[tuple]:
        return [call for coll in dict.values(self) for call in coll.calls]
