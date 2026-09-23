import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { Button } from "primereact/button";
import { Dropdown } from "primereact/dropdown";
import { SelectButton } from "primereact/selectbutton";
import { api } from "../api/client";
import { AuditDetails } from "../components/auditText";
import { TargetUserPicker } from "../components/userSearch";
import { DateField } from "../components/dateField";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";
import { DName } from "../names";

type OriginFilter = "" | "web" | "discord";
type AuditSource = "site" | "discord";

const SOURCES: { key: AuditSource; label: string }[] = [
  { key: "site", label: "Журнал сайта" },
  { key: "discord", label: "Журнал Discord" },
];

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
  const [source, setSource] = useState<AuditSource>("site");
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
    enabled: source === "site",
  });

  const facets = useQuery({
    queryKey: ["audit-actions", guildId],
    queryFn: () => api.auditActions(guildId),
    staleTime: 300_000,
    enabled: source === "site",
  });

  const discord = useQuery({
    queryKey: ["audit-discord", guildId],
    queryFn: () => api.auditDiscord(guildId, 100),
    enabled: source === "discord",
    staleTime: 30_000,
  });

  const sourceToggle = (
    <SelectButton
      className="chip-group"
      value={source}
      options={SOURCES.map((s) => ({ label: s.label, value: s.key }))}
      optionValue="value"
      onChange={(e) => setSource(e.value as AuditSource)}
    />
  );

  if (source === "discord") {
    return (
      <Section title="Журнал аудита Discord">
        <div className="toolbar">
          {sourceToggle}
          <span className="muted tiny">последние ≤100 записей журнала сервера Discord</span>
          <span style={{ flex: 1 }} />
          <Button icon="pi pi-refresh" loading={discord.isFetching} onClick={() => discord.refetch()} />
        </div>
        {discord.isLoading ? (
          <Loading />
        ) : discord.isError ? (
          <ErrorBox error={discord.error} />
        ) : (discord.data?.items.length ?? 0) === 0 ? (
          <Empty>В журнале Discord по этому серверу пока нет записей.</Empty>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Когда</th>
                <th>Кто</th>
                <th>Что</th>
                <th>Над кем</th>
                <th>Где</th>
                <th>Причина</th>
              </tr>
            </thead>
            <tbody>
              {discord.data?.items.map((entry) => (
                <tr key={entry.id}>
                  <td>{fmtDate(entry.at)}</td>
                  <td title={entry.actorUserId || ""}>
                    {entry.actorName ||
                      (entry.actorUserId ? <DName kind="user" id={entry.actorUserId} /> : "—")}
                  </td>
                  <td title={`Действие ${entry.actionType}`}>{entry.action}</td>
                  <td title={entry.targetUserId || entry.targetId || ""}>
                    {entry.targetUserName ||
                      (entry.targetUserId ? (
                        <DName kind="user" id={entry.targetUserId} />
                      ) : entry.targetId ? (
                        `ID ${entry.targetId}`
                      ) : (
                        "—"
                      ))}
                  </td>
                  <td>{entry.channelId ? <DName kind="channel" id={entry.channelId} /> : "—"}</td>
                  <td>{entry.reason || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    );
  }

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
        {sourceToggle}
        <Button
          className={showFilters || hasFilters ? "chip active" : "chip"}
          onClick={() => setShowFilters((v) => !v)}
        >
          Фильтры
          {activeCount > 0 && <span className="count-badge">{activeCount}</span>}
        </Button>
        <span className="muted tiny">
          {hasFilters ? `активных фильтров: ${activeCount}` : "фильтры не заданы"}
        </span>
        <span style={{ flex: 1 }} />
        <Button icon="pi pi-arrow-left" disabled={page <= 1} onClick={() => setPage((p) => p - 1)} />
        <span>
          {page} / {pages}
        </span>
        <Button icon="pi pi-arrow-right" disabled={page >= pages} onClick={() => setPage((p) => p + 1)} />
      </div>
      {showFilters && (
        <div className="filters-panel">
          <div className="filter-block">
            <h4>Источник</h4>
            <SelectButton
              className="chip-group"
              value={origin}
              options={FILTERS.map((f) => ({ label: f.label, value: f.key }))}
              optionValue="value"
              onChange={(e) => {
                setOrigin(e.value as OriginFilter);
                resetPage();
              }}
            />
          </div>
          <div className="filter-block">
            <h4>Итог</h4>
            <SelectButton
              className="chip-group"
              value={okFilter}
              options={OK_FILTERS.map((f) => ({ label: f.label, value: f.key }))}
              optionValue="value"
              onChange={(e) => {
                setOkFilter(e.value as "" | "1" | "0");
                resetPage();
              }}
            />
          </div>
          <div className="filter-block">
            <h4>Действие</h4>
            <Dropdown
              value={action}
              options={[
                { label: "все действия", value: "" },
                ...(facets.data?.items ?? []).map((a) => ({ label: `${a.action} (${a.count})`, value: a.action })),
              ]}
              optionValue="value"
              onChange={(e) => {
                setAction(e.value as string);
                resetPage();
              }}
            />
          </div>
          <div className="filter-block">
            <h4>Цель</h4>
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
          <div className="filter-block">
            <h4>Период</h4>
            <div className="filter-row">
              <span className="chart-control">
                <span>с</span>
                <DateField
                  value={dateFrom}
                  placeholder="дд.мм.гггг"
                  onChange={(v) => {
                    setDateFrom(v);
                    resetPage();
                  }}
                />
              </span>
              <span className="chart-control">
                <span>по</span>
                <DateField
                  value={dateTo}
                  placeholder="дд.мм.гггг"
                  onChange={(v) => {
                    setDateTo(v);
                    resetPage();
                  }}
                />
              </span>
            </div>
          </div>
          <div className="filter-block">
            <h4>Порядок</h4>
            <Button
              className="chip"
              onClick={() => {
                setSort((s) => (s === "desc" ? "asc" : "desc"));
                resetPage();
              }}
            >
              {sort === "desc" ? "сначала новые ↓" : "сначала старые ↑"}
            </Button>
          </div>
          {hasFilters && (
            <div className="filter-actions">
              <span className="muted tiny">активных фильтров: {activeCount}</span>
              <Button className="linklike" onClick={resetAll}>
                сбросить все фильтры
              </Button>
            </div>
          )}
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
