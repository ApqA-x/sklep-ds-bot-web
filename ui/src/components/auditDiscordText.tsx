import type { ReactNode } from "react";
import type { DiscordAuditEntry } from "../api/types";
import { DName } from "../names";
import { UserLink } from "./userLink";

function RoleChip({ id, name }: { id: string; name?: string }) {
  return <span className="chip tiny-chip">{name || <DName kind="role" id={id} />}</span>;
}

function roleList(value: unknown): ReactNode {
  if (!Array.isArray(value)) return null;
  const roles = value as { id?: string; name?: string }[];
  if (roles.length === 0) return null;
  return (
    <>
      {roles.map((r, i) => (
        <RoleChip key={r.id ?? i} id={String(r.id ?? "")} name={r.name} />
      ))}
    </>
  );
}

// Права Discord: бит → читаемое имя
const PERMISSIONS: [bigint, string][] = [
  [1n << 0n, "создавать приглашения"],
  [1n << 1n, "кикать участников"],
  [1n << 2n, "банить участников"],
  [1n << 3n, "администратор"],
  [1n << 4n, "управлять каналами"],
  [1n << 5n, "управлять сервером"],
  [1n << 6n, "добавлять реакции"],
  [1n << 7n, "видеть журнал аудита"],
  [1n << 8n, "приоритетный режим"],
  [1n << 9n, "транслировать видео"],
  [1n << 10n, "видеть канал"],
  [1n << 11n, "отправлять сообщения"],
  [1n << 12n, "отправлять TTS-сообщения"],
  [1n << 13n, "управлять сообщениями"],
  [1n << 14n, "встраивать ссылки"],
  [1n << 15n, "прикреплять файлы"],
  [1n << 16n, "видеть историю сообщений"],
  [1n << 17n, "упоминать @everyone и всех"],
  [1n << 18n, "использовать внешние эмодзи"],
  [1n << 19n, "видеть статистику сервера"],
  [1n << 20n, "подключаться к голосу"],
  [1n << 21n, "говорить"],
  [1n << 22n, "глушить участников"],
  [1n << 23n, "оглушать участников"],
  [1n << 24n, "перемещать участников"],
  [1n << 25n, "использовать детектор речи"],
  [1n << 26n, "менять свой ник"],
  [1n << 27n, "управлять никами"],
  [1n << 28n, "управлять ролями"],
  [1n << 29n, "управлять вебхуками"],
  [1n << 30n, "управлять эмодзи и стикерами"],
  [1n << 31n, "использовать слэш-команды"],
  [1n << 32n, "запрашивать слово"],
  [1n << 33n, "управлять событиями"],
  [1n << 34n, "управлять тредами"],
  [1n << 35n, "создавать публичные треды"],
  [1n << 36n, "создавать приватные треды"],
  [1n << 37n, "использовать внешние стикеры"],
  [1n << 38n, "писать в тредах"],
  [1n << 39n, "использовать встроенные активности"],
  [1n << 40n, "модерировать участников"],
  [1n << 41n, "использовать внешние звуки"],
  [1n << 42n, "отправлять голосовые сообщения"],
];

function parseBits(value: unknown): bigint | null {
  if (value === null || value === undefined || value === "") return null;
  try {
    return BigInt(String(value));
  } catch {
    return null;
  }
}

// Биты из таблицы PERMISSIONS — остальное показываем обобщённо, чтобы не
// молчать о новых флагах Discord, которых ещё нет в словаре
const KNOWN_MASK = PERMISSIONS.reduce((acc, [bit]) => acc | bit, 0n);

export function permDiff(oldV: unknown, newV: unknown): { added: string[]; removed: string[] } {
  const before = parseBits(oldV) ?? 0n;
  const after = parseBits(newV) ?? 0n;
  const addedBits = after & ~before;
  const removedBits = before & ~after;
  const added: string[] = [];
  const removed: string[] = [];
  for (const [bit, label] of PERMISSIONS) {
    if ((addedBits & bit) !== 0n) added.push(label);
    if ((removedBits & bit) !== 0n) removed.push(label);
  }
  if ((addedBits & ~KNOWN_MASK) !== 0n) added.push("прочие новые права");
  if ((removedBits & ~KNOWN_MASK) !== 0n) removed.push("прочие снятые права");
  return { added, removed };
}

