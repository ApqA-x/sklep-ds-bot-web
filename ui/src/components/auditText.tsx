import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import type { AuditItem } from "../api/types";
import { DName } from "../names";

// «Над кем» совершено действие: после bot.* / списочных действий — после stalker это target из subscriptionId
export function targetUserId(item: AuditItem): string | null {
  const after = item.after as Record<string, unknown> | null;
  if (!after || typeof after !== "object") return null;
  const direct = after.userId;
  if (typeof direct === "string" && /^\d{5,25}$/.test(direct)) return direct;
  const sub = after.subscriptionId;
  if (typeof sub === "string") {
    const parts = sub.split(":");
    const last = parts[parts.length - 1];
    if (/^\d{5,25}$/.test(last)) return last;
  }
  return null;
}

function UserLink({ id }: { id: string }) {
  const { guildId = "" } = useParams();
  return (
    <Link to={`/g/${guildId}/users/${id}`} title={id}>
      <DName kind="user" id={id} />
    </Link>
  );
}

function Channel({ id }: { id: string }) {
  return <DName kind="channel" id={id} />;
}

function Role({ id }: { id: string }) {
  return <DName kind="role" id={id} />;
}

function fmtSeconds(value: unknown): string {
  const s = Number(value);
  if (!Number.isFinite(s) || s <= 0) return "";
  if (s < 60) return `${s} сек`;
  if (s < 3600) return `${Math.round(s / 60)} мин`;
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  return m ? `${h} ч ${m} мин` : `${h} ч`;
}

const SETTING_LABELS: Record<string, string> = {
  trackingMode: "режим трекинга",
  trackedChannelIds: "трекаемые каналы",
  summaryChannelId: "канал саммари",
  fallbackSummaryChannelId: "запасной канал саммари",
  autoRoleId: "авто-роль",
  autoUnmuteUserIds: "auto-unmute",
  trustedUserIds: "доверенные",
  soundboardEnforcementEnabled: "контроль звуковой доски",
  activityChannelId: "канал активности",
  activityCategoryChannelIds: "каналы по категориям",
  activityEventTypes: "типы событий",
  commandAccess: "доступ к командам",
  managedVoiceChannelId: "управляемый канал",
};

function idList(value: unknown, kind: "user" | "channel" | "role"): ReactNode {
  const ids = Array.isArray(value) ? value.map(String) : [];
  if (ids.length === 0) return <span className="muted">пусто</span>;
  const shown = ids.slice(0, 6);
  return (
    <>
      {shown.map((id, i) => (
        <span key={id}>
          {i > 0 && ", "}
          {kind === "channel" ? <Channel id={id} /> : kind === "role" ? <Role id={id} /> : <UserLink id={id} />}
        </span>
      ))}
      {ids.length > shown.length && ` и ещё ${ids.length - shown.length}`}
    </>
  );
}

function settingValue(key: string, value: unknown): ReactNode {
  if (typeof value === "boolean") return value ? "включено" : "выключено";
  if (value === null || value === undefined || value === "") return <span className="muted">сброшено</span>;
  if (typeof value === "string" && /^\d{5,25}$/.test(value)) {
    if (key === "autoRoleId") return <Role id={value} />;
    if (key.endsWith("ChannelId")) return <Channel id={value} />;
    return <UserLink id={value} />;
  }
  if (Array.isArray(value)) {
    if (key === "trackedChannelIds") return idList(value, "channel");
    if (key === "autoUnmuteUserIds" || key === "trustedUserIds") return idList(value, "user");
    return value.length ? `${value.length} шт: ${value.map(String).join(", ")}` : "пусто";
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    if (key === "commandAccess") {
      return entries.length
        ? entries.map(([n, a], i) => (
            <span key={n}>
              {i > 0 && ", "}
              /{n}: {String(a) === "admin" ? "ADMIN ONLY" : "все"}
            </span>
          ))
        : "сброшено к дефолтам";
    }
    if (key === "activityCategoryChannelIds") {
      return entries.length
        ? entries.map(([cat, ch], i) => (
            <span key={cat}>
              {i > 0 && ", "}
              {cat}: <Channel id={String(ch)} />
            </span>
          ))
        : "пусто";
    }
    return `${entries.length} записей`;
  }
  return String(value);
}

