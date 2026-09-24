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

// одно предложение на запись; неизвестные действия — JSON в сворачиваемом блоке
export function AuditDetails({ item }: { item: AuditItem }) {
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
        return (
          <details className="intervals">
            <summary>отправил сообщение</summary>
            <div className="bot-message-detail">
              <div>
                чат: <Channel id={String(after.channelId)} />{" "}
                <span className="muted tiny">({String(after.channelId)})</span>
              </div>
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
    return (
      <details className="intervals">
        <summary>детали</summary>
        <pre className="inline">{JSON.stringify(item.after ?? item.before ?? null)}</pre>
      </details>
    );
  }

  return (
    <>
      {body}
      {errorSuffix(item)}
    </>
  );
}
