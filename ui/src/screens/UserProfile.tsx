import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, useCanWrite } from "../api/client";
import type { Period } from "../api/types";
import { Empty, ErrorBox, Loading, OptionSelect, roleColorCss, Section, type PickerOption } from "../components/ui";
import { discordUserUrl, fmtDate, fmtDuration } from "../lib/format";
import { DName, usePicker } from "../names";

const PERIODS: Period[] = ["7d", "30d", "all"];

export default function UserProfile() {
  const { guildId = "", userId = "" } = useParams();
  const canWrite = useCanWrite(guildId);
  const [period, setPeriod] = useState<Period>("30d");
  const query = useQuery({
    queryKey: ["user", guildId, userId, period],
    queryFn: () => api.userProfile(guildId, userId, period),
  });
  const picker = usePicker(guildId);

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const p = query.data;
  if (!p) return null;
  const daily = p.daily.map((d) => ({
    date: d.date.slice(5),
    hours: Math.round((d.ms / 3_600_000) * 100) / 100,
  }));

  return (
    <>
      <div className="toolbar">
        {PERIODS.map((x) => (
          <button key={x} className={x === period ? "chip active" : "chip"} onClick={() => setPeriod(x)}>
            {x}
          </button>
        ))}
      </div>
      <Section title={p.userName}>
        <p className="muted">
          <a href={discordUserUrl(p.userId)} target="_blank" rel="noreferrer">
            {p.userId}
          </a>{" "}
          · заходов за период: {p.appearances} · время: {fmtDuration(p.totalMs)}
        </p>
        {daily.length > 0 && (
          <div className="chart">
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={daily}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="date" />
                <YAxis />
                <Tooltip />
                <Line type="monotone" dataKey="hours" stroke="#cba6f7" dot={false} />
              </LineChart>
            </ResponsiveContainer>
          </div>
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
      <Section title={`Роли (${p.roleIds.length})`}>
        <div className="chips">
          {p.roleIds.map((r) => {
            const meta = picker.data?.roles.find((x) => x.id === r);
            const color = roleColorCss(meta?.color);
            return (
              <span className="chip role-chip" key={r} title={r}>
                {color && <span className="opt-dot" style={{ background: color }} />}
                <DName kind="role" id={r} />
              </span>
            );
          })}
        </div>
      </Section>
      {canWrite && <ActionPanel guildId={guildId} userId={userId} roleIds={p.roleIds} />}
    </>
  );
}

function ActionPanel({ guildId, userId, roleIds }: { guildId: string; userId: string; roleIds: string[] }) {
  const [roleId, setRoleId] = useState("");
  const [channelId, setChannelId] = useState("");
  const [reason, setReason] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const picker = usePicker(guildId);
  const queryClient = useQueryClient();

  const run = useMutation({
    mutationFn: async ({ kind }: { kind: string }) => {
      switch (kind) {
        case "grant":
          return api.botRole(guildId, userId, roleId, "grant");
        case "revoke":
          return api.botRole(guildId, userId, roleId, "revoke");
        case "mute":
          return api.botTimeout(guildId, userId, true);
        case "unmute":
          return api.botTimeout(guildId, userId, false);
        case "move":
          return api.botMove(guildId, userId, channelId);
        case "kick":
          return api.botKick(guildId, userId, reason);
        default:
          throw new Error("unknown action");
      }
    },
    onSuccess: (_data, vars) => {
      setNotice(`выполнено: ${vars.kind}`);
      queryClient.invalidateQueries({ queryKey: ["user", guildId, userId] });
      queryClient.invalidateQueries({ queryKey: ["audit", guildId] });
    },
    onError: (err: Error) => setNotice(err.message),
  });
  const busy = run.isPending;
  const isId = (v: string) => /^\d{5,25}$/.test(v);

  const held = new Set(roleIds);
  const roles = picker.data?.roles ?? [];
  const channels = picker.data?.voiceChannels ?? [];
  const roleOptions: PickerOption[] = roles.map((r) => ({
    id: r.id,
    name: r.name,
    color: r.color,
    note: !r.assignable ? "неуправляема" : held.has(r.id) ? "есть" : undefined,
    disabled: !r.assignable,
  }));
  const channelOptions: PickerOption[] = channels.map((c) => ({
    id: c.id,
    name: c.name,
    note: c.type === 13 ? "stage" : undefined,
  }));
  const selectedRole = roles.find((r) => r.id === roleId);

  return (
    <Section title="Действия от имени бота">
      {notice && <p className="hint">{notice}</p>}
      <div className="actions-grid">
        <div className="action">
          {roleOptions.length > 0 ? (
            <OptionSelect placeholder="выбери роль" options={roleOptions} value={roleId} onChange={setRoleId} disabled={busy} />
          ) : (
            <input value={roleId} onChange={(e) => setRoleId(e.target.value)} placeholder="role id" />
          )}
          <button
            disabled={busy || (!isId(roleId) && !selectedRole) || (selectedRole ? !selectedRole.assignable || held.has(roleId) : false)}
            onClick={() => run.mutate({ kind: "grant" })}
          >
            выдать роль
          </button>
          <button
            disabled={busy || (!isId(roleId) && !selectedRole) || (selectedRole ? !selectedRole.assignable || !held.has(roleId) : false)}
            onClick={() => run.mutate({ kind: "revoke" })}
          >
            снять роль
          </button>
        </div>
        <div className="action">
          <button disabled={busy} onClick={() => run.mutate({ kind: "mute" })}>
            mute 10 мин
          </button>
          <button disabled={busy} onClick={() => run.mutate({ kind: "unmute" })}>
            снять mute
          </button>
        </div>
        <div className="action">
          {channelOptions.length > 0 ? (
            <OptionSelect
              placeholder="выбери голосовой канал"
              options={channelOptions}
              value={channelId}
              onChange={setChannelId}
              disabled={busy}
            />
          ) : (
            <input value={channelId} onChange={(e) => setChannelId(e.target.value)} placeholder="voice channel id" />
          )}
          <button disabled={busy || !isId(channelId)} onClick={() => run.mutate({ kind: "move" })}>
            переместить
          </button>
        </div>
        <div className="action">
          <input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="причина кика" />
          <button
            disabled={busy || reason.trim().length === 0}
            onClick={() => {
              if (window.confirm(`Кикнуть пользователя ${userId}?`)) run.mutate({ kind: "kick" });
            }}
          >
            кикнуть
          </button>
        </div>
      </div>
    </Section>
  );
}