function errorSuffix(item: AuditItem): ReactNode {
  if (item.ok) return null;
  const after = (item.after ?? {}) as Record<string, unknown>;
  const detail = (after.detail ?? {}) as Record<string, unknown>;
  const message = typeof detail.message === "string" ? detail.message : "Discord отклонил запрос";
  const code = after.discordStatus != null ? ` (${String(after.discordStatus)}${detail.code ? `, ${String(detail.code)}` : ""})` : "";
  return (
    <span style={{ color: "var(--red)" }}>
      {" "}
      — не удалось: {message}
      {code}
    </span>
  );
}

// фолбэк для действий без специального кейса: читаемое описание вместо сырого JSON
const KEY_LABELS: Record<string, string> = {
  channelId: "канал",
  userId: "пользователь",
  roleId: "роль",
  guildId: "сервер",
  presetId: "пресет",
  subscriptionId: "подписка",
  text: "текст",
  name: "название",
  nickname: "ник",
  content: "содержимое",
  reason: "причина",
  seconds: "секунды",
  duration: "длительность",
  length: "длина",
  mute: "заглушение",
  ids: "список",
  code: "код",
  maxAge: "срок",
  maxUses: "использований",
  discordStatus: "ответ Discord",
  action: "действие",
};

const ACTION_LABELS: Record<string, string> = {
  "bot.timeout": "тайм-аут",
  "bot.move": "перемещение в голос",
  "bot.disconnect": "отключение от голоса",
  "bot.role": "изменение роли",
  "bot.kick": "кик",
  "bot.message": "сообщение от бота",
  "bot.invite.create": "создание инвайта",
  "bot.invite.delete": "удаление инвайта",
};

const COMMAND_REASON_LABELS: Record<string, string> = {
  disabled: "команда отключена в дашборде",
  permissions: "недостаточно прав",
  unknown: "неизвестная команда",
  error: "ошибка выполнения",
  rejected: "отклонено",
};

function humanizeAction(action: string): string {
  if (ACTION_LABELS[action]) return ACTION_LABELS[action];
  const last = action.includes(".") ? action.split(".").slice(-1)[0] : action;
  return last.charAt(0).toUpperCase() + last.slice(1);
}

function IdRef({ keyName, id }: { keyName: string; id: string }) {
  const k = keyName.toLowerCase();
  if (k.includes("role")) return <Role id={id} />;
  if (k.includes("channel")) return <Channel id={id} />;
  return <UserLink id={id} />;
}

function renderAuditValue(key: string, value: unknown): ReactNode {
  if (typeof value === "string" && /^\d{5,25}$/.test(value)) return <IdRef keyName={key} id={value} />;
  if (typeof value === "boolean") return value ? "включено" : "выключено";
  if (value === null || value === undefined || value === "") return <span className="muted">—</span>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="muted">пусто</span>;
    const singular = key.endsWith("s") ? key.slice(0, -1) : key;
    return (
      <>
        {value.map((v, i) => (
          <span key={i}>
            {i > 0 && ", "}
            {renderAuditValue(singular, v)}
          </span>
        ))}
      </>
    );
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    return (
      <>
        {entries.map(([k, v], i) => (
          <span key={k}>
            {i > 0 && "; "}
            {KEY_LABELS[k] ?? k}: {renderAuditValue(k, v)}
          </span>
        ))}
      </>
    );
  }
  return String(value);
}

function GenericDetails({ item }: { item: AuditItem }) {
  const after = (item.after ?? {}) as Record<string, unknown>;
  const before = (item.before ?? {}) as Record<string, unknown>;
  const hasAfter = Object.keys(after).length > 0;
  const source = hasAfter ? after : before;
  const wasBefore = hasAfter && Object.keys(before).length > 0;
  const skip = (k: string) => k === "detail" || (k === "discordStatus" && item.ok);
  const keys = Object.keys(source).filter((k) => !skip(k));
  return (
    <details className="intervals">
      <summary>{humanizeAction(item.action)}</summary>
      <div className="bot-message-detail">
        {keys.length === 0 && <span className="muted">нет подробностей</span>}
        {keys.map((k) => (
          <div key={k}>
            {KEY_LABELS[k] ?? k}: {renderAuditValue(k, source[k])}
            {wasBefore && before[k] !== undefined && (
              <span className="muted"> (было: {renderAuditValue(k, before[k])})</span>
            )}
          </div>
        ))}
      </div>
    </details>
  );
}