const CHANGE_LABELS: Record<string, string> = {
  name: "название",
  permissions: "права",
  color: "цвет",
  hoist: "показывать отдельно",
  mentionable: "можно упоминать",
  position: "позиция",
  user_limit: "лимит участников",
  bitrate: "битрейт",
  rtc_region: "регион голоса",
  nsfw: "NSFW",
  rate_limit_per_user: "замедление",
  topic: "тема канала",
  default_auto_archive_duration: "авто-архив тредов",
  nick: "ник",
  avatar: "аватар",
  channel: "канал",
  type: "тип",
  flags: "флаги",
  icon: "иконка",
  banner: "банер",
  description: "описание",
};

function fmtValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "да" : "нет";
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    if (typeof obj.name === "string") return obj.name;
    if (Array.isArray(value)) return `${(value as unknown[]).length} шт`;
    return JSON.stringify(value);
  }
  return String(value);
}

const ROLE_KEYS = new Set(["$add", "$+", "$remove", "$-"]);

function Line({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="audit-line">
      <b>{label}:</b> {children}
    </div>
  );
}

// Развёрнутая карточка записи журнала: кто, что, над кем, роли, права, причина
export function DiscordAuditFull({ entry }: { entry: DiscordAuditEntry }) {
  const added = entry.changes.find((c) => c.key === "$add" || c.key === "$+");
  const removed = entry.changes.find((c) => c.key === "$remove" || c.key === "$-");
  const permChanges = entry.changes.filter((c) => c.key === "permissions");
  const others = entry.changes.filter((c) => !ROLE_KEYS.has(c.key) && c.key !== "permissions");
  const opt = entry.options || {};

  return (
    <div className="audit-full">
      <Line label="Кто">
        {entry.actorUserId ? (
          <UserLink userId={entry.actorUserId} name={entry.actorName || undefined} />
        ) : (
          "система"
        )}
      </Line>
      <Line label="Что">
        {entry.action} <span className="muted tiny">(тип {entry.actionType})</span>
      </Line>
      <Line label="Над кем">
        {entry.targetUserId ? (
          <UserLink userId={entry.targetUserId} name={entry.targetUserName || undefined} />
        ) : entry.targetId ? (
          <span title={entry.targetId}>
            <DName kind="role" id={entry.targetId} />
          </span>
        ) : (
          "—"
        )}
      </Line>
      {(added?.new || removed?.new) ? (
        <Line label="Роли">
          {added?.new ? <>выдано: {roleList(added.new)} </> : null}
          {removed?.new ? <>снято: {roleList(removed.new)}</> : null}
        </Line>
      ) : null}
      {permChanges.map((c, i) => {
        const d = permDiff(c.old, c.new);
        if (d.added.length === 0 && d.removed.length === 0) return null;
        return (
          <Line label="Права роли" key={i}>
            {d.added.map((p) => (
              <span className="chip tiny-chip perm-add" key={`+${p}`}>
                +{p}
              </span>
            ))}
            {d.removed.map((p) => (
              <span className="chip tiny-chip perm-del" key={`-${p}`}>
                −{p}
              </span>
            ))}
          </Line>
        );
      })}
      {others.length > 0 && (
        <Line label="Изменения">
          {others.map((c, i) => (
            <span key={i} className="muted tiny diff-line">
              {i > 0 && "; "}
              {CHANGE_LABELS[c.key] ?? c.key}: {fmtValue(c.old)} → {fmtValue(c.new)}
            </span>
          ))}
        </Line>
      )}
      {entry.channelId && (
        <Line label="Где">
          <DName kind="channel" id={entry.channelId} />
        </Line>
      )}
      {entry.reason ? <Line label="Причина">{entry.reason}</Line> : null}
      {(opt.code || opt.delete_message_days || opt.count || opt.channel_type || opt.message_id) && (
        <Line label="Детали">
          {[
            opt.code ? `инвайт: ${opt.code}` : "",
            opt.count ? `задействовано: ${opt.count}` : "",
            opt.delete_message_days && opt.delete_message_days !== "0"
              ? `удалено сообщений за ${opt.delete_message_days} дн.`
              : "",
            opt.channel_type ? `тип канала: ${opt.channel_type}` : "",
            opt.message_id ? `сообщение: ${opt.message_id}` : "",
          ]
            .filter(Boolean)
            .join(" · ")}
        </Line>
      )}
    </div>
  );
}
