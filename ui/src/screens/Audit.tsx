import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";

export default function Audit() {
  const { guildId = "" } = useParams();
  const [page, setPage] = useState(1);
  const query = useQuery({
    queryKey: ["audit", guildId, page],
    queryFn: () => api.audit(guildId, page),
    placeholderData: (prev) => prev,
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;
  const data = query.data;
  if (!data) return null;

  const pages = Math.max(1, Math.ceil(data.total / data.size));

  return (
    <Section title={`Журнал изменений (${data.total})`}>
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
      {data.items.length === 0 ? (
        <Empty>Изменений пока нет.</Empty>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Когда</th>
              <th>Кто</th>
              <th>Действие</th>
              <th>Детали</th>
              <th>Итог</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((item, index) => (
              <tr key={`${item.at}-${index}`}>
                <td>{fmtDate(item.at)}</td>
                <td>{item.actorName || item.actorUserId}</td>
                <td>{item.action}</td>
                <td>
                  <pre className="inline">{JSON.stringify(item.after ?? item.before ?? null)}</pre>
                </td>
                <td>{item.ok ? "ok" : "ошибка"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}
