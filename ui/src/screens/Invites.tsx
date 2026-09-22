import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
import type { Period } from "../api/types";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";
import { DName } from "../names";

const PERIODS: Period[] = ["7d", "30d", "all"];

export default function Invites() {
  const { guildId = "" } = useParams();
  const [period, setPeriod] = useState<Period>("30d");
  const query = useQuery({
    queryKey: ["invites", guildId, period],
    queryFn: () => api.invites(guildId, period),
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const data = query.data;
  if (!data) return null;
  const chart = data.byInviter.slice(0, 10);

  return (
    <>
      <div className="toolbar">
        {PERIODS.map((p) => (
          <button key={p} className={p === period ? "chip active" : "chip"} onClick={() => setPeriod(p)}>
            {p}
          </button>
        ))}
      </div>
      {chart.length > 0 && (
        <Section title="Привели участников (топ-10)">
          <div className="chart">
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={chart}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="userName" interval={0} angle={-20} height={50} textAnchor="end" />
                <YAxis allowDecimals={false} />
                <Tooltip />
                <Bar dataKey="count" fill="#a6e3a1" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
      )}
      <Section title={`Вступления (${data.attributions.length})`}>
        {data.attributions.length === 0 ? (
          <Empty>Вступлений за период не зафиксировано.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Когда</th>
                <th>Пользователь</th>
                <th>Инвайт</th>
                <th>Пригласил</th>
                <th>Статус</th>
                <th>Источник</th>
              </tr>
            </thead>
            <tbody>
              {data.attributions.map((a) => (
                <tr key={`${a.userId}-${a.joinedAt}`}>
                  <td>{fmtDate(a.joinedAt)}</td>
                  <td>
                    <Link to={`/g/${guildId}/users/${a.userId}`}>
                      <DName kind="user" id={a.userId} />
                    </Link>
                  </td>
                  <td>{a.inviteCode ?? "—"}</td>
                  <td>{a.inviterName ?? "—"}</td>
                  <td>{a.attributionStatus ?? "—"}</td>
                  <td>{a.source ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      <Section title={`Каталог инвайтов (${data.catalog.length})`}>
        <table>
          <thead>
            <tr>
              <th>Код</th>
              <th>Канал</th>
              <th>Создал</th>
              <th>Создан</th>
              <th>Удалён</th>
              <th>Виден</th>
            </tr>
          </thead>
          <tbody>
            {data.catalog.map((c) => (
              <tr key={c.code}>
                <td>{c.code}</td>
                <td><DName kind="channel" id={c.channelId} /></td>
                <td>{c.createdByName ?? "—"}</td>
                <td>{fmtDate(c.createdAt)}</td>
                <td>{c.deletedAt ? fmtDate(c.deletedAt) : "активен"}</td>
                <td>{fmtDate(c.lastSeenAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </>
  );
}
