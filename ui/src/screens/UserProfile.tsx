import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
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
import { api } from "../api/client";
import type { Period } from "../api/types";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { discordUserUrl, fmtDate, fmtDuration } from "../lib/format";

const PERIODS: Period[] = ["7d", "30d", "all"];

export default function UserProfile() {
  const { guildId = "", userId = "" } = useParams();
  const [period, setPeriod] = useState<Period>("30d");
  const query = useQuery({
    queryKey: ["user", guildId, userId, period],
    queryFn: () => api.userProfile(guildId, userId, period),
  });

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
                <Line type="monotone" dataKey="hours" stroke="#5865f2" dot={false} />
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
          {p.roleIds.map((r) => (
            <span className="chip" key={r}>
              {r}
            </span>
          ))}
        </div>
      </Section>
    </>
  );
}