// вызов slash-команды бота в Discord (пишет services/commands.py бота, origin=discord)
function CommandCall({ item }: { item: AuditItem }) {
  const after = (item.after ?? {}) as Record<string, unknown>;
  const command =
    typeof after.command === "string" ? after.command : `/${item.action.slice("command.".length)}`;
  const options =
    after.options && typeof after.options === "object" ? (after.options as Record<string, unknown>) : {};
  const entries = Object.entries(options);
  const reason =
    typeof after.reason === "string" ? COMMAND_REASON_LABELS[after.reason] ?? after.reason : "";
  return (
    <>
      вызвал команду <code>{command}</code>
      {entries.length > 0 && (
        <details className="intervals">
          <summary>аргументы</summary>
          <div className="bot-message-detail">
            {entries.map(([k, v]) => (
              <div key={k}>
                {KEY_LABELS[k] ?? k}: {renderAuditValue(k, v)}
              </div>
            ))}
          </div>
        </details>
      )}
      {!item.ok && (
        <span style={{ color: "var(--red)" }}>
          {" "}
          — отклонено{reason ? `: ${reason}` : ""}
        </span>
      )}
    </>
  );
}

// карточка отправленного embed-блока в журнале — как предпросмотр на экране «Чат»,
// но без картинок: во вложении живой файл, в аудите остаётся только его имя
function EmbedAuditCard({ embed }: { embed: Record<string, unknown> }) {
  const color = typeof embed.color === "number" ? `#${(embed.color & 0xffffff).toString(16).padStart(6, "0")}` : "#000000";
  const title = typeof embed.title === "string" ? embed.title : null;
  const description = typeof embed.description === "string" ? embed.description : null;
  const author = typeof embed.author === "string" ? embed.author : null;
  const footer = typeof embed.footer === "string" ? embed.footer : null;
  const image = typeof embed.image === "string" ? embed.image : null;
  const thumbnail = typeof embed.thumbnail === "string" ? embed.thumbnail : null;
  return (
    <div className="embed-audit-card" style={{ borderLeftColor: color }}>
      {author && <div className="embed-author">{author}</div>}
      {title && <div className="embed-title">{title}</div>}
      {description && <div className="embed-description">{description}</div>}
      {(image || thumbnail) && (
        <div className="muted tiny">
          {image && <>вложение: {image}  </>}
          {thumbnail && <>миниатюра: {thumbnail}</>}
        </div>
      )}
      {footer && <div className="embed-footer">{footer}</div>}
    </div>
  );
}

