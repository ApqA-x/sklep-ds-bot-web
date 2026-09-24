import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { Button } from "primereact/button";
import { InputText } from "primereact/inputtext";
import { SelectButton } from "primereact/selectbutton";
import { confirmDialog } from "primereact/confirmdialog";
import {
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, useCanWrite } from "../api/client";
import { PERIOD_LABELS, type Period } from "../api/types";
import { Grid } from "../components/charts";
import { Empty, ErrorBox, Loading, OptionSelect, roleColorCss, Section, type PickerOption } from "../components/ui";
import { discordUserUrl, fmtDate, fmtDayRu, fmtDuration } from "../lib/format";
import { DName, useMemberState, usePicker } from "../names";

const PERIODS: Period[] = ["7d", "30d", "all"];

type ChartKind = "hours" | "messages" | "invites";

const CHARTS: { key: ChartKind; label: string }[] = [
  { key: "hours", label: "Часы в войсе" },
  { key: "messages", label: "Сообщения" },
  { key: "invites", label: "Инвайты" },
];

function accentHex(value: number | null | undefined): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return `#${value.toString(16).padStart(6, "0")}`;
}

export default function UserProfile() {
  const { guildId = "", userId = "" } = useParams();
  const canWrite = useCanWrite(guildId);
  const [period, setPeriod] = useState<Period>("all");
  const [chartKind, setChartKind] = useState<ChartKind>("hours");
  const query = useQuery({
    queryKey: ["user", guildId, userId, period],
    queryFn: () => api.userProfile(guildId, userId, period),
  });
  const card = useQuery({
    queryKey: ["userCard", guildId, userId],
    queryFn: () => api.userCard(guildId, userId),
    staleTime: 300_000,
  });
  const picker = usePicker(guildId);
  const member = useMemberState(guildId, userId, true);
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<string | null>(null);

  // Роли берём из Discord вживую: после снятия роль сразу исчезает из списка, а
  // «выдать» снова становится доступной. Если API недоступен — откат на БД.
  const live = member.data?.source === "discord" ? member.data : null;
  const roleIds = live?.roleIds ?? query.data?.roleIds ?? [];

  const revoke = useMutation({
    mutationFn: (roleId: string) => api.botRole(guildId, userId, roleId, "revoke"),
    onSuccess: (_d, roleId) => {
      setNotice(`роль снята: ${picker.data?.roles.find((r) => r.id === roleId)?.name ?? roleId}`);
      queryClient.invalidateQueries({ queryKey: ["memberState", guildId, userId] });
      queryClient.invalidateQueries({ queryKey: ["user", guildId, userId] });
      queryClient.invalidateQueries({ queryKey: ["audit", guildId] });
    },
    onError: (err: Error) => setNotice(err.message),
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const p = query.data;
  if (!p) return null;
  const c = card.data;
  const displayName = c?.nick || c?.globalName || p.userName;
  const chartData =
    chartKind === "hours"
      ? p.daily.map((d) => ({ date: d.date, value: Math.round((d.ms / 3_600_000) * 100) / 100 }))
      : chartKind === "messages"
        ? p.dailyMessages.map((d) => ({ date: d.date, value: d.count }))
        : p.dailyInvites.map((d) => ({ date: d.date, value: d.count }));
  const byPosition = new Map((picker.data?.roles ?? []).map((r) => [r.id, r.position]));
  const orderedRoles = [...roleIds].sort((a, b) => (byPosition.get(b) ?? -1) - (byPosition.get(a) ?? -1));

  return (
    <>
      <div className="toolbar">
        <SelectButton
          className="chip-group"
          value={period}
          options={PERIODS.map((x) => ({ label: PERIOD_LABELS[x], value: x }))}
          optionValue="value"
          onChange={(e) => setPeriod(e.value as Period)}
        />
      </div>
      <div className="profile-card">
        <div
          className="profile-banner"
          style={c?.bannerUrl ? { backgroundImage: `url(${c.bannerUrl})` } : undefined}
        />
        <div className="profile-body">
          {c && (
            <img className="profile-avatar" src={c.avatarUrl} alt="" width={88} height={88} referrerPolicy="no-referrer" />
          )}
          <div className="profile-meta">
          <h2 style={c?.accentColor ? { color: accentHex(c.accentColor) ?? undefined } : undefined}>{displayName}</h2>
          {(c?.avatars.length ?? 0) > 0 && (
            <div className="avatar-history" title="История аватарок (копится с момента запуска фичи)">
              {c!.avatars.map((a) => (
                <a key={a.hash} href={a.url} target="_blank" rel="noreferrer">
                  <img
                    src={a.url}
                    alt={`${a.kind} ${fmtDate(a.firstSeen ?? "")}`}
                    width={44}
                    height={44}
                    referrerPolicy="no-referrer"
                  />
                </a>
              ))}
            </div>
          )}
          </div>
        </div>
      </div>
      <Section title="Информация">
        <p className="muted">
          {c?.source === "discord" && c.username ? (
            <>
              @{c.username}
              {c.globalName && c.globalName !== displayName ? <> · {c.globalName}</> : null}
              {c.joinedAt ? <> · на сервере с {fmtDate(c.joinedAt)}</> : null}
            </>
          ) : (
            "данные Discord о профиле недоступны"
          )}
        </p>
        <p className="muted">
          <a href={discordUserUrl(p.userId)} target="_blank" rel="noreferrer">
            {p.userId}
          </a>{" "}
          · период «{PERIOD_LABELS[period]}»: заходов {p.appearances} · время {fmtDuration(p.totalMs)} ·
          сообщений{" "}
          {p.messageCount.toLocaleString("ru-RU")} · пригласил {p.invitedCount}
        </p>
      </Section>
      <Section title={`Активность · период «${PERIOD_LABELS[period]}»`}>
        <div className="toolbar">
          <SelectButton
            className="chip-group"
            value={chartKind}
            options={CHARTS.map((x) => ({ label: x.label, value: x.key }))}
            optionValue="value"
            onChange={(e) => setChartKind(e.value as ChartKind)}
          />
        </div>
        {chartData.length > 0 ? (
          <div className="chart">
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={chartData}>
                <Grid />
                <XAxis dataKey="date" tickFormatter={(v: string) => v.slice(5)} />
                <YAxis />
                <Tooltip labelFormatter={(v) => fmtDayRu(String(v))} />
                <Line type="monotone" dataKey="value" stroke="#cba6f7" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <Empty>За этот период активности не было.</Empty>
        )}
      </Section>
      <Section title="Как попал на сервер">
        {p.join ? (
          <dl className="kv">
            <dt>Инвайт</dt>
            <dd>{p.join.inviteCode ?? "—"}</dd>
            <dt>Пригласил</dt>
            <dd>{p.join.inviterName ?? "—"}</dd>
            <dt>Статус атрибуции</dt>
            <dd>{p.join.attributionStatus ?? "—"}</dd>
            <dt>Дата входа</dt>
            <dd>{fmtDate(p.join.joinedAt)}</dd>
          </dl>
        ) : (
          <Empty>Нет данных о вступлении.</Empty>
        )}
      </Section>
      <Section title={`История никнеймов (${p.nicknames.length})`}>
        {p.nicknames.length === 0 ? (
          <Empty>Изменений никнейма не записано.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Когда</th>
                <th>Был</th>
                <th>Стал</th>
                <th>Источник</th>
              </tr>
            </thead>
            <tbody>
              {p.nicknames.map((n) => (
                <tr key={n.changedAt}>
                  <td>{fmtDate(n.changedAt)}</td>
                  <td>{n.previousNickname ?? "—"}</td>
                  <td>{n.nickname ?? "—"}</td>
                  <td>{n.source ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      <Section title={`Роли (${orderedRoles.length})`}>
        <p className="muted tiny">
          {live
            ? "актуальные роли с сервера"
            : "ролей на сервере не удалось получить — показаны данные из базы, они могут отставать"}
          {canWrite && orderedRoles.length > 0 ? " · × снимает роль" : ""}
        </p>
        {orderedRoles.length === 0 ? (
          <Empty>Ролей нет.</Empty>
        ) : (
          <div className="chips">
            {orderedRoles.map((r) => {
              const meta = picker.data?.roles.find((x) => x.id === r);
              const color = roleColorCss(meta?.color);
              return (
                <span className="chip role-chip" key={r} title={r}>
                  {color && <span className="opt-dot" style={{ background: color }} />}
                  <DName kind="role" id={r} />
                  {canWrite && (
                    <Button
                      className="chip-x"
                      type="button"
                      label="×"
                      aria-label={`снять роль ${meta?.name ?? r}`}
                      disabled={revoke.isPending || (meta ? !meta.assignable : false)}
                      onClick={() => revoke.mutate(r)}
                    />
                  )}
                </span>
              );
            })}
          </div>
        )}
      </Section>
      {canWrite && (
        <ActionPanel
          guildId={guildId}
          userId={userId}
          roleIds={roleIds}
          notice={notice}
          setNotice={setNotice}
          clearNotice={() => setNotice(null)}
        />
      )}
    </>
  );
}

const TIMEOUT_PRESETS: { label: string; seconds: number }[] = [
  { label: "5 мин", seconds: 300 },
  { label: "1 ч", seconds: 3600 },
  { label: "6 ч", seconds: 21600 },
  { label: "24 ч", seconds: 86400 },
];

type RunKind = "grant" | "revoke" | "mute" | "unmute" | "move" | "disconnect" | "kick";

function ActionPanel({
  guildId,
  userId,
  roleIds,
  notice,
  setNotice,
  clearNotice,
}: {
  guildId: string;
  userId: string;
  roleIds: string[];
  notice: string | null;
  setNotice: (v: string) => void;
  clearNotice: () => void;
}) {
  const [roleId, setRoleId] = useState("");
  const [channelId, setChannelId] = useState("");
  const [reason, setReason] = useState("");
  const [seconds, setSeconds] = useState(3600);
  const [error, setError] = useState<string | null>(null);
  const picker = usePicker(guildId);
  const member = useMemberState(guildId, userId, true);
  const queryClient = useQueryClient();

  const run = useMutation({
    mutationFn: async ({ kind }: { kind: RunKind }) => {
      switch (kind) {
        case "grant":
          return api.botRole(guildId, userId, roleId, "grant");
        case "revoke":
          return api.botRole(guildId, userId, roleId, "revoke");
        case "mute":
          return api.botTimeout(guildId, userId, true, seconds);
        case "unmute":
          return api.botTimeout(guildId, userId, false);
        case "move":
          return api.botMove(guildId, userId, channelId);
        case "disconnect":
          return api.botDisconnect(guildId, userId);
        case "kick":
          return api.botKick(guildId, userId, reason);
      }
    },
    onSuccess: (_data, vars) => {
      setError(null);
      clearNotice();
      queryClient.invalidateQueries({ queryKey: ["memberState", guildId, userId] });
      queryClient.invalidateQueries({ queryKey: ["user", guildId, userId] });
      queryClient.invalidateQueries({ queryKey: ["audit", guildId] });
      setNotice(LABELS[vars.kind]);
    },
    onError: (err: Error) => {
      clearNotice();
      setError(err.message);
    },
  });
  const busy = run.isPending;
  const isId = (v: string) => /^\d{5,25}$/.test(v);

  const held = new Set(roleIds);
  const roles = picker.data?.roles ?? [];
  const channels = picker.data?.voiceChannels ?? [];
  const live = member.data?.source === "discord" ? member.data : null;
  const voiceChannelId = live?.voiceChannelId ?? null;
  const timeoutUntil = live?.timeoutUntil ?? null;
  const roleOptions: PickerOption[] = roles.map((r) => ({
    id: r.id,
    name: r.name,
    color: r.color,
    note: !r.assignable ? "неуправляема" : held.has(r.id) ? "уже есть" : undefined,
    disabled: !r.assignable,
  }));
  const channelOptions: PickerOption[] = channels.map((c) => ({
    id: c.id,
    name: c.name,
    note: c.type === 13 ? "stage" : c.id === voiceChannelId ? "здесь" : undefined,
    disabled: c.id === voiceChannelId,
  }));
  const selectedRole = roles.find((r) => r.id === roleId);
  const roleReady = isId(roleId) || !!selectedRole;
  const canGrant = roleReady && (selectedRole ? selectedRole.assignable && !held.has(roleId) : true);
  const canRevoke = roleReady && (selectedRole ? selectedRole.assignable && held.has(roleId) : held.has(roleId));
  const moveDisabled = busy || !isId(channelId) || (live ? !voiceChannelId || channelId === voiceChannelId : false);

  return (
    <Section title="Действия от имени бота">
      {(notice || error) && (
        <p className={error ? "hint danger" : "hint"} onClick={clearNotice}>
          {error ?? notice}
        </p>
      )}
      {live ? (
        <div className="state-strip">
          {voiceChannelId ? (
            <span className="badge voice">
              в голосовом · <DName kind="channel" id={voiceChannelId} />
            </span>
          ) : (
            <span className="badge idle">не в голосовом канале</span>
          )}
          {timeoutUntil ? <span className="badge muted-until">тайм-аут до {fmtDate(timeoutUntil)}</span> : null}
        </div>
      ) : (
        <p className="muted tiny">
          Discord API сейчас не отдаёт состояние участника — часть кнопок может быть неточной, а роли показаны по базе.
        </p>
      )}

      <div className="actions-grid">
        <div className="action">
          <h4>Роли</h4>
          {roleOptions.length > 0 ? (
            <OptionSelect
              placeholder="выбери роль"
              options={roleOptions}
              value={roleId}
              onChange={setRoleId}
              disabled={busy}
            />
          ) : (
            <InputText value={roleId} onChange={(e) => setRoleId(e.target.value)} placeholder="role id" />
          )}
          <div className="action-row">
            <Button disabled={busy || !canGrant} onClick={() => run.mutate({ kind: "grant" })}>
              выдать роль
            </Button>
            <Button disabled={busy || !canRevoke} onClick={() => run.mutate({ kind: "revoke" })}>
              снять роль
            </Button>
          </div>
          <p className="muted tiny">
            Снять можно и крестиком в списке ролей выше — так роль сразу исчезает из карточки.
          </p>
        </div>

        <div className="action">
          <h4>Тайм-аут</h4>
          <SelectButton
            className="chip-group"
            value={seconds}
            options={TIMEOUT_PRESETS.map((t) => ({ label: t.label, value: t.seconds }))}
            optionValue="value"
            onChange={(e) => setSeconds(e.value as number)}
          />
          <div className="action-row">
            <Button disabled={busy} onClick={() => run.mutate({ kind: "mute" })}>
              в тайм-аут
            </Button>
            <Button disabled={busy || !timeoutUntil} onClick={() => run.mutate({ kind: "unmute" })}>
              снять тайм-аут
            </Button>
          </div>
        </div>

        <div className="action">
          <h4>Голосовой канал</h4>
          {channelOptions.length > 0 ? (
            <OptionSelect
              placeholder="выбери канал"
              options={channelOptions}
              value={channelId}
              onChange={setChannelId}
              disabled={busy}
            />
          ) : (
            <InputText
              value={channelId}
              onChange={(e) => setChannelId(e.target.value)}
              placeholder="voice channel id"
            />
          )}
          <div className="action-row">
            <Button disabled={moveDisabled} onClick={() => run.mutate({ kind: "move" })}>
              переместить
            </Button>
            <Button disabled={busy || !voiceChannelId} onClick={() => run.mutate({ kind: "disconnect" })}>
              выкинуть из канала
            </Button>
          </div>
          {live && !voiceChannelId && <p className="muted tiny">перемещение доступно, пока участник в голосовом</p>}
        </div>

        <div className="action danger">
          <h4>Кик с сервера</h4>
          <InputText
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="причина (в аудит-лог Discord)"
          />
          <Button
            severity="danger"
            disabled={busy || reason.trim().length === 0}
            onClick={() => {
              confirmDialog({
                header: "Кик с сервера",
                message: `Кикнуть пользователя ${userId} с сервера? Это можно будет исправить только инвайтом.`,
                icon: "pi pi-exclamation-triangle",
                acceptLabel: "Кикнуть",
                rejectLabel: "Отмена",
                acceptClassName: "p-button-danger",
                accept: () => run.mutate({ kind: "kick" }),
              });
            }}
          >
            кикнуть
          </Button>
        </div>
      </div>
    </Section>
  );
}

const LABELS: Record<RunKind, string> = {
  grant: "роль выдана",
  revoke: "роль снята",
  mute: "участник отправлен в тайм-аут",
  unmute: "тайм-аут снят",
  move: "перемещён в выбранный канал",
  disconnect: "отключён от голосового канала",
  kick: "пользователь удалён с сервера",
};
