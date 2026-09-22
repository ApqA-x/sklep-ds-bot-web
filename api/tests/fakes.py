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


class FakeDB(dict):
    def __getitem__(self, name: str) -> FakeCollection:
        if name not in self:
            dict.__setitem__(self, name, FakeCollection(name))
        return dict.__getitem__(self, name)

    def collection(self, name: str) -> FakeCollection:
        return self[name]

    def call_log(self) -> list[tuple]:
        return [call for coll in dict.values(self) for call in coll.calls]
