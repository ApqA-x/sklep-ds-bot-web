import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";

export default function Sessions() {
  const { guildId = "" } = useParams();
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: ["sessions", guildId, page],
    queryFn: () => api.sessionsHistory(guildId, page),
    placeholderData: (prev) => prev,
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const pageData = query.data;
  if (!pageData) return null;
  const { items, total, size } = pageData;
  const pages = Math.max(1, Math.ceil(total / size));

  return (
    <Section title={`История сессий (${total})`}>
      <div className="toolbar">
        <button disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
          ←
        </button>
        <span>
          {page} / {pages}
        </span>
        <button disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>
          →
        </button>
      </div>
      {items.length === 0 ? (
        <Empty>Закрытых сессий нет.</Empty>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Канал</th>
              <th>Начало</th>
              <th>Конец</th>
              <th>Завершил</th>
              <th>Саммари</th>
            </tr>
          </thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.id}>
                <td>
                  <Link to={`/g/${guildId}/sessions/${s.id}`}>{s.channelId}</Link>
                </td>
                <td>{fmtDate(s.startedAt)}</td>
                <td>{fmtDate(s.endedAt)}</td>
                <td>{s.endedByUserId ?? "—"}</td>
                <td>{s.hasSummary ? "есть" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}
