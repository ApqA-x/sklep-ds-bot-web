// T07: логика черновика настроек (U01–U05). Запуск: node --test src/screens/settingsDraft.test.ts

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  applySaved,
  clearDraft,
  clearDrafts,
  discardField,
  dirtyFields,
  emptyDraft,
  hasUnsavedDraft,
  isDirty,
  loadDraft,
  mergeServer,
  normalize,
  onConflict,
  sameValue,
  saveDraft,
  setField,
} from "./settingsDraft.ts";

const server0 = { guildId: "1", revision: 5, activityEventColors: {}, trackingMode: "all", trustedUserIds: ["a"] };

test("U01: фоновый refetch (trusted add) обновляет snapshot/revision, но не трогает dirty draft", () => {
  let draft = emptyDraft(server0);
  draft = setField(draft, "activityEventColors", { member_join: 255 }, server0);
  assert.deepEqual(dirtyFields(draft), { activityEventColors: { member_join: 255 } });

  // server: добавлен trusted, revision 5→6, черновое поле не тронуто
  const server1 = { ...server0, revision: 6, trustedUserIds: ["a", "b"] };
  draft = mergeServer(draft, server1);

  assert.equal(draft.baseRevision, 6);
  assert.deepEqual(dirtyFields(draft), { activityEventColors: { member_join: 255 } });
  assert.equal(isDirty(draft), true);
});

test("U01 (сценарий готовности): изменить цвет → добавить trusted → сохранить цвет — ни одно действие не теряется", () => {
  let draft = emptyDraft(server0);
  draft = setField(draft, "activityEventColors", { member_join: 255 }, server0);
  const afterTrusted = { ...server0, revision: 6, trustedUserIds: ["a", "b"] };
  draft = mergeServer(draft, afterTrusted);
  const patch = { ...dirtyFields(draft), expectedRevision: draft.baseRevision };
  assert.deepEqual(patch, { activityEventColors: { member_join: 255 }, expectedRevision: 6 });
  // сервер применяет patch и отдаёт свежий документ (rev 7)
  const saved = { ...afterTrusted, revision: 7, activityEventColors: { member_join: 255 } };
  draft = applySaved(draft, saved);
  assert.equal(isDirty(draft), false);
  assert.equal(draft.baseRevision, 7);
});

test("внешнее изменение того же поля помечается changed, черновик сохраняется", () => {
  let draft = emptyDraft(server0);
  draft = setField(draft, "trackingMode", "specific", server0);
  const server1 = { ...server0, revision: 6, trackingMode: "none" };
  draft = mergeServer(draft, server1);
  assert.equal(draft.fields.trackingMode?.changed, true);
  assert.equal(draft.fields.trackingMode?.value, "specific");
  assert.deepEqual(dirtyFields(draft), { trackingMode: "specific" });
});

test("U02: 409 — baseRevision обновляется, совпавшие поля снимаются, авто-retry всех старых значений нет", () => {
  let draft = emptyDraft(server0);
  draft = setField(draft, "trackingMode", "specific", server0);
  draft = setField(draft, "summaryChannelId", "99", server0);
  // server current (из ответа 409): trackingMode уже "specific" (совпал), summaryChannelId изменён другим
  const current = { ...server0, revision: 9, trackingMode: "specific", summaryChannelId: "42" };
  draft = onConflict(draft, current);
  assert.equal(draft.baseRevision, 9);
  assert.equal(draft.fields.trackingMode, undefined); // нечего сохранять — значение уже на сервере
  assert.equal(draft.fields.summaryChannelId?.value, "99"); // черновик остался, но требует явного решения
  assert.equal(draft.fields.summaryChannelId?.changed, true);
  assert.deepEqual(dirtyFields(draft), { summaryChannelId: "99" });
});

test("U02: discardField принимает серверное значение", () => {
  let draft = emptyDraft(server0);
  draft = setField(draft, "summaryChannelId", "99", server0);
  draft = onConflict(draft, { ...server0, revision: 9, summaryChannelId: "42" });
  draft = discardField(draft, "summaryChannelId");
  assert.equal(isDirty(draft), false);
});

test("T07.2: normalize/false/0 — явная семантика пустых значений", () => {
  assert.equal(normalize(undefined), "");
  assert.equal(normalize(null), "");
  assert.equal(sameValue(undefined, ""), true);
  assert.equal(sameValue(null, ""), true);
  assert.equal(sameValue(false, ""), false);
  assert.equal(sameValue(false, 0), false);
  assert.equal(sameValue(0, ""), false);

  // поле отсутствует на сервере (undefined→""): включение false→true — грязное; возврат к false — чистое
  let draft = emptyDraft(server0);
  draft = setField(draft, "autoRestoreRoles", false, server0);
  assert.deepEqual(dirtyFields(draft), { autoRestoreRoles: false }); // server: undefined, value: false → dirty
  draft = setField(draft, "autoRestoreRoles", undefined, server0);
  assert.equal(draft.fields.autoRestoreRoles, undefined); // undefined == "" == server → не грязное
});

test("setField: значение, вернувшееся к base, снимает грязность", () => {
  let draft = emptyDraft(server0);
  draft = setField(draft, "trackingMode", "specific", server0);
  assert.equal(isDirty(draft), true);
  draft = setField(draft, "trackingMode", "all", server0);
  assert.equal(isDirty(draft), false);
});

test("U04: applySaved сохраняет поля, изменённые во время запроса", () => {
  let draft = emptyDraft(server0);
  draft = setField(draft, "trackingMode", "specific", server0);
  const response = { ...server0, revision: 6, trackingMode: "specific" };
  // отправлен trackingMode; пока шёл запрос, пользователь тронул summaryChannelId
  draft = setField(draft, "summaryChannelId", "77", server0);
  draft = applySaved(draft, response);
  assert.equal(draft.fields.trackingMode, undefined);
  assert.deepEqual(dirtyFields(draft), { summaryChannelId: "77" });
  assert.equal(draft.baseRevision, 6);
});

test("U03/U05: in-memory store — персист между mount/unmount одной гильдии, чистка по guild/logout", () => {
  clearDrafts();
  let draft = emptyDraft(server0);
  draft = setField(draft, "trackingMode", "specific", server0);
  saveDraft("1", draft);
  assert.deepEqual(loadDraft("1"), draft);
  assert.equal(hasUnsavedDraft("1"), null); // своя гильдия не считается
  assert.equal(hasUnsavedDraft("2"), "1"); // уход в другую гильдию — предупреждение
  clearDraft("1");
  assert.equal(loadDraft("1"), undefined);
  saveDraft("1", draft);
  saveDraft("2", draft);
  clearDrafts(); // logout
  assert.equal(loadDraft("1"), undefined);
  assert.equal(loadDraft("2"), undefined);
  assert.equal(hasUnsavedDraft(), null);
});

test("saveDraft не хранит чистый черновик (не засоряет store)", () => {
  clearDrafts();
  const clean = emptyDraft(server0);
  saveDraft("1", clean);
  assert.equal(loadDraft("1"), undefined);
});
