// R26-03.6/7/8: состояние намерения существует ДО fetch.
//
// Ключ идемпотентности нельзя генерировать внутри запроса — тогда каждый повтор
// (double-click, потеря ответа, reload страницы) уходит с новым ключом и создаёт
// второй эффект. Здесь чистые функции без зависимостей от React/fetch: их можно
// проверить node:test, а UI обязан вызывать их до обращения к API.
//
// Persistence: sessionStorage (не localStorage — не переживает закрытие вкладки),
// только минимальные ID и отпечаток payload. Тексты сообщений, embed-содержимое и
// любые токены здесь не хранятся (R26-03.8); при logout — полная очистка.

export interface ChatIntent {
  batchId: string;
  fp: string;
}

const PREFIX = "dsbot.chatIntent.";

export function childKey(batchId: string, channelId: string): string {
  // стабильный child-ключ на канал внутри батча: повтор батча с теми же каналом
  // и payload попадает в ту же операцию серверного журнала
  return `${batchId}:${channelId}`;
}

function djb2(input: string): string {
  let h = 5381;
  for (let i = 0; i < input.length; i++) h = ((h << 5) + h + input.charCodeAt(i)) | 0;
  return (h >>> 0).toString(36);
}

export interface FingerprintParts {
  content: string;
  embed?: unknown;
  files?: { name: string; size: number; lastModified: number }[];
}

export function fingerprint(parts: FingerprintParts): string {
  // отпечаток намерения: смена текста/embed/файлов => новое намерение (новый batchId);
  // identical payload => прежний batchId даже после перезагрузки страницы
  const files = (parts.files ?? []).map((f) => `${f.name}:${f.size}:${f.lastModified}`).join(",");
  const embed = parts.embed === undefined || parts.embed === null ? "" : JSON.stringify(parts.embed);
  return djb2(`${parts.content}\u001f${embed}\u001f${files}`);
}

function storage(): Storage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}

export function loadIntent(guildId: string, tab: string): ChatIntent | null {
  const raw = storage()?.getItem(PREFIX + guildId + "." + tab);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as ChatIntent;
    return typeof parsed.batchId === "string" && typeof parsed.fp === "string" ? parsed : null;
  } catch {
    return null;
  }
}

export function saveIntent(guildId: string, tab: string, intent: ChatIntent): void {
  storage()?.setItem(PREFIX + guildId + "." + tab, JSON.stringify(intent));
}

export function clearIntent(guildId: string, tab: string): void {
  storage()?.removeItem(PREFIX + guildId + "." + tab);
}

// logout/смена прав: доступ к старым статусам операций теряется вместе с намерениями
export function clearAllIntents(): void {
  const s = storage();
  if (!s) return;
  for (let i = s.length - 1; i >= 0; i--) {
    const key = s.key(i);
    if (key && key.startsWith(PREFIX)) s.removeItem(key);
  }
}

export interface OperationDetail {
  operationId?: string;
  state?: string;
  error?: string;
  unknown: boolean;
}

// R26-03.6: structured ApiError.detail → различимый исход. unknown/504 нельзя
// показывать как обычное «не отправлено»: эффект мог наступить, нужна сверка.
export function parseOperationDetail(detail: unknown): OperationDetail | null {
  if (typeof detail !== "object" || detail === null) return null;
  const d = detail as Record<string, unknown>;
  if (typeof d.operationId !== "string") return null;
  const state = typeof d.state === "string" ? d.state : undefined;
  return {
    operationId: d.operationId,
    state,
    error: typeof d.error === "string" ? d.error : undefined,
    unknown: state === "unknown" || d.error === "outcome_unproven" || d.error === "ownership_lost",
  };
}
