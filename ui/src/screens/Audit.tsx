import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";
import { DName } from "../names";

type OriginFilter = "" | "web" | "discord";

const FILTERS: { key: OriginFilter; label: string }[] = [
  { key: "", label: "Все" },
  { key: "web", label: "Через сайт" },
  { key: "discord", label: "Через Discord" },
];

function OriginBadge({ origin }: { origin: "web" | "discord" | undefined }) {
  const isDiscord = origin === "discord";
  return (
    <span className={isDiscord ? "badge discord" : "badge web"} title={isDiscord ? "выполнено через Discord API" : "изменение через сайт"}>
      {isDiscord ? "Discord" : "сайт"}
    </span>
  );
}

export default function Audit() {
  const { guildId = "" } = useParams();
  const [page, setPage] = useState(1);
  const [origin, setOrigin] = useState<OriginFilter>("");
  const query = useQuery({
    queryKey: ["audit", guildId, page, origin],
    queryFn: () => api.audit(guildId, page, 50, origin || undefined),
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
        {FILTERS.map((f) => (
          <button
            key={f.key || "all"}
            className={f.key === origin ? "chip active" : "chip"}
            onClick={() => {
              setOrigin(f.key);
              setPage(1);
            }}
          >
            {f.label}
          </button>
        ))}
        <span style={{ flex: 1 }} />
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
        <Empty>Изменений по этому фильтру пока нет.</Empty>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Когда</th>
              <th>Откуда</th>
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
                <td>
                  <OriginBadge origin={item.origin} />
                </td>
                <td title={item.actorUserId}>
                  {item.actorName ? (
                    item.actorName
                  ) : item.actorUserId ? (
                    <DName kind="user" id={item.actorUserId} />
                  ) : (
                    "—"
                  )}
                </td>
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
