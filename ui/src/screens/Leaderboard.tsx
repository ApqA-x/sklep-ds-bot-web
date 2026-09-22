import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import {
  Bar,
  BarChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api } from "../api/client";
import type { Period } from "../api/types";
import { ChartControls, Grid, useGridPref } from "../components/charts";
import { ErrorBox, Loading, Section } from "../components/ui";
import { fmtDuration, toHours } from "../lib/format";

const PERIODS: Period[] = ["7d", "30d", "all"];
const TOPS = [5, 10, 20, 50];

export default function Leaderboard() {
  const { guildId = "" } = useParams();
  const [period, setPeriod] = useState<Period>("30d");
  const [top, setTop] = useState(10);
  const [grid, setGrid] = useGridPref();
  const query = useQuery({
    queryKey: ["leaderboard", guildId, period, 50],
    queryFn: () => api.leaderboard(guildId, period, 50),
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const items = query.data?.items ?? [];
  const chart = items.slice(0, top).map((i) => ({
    name: i.userName,
    hours: toHours(i.totalMs),
    ms: i.totalMs,
  }));
  const height = Math.max(260, top * 26);

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
        <Section title={`Топ-${top}, часы в голосе`}>
          <ChartControls top={top} setTop={setTop} topOptions={TOPS} grid={grid} setGrid={setGrid} />
          <div className="chart" style={{ height }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chart}>
                <Grid show={grid} />
                <XAxis dataKey="name" interval={0} angle={-20} height={50} textAnchor="end" />
                <YAxis />
                <Tooltip
                  formatter={(value, _name, entry) => [
                    `${fmtDuration((entry?.payload as { ms?: number } | undefined)?.ms ?? Number(value) * 3_600_000)} (${value} ч)`,
                    "в голосе",
                  ]}
                />
                <Bar dataKey="hours" fill="#cba6f7" />
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