// одно предложение на запись; неизвестные действия — читаемый фолбэк
export function AuditDetails({ item }: { item: AuditItem }) {
  if (item.action.startsWith("command.")) {
    return <CommandCall item={item} />;
  }
  const after = (item.after ?? {}) as Record<string, unknown>;
  const before = (item.before ?? {}) as Record<string, unknown>;
  const target = targetUserId(item);

  const body = (() => {
    switch (item.action) {
      case "bot.timeout":
        return after.mute ? (
          <>
            выдал тайм-аут {target && <UserLink id={String(after.userId)} />} на {fmtSeconds(after.seconds) || "?"}
          </>
        ) : (
          <>снял тайм-аут с {target && <UserLink id={String(after.userId)} />}</>
        );
      case "bot.move":
        return (
          <>
            переместил {target && <UserLink id={String(after.userId)} />} в <Channel id={String(after.channelId)} />
          </>
        );
      case "bot.disconnect":
        return <>выкинул из голоса {target && <UserLink id={String(after.userId)} />}</>;
      case "bot.role":
        return (
          <>
            {after.action === "grant" ? "выдал" : "снял"} роль <Role id={String(after.roleId)} /> у{" "}
            {target && <UserLink id={String(after.userId)} />}
          </>
        );
      case "bot.kick":
        return (
          <>
            выгнал с сервера {target && <UserLink id={String(after.userId)} />}
            {after.reason ? <> (причина: {String(after.reason)})</> : null}
          </>
        );
      case "bot.message": {
        const attachments = Array.isArray(after.attachments)
          ? (after.attachments as { name?: string; size?: number }[])
          : [];
        const content = typeof after.content === "string" ? after.content : null;
        const preview = typeof after.preview === "string" ? after.preview : null;
        const embed =
          after.embed && typeof after.embed === "object" ? (after.embed as Record<string, unknown>) : null;
        return (
          <details className="intervals">
            <summary>
              отправил сообщение{embed ? <> + embed-блок{typeof embed.title === "string" && embed.title ? ` «${String(embed.title).slice(0, 60)}»` : null}</> : null}
            </summary>
            <div className="bot-message-detail">
              <div>
                чат: <Channel id={String(after.channelId)} />{" "}
                <span className="muted tiny">({String(after.channelId)})</span>
              </div>
              {embed && <EmbedAuditCard embed={embed} />}
              <div>
                текст ({String(after.length)} симв.):
                {content !== null ? (
                  <pre className="inline">{content}</pre>
                ) : preview !== null ? (
                  <pre className="inline">{preview}</pre>
                ) : (
                  <span className="muted"> не сохранён (запись до обновления журнала)</span>
                )}
              </div>
              {attachments.length > 0 && (
                <div>
                  файлы:{" "}
                  {attachments.map((a, i) => (
                    <span key={i}>
                      {i > 0 && ", "}
                      {a.name ?? "?"}
                      {typeof a.size === "number" &&
                        ` (${a.size >= 1024 ? `${Math.round(a.size / 1024)} КБ` : `${a.size} Б`})`}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </details>
        );
      }
      case "chatPreset.add":
        return (
          <>
            сохранил пресет сообщения{after.name ? <> «{String(after.name)}»</> : null}
            {Array.isArray(after.channelIds) && (after.channelIds as string[]).length > 0 ? (
              <>
                {" "}
                → {(after.channelIds as string[]).map((id, i) => (
                  <span key={id}>
                    {i > 0 && ", "}
                    <Channel id={id} />
                  </span>
                ))}
              </>
            ) : null}
            {after.text ? <> : «{String(after.text).slice(0, 80)}{(String(after.text).length > 80 ? "…" : "")}»</> : null}
          </>
        );
      case "chatPreset.remove":
        return (
          <>
            удалил пресет сообщения{after.name ? <> «{String(after.name)}»</> : null}
            {Array.isArray(after.channelIds) && (after.channelIds as string[]).length > 0 ? (
              <>
                {" "}
                (каналы: {(after.channelIds as string[]).map((id, i) => (
                  <span key={id}>
                    {i > 0 && ", "}
                    <Channel id={id} />
                  </span>
                ))}
                )
              </>
            ) : null}
          </>
        );
      case "bot.invite.create":
        return (
          <>
            созвал инвайт из <Channel id={String(after.channelId)} />
          </>
        );
      case "bot.invite.delete":
        return <>удалил инвайт {String(after.code)}</>;
      case "trustedUserIds.add":
        return <>добавил {target && <UserLink id={target} />} в доверенные</>;
      case "trustedUserIds.remove":
        return <>убрал {target && <UserLink id={target} />} из доверенных</>;
      case "autoUnmuteUserIds.add":
        return <>добавил {target && <UserLink id={target} />} в auto-unmute</>;
      case "autoUnmuteUserIds.remove":
        return <>убрал {target && <UserLink id={target} />} из auto-unmute</>;
      case "stalker.add":
        return <>оформил stalker-подписку на {target && <UserLink id={target} />}</>;
      case "stalker.remove":
        return <>снял stalker-подписку с {target && <UserLink id={target} />}</>;
      case "settings.patch": {
        const keys = Object.keys(after);
        if (keys.length === 0) return null;
        return (
          <>
            изменил настройки:{" "}
            {keys.map((key, i) => (
              <span key={key}>
                {i > 0 && "; "}
                {SETTING_LABELS[key] ?? key}: {settingValue(key, after[key])}
                {before[key] !== undefined && (
                  <span className="muted"> (было: {settingValue(key, before[key])})</span>
                )}
              </span>
            ))}
          </>
        );
      }
      default:
        return null;
    }
  })();

  if (!body) {
    return <GenericDetails item={item} />;
  }

  return (
    <>
      {body}
      {errorSuffix(item)}
    </>
  );
}
