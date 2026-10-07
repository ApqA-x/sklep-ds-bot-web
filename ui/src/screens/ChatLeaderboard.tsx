import { useEffect, useState } from "react";
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
import { PERIOD_LABELS, type Period } from "../api/types";
import { ChartControls, Grid, useGridPref } from "../components/charts";
import { ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";
import { displayUserName, useNames } from "../names";

const TOPS = [5, 10, 20, 50];
const PAGE_SIZE = 50;

export function ChatBoard({ guildId, period, q }: { guildId: string; period: Period; q: string }) {
  const navigate = useNavigate();
  const names = useNames(guildId);
  const [page, setPage] = useState(1);
  const [top, setTop] = useState(10);
  const [grid, setGrid] = useGridPref();
  useEffect(() => setPage(1), [q, period]);
  const query = useQuery({
    queryKey: ["chat-leaderboard", guildId, period, PAGE_SIZE, page, q],
    queryFn: () => api.chatLeaderboard(guildId, period, PAGE_SIZE, page, q),
    placeholderData: (prev, previousQuery) => previousQuery?.queryKey[1] === guildId ? prev : undefined,
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const rankOffset = (page - 1) * PAGE_SIZE;
  const chart = items.slice(0, top).map((i) => ({
    name: displayUserName(names.data, i.userId, i.userName),
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
      <Section title={`Лидерборд по сообщениям · за период «${PERIOD_LABELS[period]}» · всего авторов: ${total}`}>
        <div className="toolbar leaderboard-pager">
          <span className="muted tiny">
            {total === 0
              ? q
                ? `ник не найден: «${q}»`
                : "нет сообщений за период"
              : `стр. ${page} из ${pages}: с ${rankOffset + 1} по ${rankOffset + items.length}`}
          </span>
          <span className="pager-controls">
            <Button icon="pi pi-arrow-left" aria-label="предыдущая страница" disabled={page <= 1} onClick={() => setPage((p) => p - 1)} />
            <span>{page} / {pages}</span>
            <Button icon="pi pi-arrow-right" aria-label="следующая страница" disabled={page >= pages} onClick={() => setPage((p) => p + 1)} />
          </span>
        </div>
        <table className="responsive-board">
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
                <td><span className="mobile-label">Место</span>{rankOffset + index + 1}</td>
                <td><span className="mobile-label">Пользователь</span>
                  <Link to={`/g/${guildId}/users/${item.userId}`}>
                    {displayUserName(names.data, item.userId, item.userName)}
                  </Link>
                </td>
                <td><span className="mobile-label">Сообщений</span>{item.messages}</td>
                <td><span className="mobile-label">Каналов</span>{item.channels}</td>
                <td><span className="mobile-label">Последнее</span>{fmtDate(item.lastMessageAt)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
    </>
  );
}
