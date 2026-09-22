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
import { ErrorBox, Loading, Section } from "../components/ui";
import { fmtDuration, toHours } from "../lib/format";

const PERIODS: Period[] = ["7d", "30d", "all"];

export default function Leaderboard() {
  const { guildId = "" } = useParams();
  const [period, setPeriod] = useState<Period>("30d");
  const query = useQuery({
    queryKey: ["leaderboard", guildId, period],
    queryFn: () => api.leaderboard(guildId, period, 50),
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const items = query.data?.items ?? [];
  const chart = items.slice(0, 10).map((i) => ({
    name: i.userName,
    hours: toHours(i.totalMs),
  }));

  return (
    <>
      <div className="toolbar">
        {PERIODS.map((p) => (
          <button
            key={p}
            className={p === period ? "chip active" : "chip"}
            onClick={() => setPeriod(p)}
          >
            {p}
          </button>
        ))}
      </div>
      {items.length > 0 && (
        <Section title="Топ-10, часы в голосе">
          <div className="chart">
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={chart}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" interval={0} angle={-20} height={50} textAnchor="end" />
                <YAxis />
                <Tooltip />
                <Bar dataKey="hours" fill="#5865f2" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
      )}
      <Section title={`Лидерборд (${items.length})`}>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Пользователь</th>
              <th>Время</th>
              <th>Заходов</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, index) => (
              <tr key={item.userId}>
                <td>{index + 1}</td>
                <td>
                  <Link to={`/g/${guildId}/users/${item.userId}`}>{item.userName}</Link>
                </td>
                <td>{fmtDuration(item.totalMs)}</td>
                <td>{item.appearances}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </>
  );
}
