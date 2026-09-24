import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { Button } from "primereact/button";
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
import { fmtDate } from "../lib/format";

const TOPS = [5, 10, 20, 50];
const PAGE_SIZE = 50;

export function ChatBoard({ guildId, period }: { guildId: string; period: Period }) {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [top, setTop] = useState(10);
  const [grid, setGrid] = useGridPref();
  const query = useQuery({
    queryKey: ["chat-leaderboard", guildId, period, PAGE_SIZE, page],
    queryFn: () => api.chatLeaderboard(guildId, period, PAGE_SIZE, page),
    placeholderData: (prev) => prev,
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const rankOffset = (page - 1) * PAGE_SIZE;
  const chart = items.slice(0, top).map((i) => ({
    name: i.userName,
    messages: i.messages,
    userId: i.userId,
  }));
  const height = Math.max(260, top * 26);

  return (
    <>
      {items.length > 0 && (
        <Section title={`Топ-${top}, сообщения (стр. ${page})`}>
          <ChartControls top={top} setTop={setTop} topOptions={TOPS} grid={grid} setGrid={setGrid} />
          <div className="chart" style={{ height }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chart}>
                <Grid show={grid} />
                <XAxis dataKey="name" interval={0} angle={-20} height={50} textAnchor="end" />
                <YAxis allowDecimals={false} />
                <Tooltip formatter={(value) => [`${value} сообщ.`, "сообщений"]} />
                <Bar
                  dataKey="messages"
                  fill="#a6da95"
                  cursor="pointer"
                  onClick={(state) => {
                    const uid =
                      (state?.payload as { userId?: string } | undefined)?.userId ??
                      (state as { userId?: string } | undefined)?.userId;
                    if (uid) navigate(`/g/${guildId}/users/${uid}`);
                  }}
                />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </Section>
      )}
      <Section title={`Лидерборд по сообщениям — всего авторов: ${total}`}>
        <div className="toolbar">
          <span className="muted tiny">
            {total === 0
              ? "нет сообщений за период"
              : `стр. ${page} из ${pages}: с ${rankOffset + 1} по ${rankOffset + items.length}`}
          </span>
          <span style={{ flex: 1 }} />
          <Button icon="pi pi-arrow-left" disabled={page <= 1} onClick={() => setPage((p) => p - 1)} />
          <span>
            {page} / {pages}
          </span>
          <Button icon="pi pi-arrow-right" disabled={page >= pages} onClick={() => setPage((p) => p + 1)} />
        </div>
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Пользователь</th>
              <th>Сообщений</th>
              <th>Каналов</th>
              <th>Последнее</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, index) => (
              <tr key={item.userId}>
                <td>{rankOffset + index + 1}</td>
                <td>
                  <Link to={`/g/${guildId}/users/${item.userId}`}>{item.userName}</Link>
                </td>
                <td>{item.messages}</td>
                <td>{item.channels}</td>
                <td>{fmtDate(item.lastMessageAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </>
  );
}
