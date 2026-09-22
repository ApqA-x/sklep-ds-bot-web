import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { AuditDetails } from "../components/auditText";
import { TargetUserPicker } from "../components/userSearch";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";
import { DName } from "../names";

type OriginFilter = "" | "web" | "discord";

const FILTERS: { key: OriginFilter; label: string }[] = [
  { key: "", label: "Все" },
  { key: "web", label: "Через сайт" },
  { key: "discord", label: "Через Discord" },
];

const OK_FILTERS: { key: "" | "1" | "0"; label: string }[] = [
  { key: "", label: "любой итог" },
  { key: "1", label: "успешные" },
  { key: "0", label: "ошибки" },
];

function OriginBadge({ origin }: { origin: "web" | "discord" | undefined }) {
  const isDiscord = origin === "discord";
  return (
    <span
      className={isDiscord ? "badge discord" : "badge web"}
      title={isDiscord ? "выполнено через Discord API" : "изменение через сайт"}
    >
      {isDiscord ? "Discord" : "сайт"}
    </span>
  );
}

export default function Audit() {
  const { guildId = "" } = useParams();
  const [page, setPage] = useState(1);
  const [showFilters, setShowFilters] = useState(false);
  const [origin, setOrigin] = useState<OriginFilter>("");
  const [okFilter, setOkFilter] = useState<"" | "1" | "0">("");
  const [action, setAction] = useState("");
  const [target, setTarget] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [sort, setSort] = useState<"asc" | "desc">("desc");

  const resetPage = () => setPage(1);

  const query = useQuery({
    queryKey: ["audit", guildId, page, origin, okFilter, action, target, dateFrom, dateTo, sort],
    queryFn: () =>
      api.audit(guildId, {
        page,
        size: 50,
        origin: origin || undefined,
        ok: okFilter || undefined,
        action: action || undefined,
        userId: target || undefined,
        dateFrom: dateFrom || undefined,
        dateTo: dateTo || undefined,
        sort,
      }),
    placeholderData: (prev) => prev,
  });

  const facets = useQuery({
    queryKey: ["audit-actions", guildId],
    queryFn: () => api.auditActions(guildId),
    staleTime: 300_000,
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;
  const data = query.data;
  if (!data) return null;

  const pages = Math.max(1, Math.ceil(data.total / data.size));
  const activeCount =
    (origin ? 1 : 0) +
    (okFilter ? 1 : 0) +
    (action ? 1 : 0) +
    (target ? 1 : 0) +
    (dateFrom ? 1 : 0) +
    (dateTo ? 1 : 0) +
    (sort !== "desc" ? 1 : 0);
  const hasFilters = activeCount > 0;

  const resetAll = () => {
    setOrigin("");
    setOkFilter("");
    setAction("");
    setTarget("");
    setDateFrom("");
    setDateTo("");
    setSort("desc");
    resetPage();
  };

  return (
    <Section title={`Журнал изменений (${data.total})`}>
      <div className="toolbar">
        <button
          className={showFilters || hasFilters ? "chip active" : "chip"}
          onClick={() => setShowFilters((v) => !v)}
        >
          Фильтры
          {activeCount > 0 && <span className="count-badge">{activeCount}</span>}
        </button>
        <span className="muted tiny">
          {hasFilters ? `активных фильтров: ${activeCount}` : "фильтры не заданы"}
        </span>
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
      {showFilters && (
        <div className="filters-panel">
          <div className="filter-block">
            <div className="filter-row">
              <span className="muted tiny">Источник</span>
              <div className="chips">
                {FILTERS.map((f) => (
                  <button
                    key={f.key || "all"}
                    className={f.key === origin ? "chip active" : "chip"}
                    onClick={() => {
                      setOrigin(f.key);
                      resetPage();
                    }}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="filter-row">
              <span className="muted tiny">Итог</span>
              <div className="chips">
                {OK_FILTERS.map((f) => (
                  <button
                    key={f.key || "any"}
                    className={f.key === okFilter ? "chip active" : "chip"}
                    onClick={() => {
                      setOkFilter(f.key);
                      resetPage();
                    }}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <div className="filter-block">
            <div className="filter-row">
              <span className="muted tiny">Действие</span>
              <select
                value={action}
                onChange={(e) => {
                  setAction(e.target.value);
                  resetPage();
                }}
              >
                <option value="">все действия</option>
                {(facets.data?.items ?? []).map((a) => (
                  <option key={a.action} value={a.action}>
                    {a.action} ({a.count})
                  </option>
                ))}
              </select>
            </div>
            <div className="filter-row">
              <span className="muted tiny">Цель</span>
              <TargetUserPicker
                guildId={guildId}
                placeholder="пользователь, над которым…"
                value={target}
                onChange={(id) => {
                  setTarget(id);
                  resetPage();
                }}
              />
            </div>
          </div>
          <div className="filter-block">
            <div className="filter-row">
              <span className="muted tiny">Период</span>
              <label className="chart-control">
                <span>с</span>
                <input
                  type="date"
                  value={dateFrom}
                  onChange={(e) => {
                    setDateFrom(e.target.value);
                    resetPage();
                  }}
                />
              </label>
              <label className="chart-control">
                <span>по</span>
                <input
                  type="date"
                  value={dateTo}
                  onChange={(e) => {
                    setDateTo(e.target.value);
                    resetPage();
                  }}
                />
              </label>
              <button
                className="chip"
                title="порядок по дате"
                onClick={() => {
                  setSort((s) => (s === "desc" ? "asc" : "desc"));
                  resetPage();
                }}
              >
                {sort === "desc" ? "сначала новые ↓" : "сначала старые ↑"}
              </button>
            </div>
            {hasFilters && (
              <button className="linklike" onClick={resetAll}>
                сбросить все фильтры
              </button>
            )}
          </div>
        </div>
      )}
      {data.items.length === 0 ? (
        <Empty>Изменений по этому фильтру пока нет.</Empty>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Когда</th>
              <th>Откуда</th>
              <th>Кто</th>
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
                <td>
                  <AuditDetails item={item} />
                </td>
                <td>{item.ok ? "ok" : <span style={{ color: "var(--red)" }}>ошибка</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}
