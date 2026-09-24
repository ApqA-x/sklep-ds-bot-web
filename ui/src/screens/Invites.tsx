import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
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
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";
import { DName } from "../names";

const TOPS = [5, 10, 20];

export function InvitesBoard({ guildId, period }: { guildId: string; period: Period }) {
  const navigate = useNavigate();
  const [top, setTop] = useState(10);
  const [grid, setGrid] = useGridPref();
  const query = useQuery({
    queryKey: ["invites", guildId, period],
    queryFn: () => api.invites(guildId, period),
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const data = query.data;
  if (!data) return null;
  const chart = data.byInviter.slice(0, top);

  return (
    <>
      {chart.length > 0 && (
        <Section title={`Привели участников (топ-${top})`}>
          <ChartControls top={top} setTop={setTop} topOptions={TOPS} grid={grid} setGrid={setGrid} />
          <div className="chart">
            <ResponsiveContainer width="100%" height={Math.max(220, top * 22)}>
              <BarChart data={chart}>
                <Grid show={grid} />
                <XAxis dataKey="userName" interval={0} angle={-20} height={50} textAnchor="end" />
                <YAxis allowDecimals={false} />
                <Tooltip formatter={(value) => [String(value), "приглашено"]} />
                <Bar
                  dataKey="count"
                  fill="#a6e3a1"
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
      <Section title={`Вступления · за период «${PERIOD_LABELS[period]}» (${data.attributions.length})`}>
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
