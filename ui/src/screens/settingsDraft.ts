// T07: состояние черновика настроек — serverSnapshot / draft / dirtyFields / baseRevision.
// Чистые функции (без React) — покрываются тестами node --test (settingsDraft.test.ts).

import type { GuildSettingsDoc } from "../api/types";

export const EDITABLE_KEYS = [
  "trackingMode",
  "trackedChannelIds",
  "summaryChannelId",
  "fallbackSummaryChannelId",
  "autoRoleId",
  "soundboardEnforcementEnabled",
  "autoRestoreRoles",
  "autoRestoreNicknames",
  "activityChannelId",
  "activityCategoryChannelIds",
  "activityEventTypes",
  "activityEventColors",
  "commandAccess",
] as const;

export type EditableKey = (typeof EDITABLE_KEYS)[number];

export const FIELD_LABELS: Record<EditableKey, string> = {
  trackingMode: "Режим трекинга",
  trackedChannelIds: "Трекаемые каналы",
  summaryChannelId: "Канал саммари",
  fallbackSummaryChannelId: "Запасной канал саммари",
  autoRoleId: "Autorole",
  soundboardEnforcementEnabled: "Soundboard-модерация",
  autoRestoreRoles: "Возвращать роли",
  autoRestoreNicknames: "Возвращать ник",
  activityChannelId: "Канал активности",
  activityCategoryChannelIds: "Каналы по категориям",
  activityEventTypes: "Типы событий",
  activityEventColors: "Цвета событий",
  commandAccess: "Доступ команд",
};

export type DraftField = {
  value: unknown;
  // серверное значение на момент, когда поле стало грязным (или на момент внешнего изменения)
  base: unknown;
  // поле изменилось на сервере, пока у был черновик — сравнить перед сохранением
  changed?: boolean;
};

export type Draft = {
  baseRevision: number;
  fields: Partial<Record<EditableKey, DraftField>>;
};

// явная семантика пустых/нулевых/false (T07.2): undefined/null → "", false остаётся false, 0 остаётся 0
export function normalize(value: unknown): unknown {
  return value === undefined || value === null ? "" : value;
}

export function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(normalize(a)) === JSON.stringify(normalize(b));
}

function serverRevision(doc: GuildSettingsDoc): number {
  return Number((doc as { revision?: unknown }).revision ?? 0);
}

export function emptyDraft(server: GuildSettingsDoc): Draft {
  return { baseRevision: serverRevision(server), fields: {} };
}

export function setField(draft: Draft, key: EditableKey, value: unknown, server: GuildSettingsDoc): Draft {
  const existing = draft.fields[key];
  const base = existing ? existing.base : server[key];
  const fields = { ...draft.fields };
  if (sameValue(value, base)) {
    delete fields[key];
  } else {
    fields[key] = { value, base, ...(existing?.changed ? { changed: true } : {}) };
  }
  return { ...draft, fields };
}

export function dirtyFields(draft: Draft): Partial<Record<EditableKey, unknown>> {
  const out: Partial<Record<EditableKey, unknown>> = {};
  for (const key of EDITABLE_KEYS) {
    const entry = draft.fields[key];
    if (entry && !sameValue(entry.value, entry.base)) out[key] = entry.value;
  }
  return out;
}

export function isDirty(draft: Draft): boolean {
  return Object.keys(dirtyFields(draft)).length > 0;
}

// принятый background response НЕ переписывает dirty draft (T07.1/U01):
// snapshot и baseRevision обновляются, пользовательские значения остаются;
// серверные значения, ставшие равными черновику, снимают пометку внешнего изменения
export function mergeServer(draft: Draft, server: GuildSettingsDoc): Draft {
  const fields: Draft["fields"] = {};
  for (const key of EDITABLE_KEYS) {
    const entry = draft.fields[key];
    if (!entry) continue;
    const serverNow = server[key];
    if (sameValue(entry.value, serverNow)) {
      continue; // значение совпало с сервером — больше не черновик
    }
    const moved = !sameValue(serverNow, entry.base);
    fields[key] = { value: entry.value, base: serverNow, changed: entry.changed || moved };
  }
  return { baseRevision: serverRevision(server), fields };
}

// успех save: baseline обновляется ответом сервера (T07.4); сохранённые поля снимаются,
// поля, изменённые пользователем во время запроса, остаются грязными относительно ответа
export function applySaved(draft: Draft, response: GuildSettingsDoc): Draft {
  const merged = mergeServer(draft, response);
  return merged;
}

// 409 revision_conflict: НЕ повторяем все старые значения автоматически (T07.3).
// baseRevision обновляется на свежий, поля с совпавшим серверным значением снимаются,
// остальные помечаются changed — UI предложит сравнить и применить выбранные поля
export function onConflict(draft: Draft, current: GuildSettingsDoc): Draft {
  return mergeServer(draft, current);
}

// принять серверное значение конкретного поля — черновик по ключу снимается
export function discardField(draft: Draft, key: EditableKey): Draft {
  const fields = { ...draft.fields };
  delete fields[key];
  return { ...draft, fields };
}

// --- in-memory хранилище черновиков по гильдии (не localStorage — черновики не переживают logout/перезагрузку) ---

const store = new Map<string, Draft>();

export function loadDraft(guildId: string): Draft | undefined {
  return store.get(guildId);
}

export function saveDraft(guildId: string, draft: Draft): void {
  if (isDirty(draft)) store.set(guildId, draft);
  else store.delete(guildId);
}

export function clearDraft(guildId: string): void {
  store.delete(guildId);
}

export function clearDrafts(): void {
  store.clear();
}

// предупреждение при смене гильдии: черновик в другой гильдии может быть несохранён
export function hasUnsavedDraft(exceptGuildId?: string): string | null {
  for (const [guildId, draft] of store) {
    if (guildId !== exceptGuildId && isDirty(draft)) return guildId;
  }
  return null;
}
