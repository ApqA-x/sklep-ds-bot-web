// R26-03.6/7/8: состояние намерения до fetch, стабильные child-ключи, разбор unknown.
// Запуск: node --test src/api/intents.test.ts

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  childKey,
  clearAllIntents,
  clearIntent,
  fingerprint,
  loadIntent,
  parseOperationDetail,
  saveIntent,
} from "./intents.ts";

// минимальная подделка sessionStorage для чистых функций хранилища
class FakeStorage {
  map = new Map<string, string>();
  get length() {
    return this.map.size;
  }
  key(i: number) {
    return [...this.map.keys()][i] ?? null;
  }
  getItem(k: string) {
    return this.map.get(k) ?? null;
  }
  setItem(k: string, v: string) {
    this.map.set(k, v);
  }
  removeItem(k: string) {
    this.map.delete(k);
  }
}

test("child key is stable per batch+channel", () => {
  assert.equal(childKey("b-1", "140000"), childKey("b-1", "140000"));
  assert.notEqual(childKey("b-1", "140000"), childKey("b-1", "140001"));
  assert.match(childKey("b-1", "140000"), /^[A-Za-z0-9._:-]{1,128}$/); // принимается сервером
});

test("fingerprint changes only with payload", () => {
  const files = [{ name: "a.png", size: 10, lastModified: 1 }];
  assert.equal(fingerprint({ content: "x", files }), fingerprint({ content: "x", files: [...files] }));
  assert.notEqual(fingerprint({ content: "x", files }), fingerprint({ content: "y", files }));
  assert.notEqual(
    fingerprint({ content: "x", files: [{ name: "a.png", size: 11, lastModified: 1 }] }),
    fingerprint({ content: "x", files }),
  );
  assert.notEqual(fingerprint({ content: "x" }), fingerprint({ content: "x", embed: { title: "t" } }));
});

test("intent persists and clears via storage", () => {
  (globalThis as Record<string, unknown>).sessionStorage = new FakeStorage();
  saveIntent("170", "text", { batchId: "b-9", fp: "fp1" });
  assert.deepEqual(loadIntent("170", "text"), { batchId: "b-9", fp: "fp1" });
  assert.equal(loadIntent("171", "text"), null);
  saveIntent("170", "embed", { batchId: "b-10", fp: "fp2" });
  clearAllIntents();
  assert.equal(loadIntent("170", "text"), null);
  assert.equal(loadIntent("170", "embed"), null);
  saveIntent("170", "text", { batchId: "b-11", fp: "fp3" });
  clearIntent("170", "text");
  assert.equal(loadIntent("170", "text"), null);
  delete (globalThis as Record<string, unknown>).sessionStorage;
});

test("unknown outcome parses into a distinct view", () => {
  const unknown = parseOperationDetail({ error: "outcome_unproven", operationId: "op1", state: "unknown" });
  assert.equal(unknown?.unknown, true);
  assert.equal(unknown?.operationId, "op1");
  const fence = parseOperationDetail({ error: "ownership_lost", operationId: "op2", state: "executing" });
  assert.equal(fence?.unknown, true);
  const plain = parseOperationDetail({ error: "idempotency_conflict" }); // без operationId — не наш формат
  assert.equal(plain, null);
  assert.equal(parseOperationDetail("detail-string"), null);
});
