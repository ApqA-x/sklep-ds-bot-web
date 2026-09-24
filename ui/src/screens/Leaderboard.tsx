import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Button } from "primereact/button";
import { SelectButton } from "primereact/selectbutton";
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
import { ChatBoard } from "./ChatLeaderboard";
import { InvitesBoard } from "./Invites";

const PERIODS: Period[] = ["7d", "30d", "all"];
const TOPS = [5, 10, 20, 50];
const PAGE_SIZE = 50;

type Board = "voice" | "chat" | "invites";

const BOARDS: { key: Board; label: string }[] = [
  { key: "voice", label: "Войс" },
  { key: "chat", label: "Топ чата" },
  { key: "invites", label: "Инвайты" },
];

export default function Leaderboard() {
  const { guildId = "" } = useParams();
  const [board, setBoard] = useState<Board>("voice");
  const [period, setPeriod] = useState<Period>("30d");

  return (
    <>
      <div className="toolbar">
        <SelectButton
          className="chip-group"
          value={board}
          options={BOARDS}
          optionValue="key"
          onChange={(e) => setBoard(e.value as Board)}
        />
        <span style={{ flex: 1 }} />
        <SelectButton
          className="chip-group"
          value={period}
          options={PERIODS.map((p) => ({ label: p, value: p }))}
          optionValue="value"
          onChange={(e) => setPeriod(e.value as Period)}
        />
      </div>
      {board === "voice" && <VoiceBoard key={period} guildId={guildId} period={period} />}
      {board === "chat" && <ChatBoard key={period} guildId={guildId} period={period} />}
      {board === "invites" && <InvitesBoard key={period} guildId={guildId} period={period} />}
    </>
  );
}

function VoiceBoard({ guildId, period }: { guildId: string; period: Period }) {
  const navigate = useNavigate();
  const [page, setPage] = useState(1);
  const [top, setTop] = useState(10);
  const [grid, setGrid] = useGridPref();
  const query = useQuery({
    queryKey: ["leaderboard", guildId, period, PAGE_SIZE, page],
    queryFn: () => api.leaderboard(guildId, period, PAGE_SIZE, page),
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
    hours: toHours(i.totalMs),
    ms: i.totalMs,
    userId: i.userId,
  }));
  const height = Math.max(260, top * 26);

  return (
    <>
      {items.length > 0 && (
        <Section title={`Топ-${top}, часы в голосе (стр. ${page})`}>
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
                <Bar
                  dataKey="hours"
                  fill="#cba6f7"
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
      <Section title={`Лидерборд — всего участников: ${total}`}>
        <div className="toolbar">
          <span className="muted tiny">
            {total === 0
              ? "нет данных за период"
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
              <th>Время</th>
              <th>Заходов</th>
            </tr>
          </thead>
          <tbody>
            {items.map((item, index) => (
              <tr key={item.userId}>
                <td>{rankOffset + index + 1}</td>
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
