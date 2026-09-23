import { useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "primereact/button";
import { Dropdown } from "primereact/dropdown";
import { SelectButton } from "primereact/selectbutton";
import { api } from "../api/client";
import { DiscordActionDetails } from "../components/auditDiscordText";
import { UserLink } from "../components/userLink";
import { TargetUserPicker } from "../components/userSearch";
import { DateField } from "../components/dateField";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";
import { DName } from "../names";

const DSort = ["desc", "asc"] as const;

// Копия журнала аудита Discord (discord_audit_logs) с фильтрами, пагинацией и
// человекочитаемым отображением. Отличается от «сайта»: источник — сам Discord.
export function AuditDiscord({ guildId, toolbarExtra }: { guildId: string; toolbarExtra?: ReactNode }) {
  const qc = useQueryClient();
  const [page, setPage] = useState(1);
  const [showFilters, setShowFilters] = useState(false);
  const [actionType, setActionType] = useState(0);
  const [actor, setActor] = useState("");
  const [target, setTarget] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [sort, setSort] = useState<(typeof DSort)[number]>("desc");
  const [syncMsg, setSyncMsg] = useState("");
  const [syncing, setSyncing] = useState(false);

  const resetPage = () => setPage(1);

  const query = useQuery({
    queryKey: ["audit-discord", guildId, page, actionType, actor, target, dateFrom, dateTo, sort],
    queryFn: () =>
      api.auditDiscord(guildId, {
        page,
        size: 50,
        actionType: actionType || undefined,
        actor: actor || undefined,
        target: target || undefined,
        dateFrom: dateFrom || undefined,
        dateTo: dateTo || undefined,
        sort,
      }),
    placeholderData: (prev) => prev,
  });

  const facets = useQuery({
    queryKey: ["audit-discord-actions", guildId],
    queryFn: () => api.auditDiscordActions(guildId),
    staleTime: 60_000,
  });

  const refresh = async () => {
    setSyncing(true);
    setSyncMsg("");
    try {
      const res = await api.auditDiscordSync(guildId);
      setSyncMsg(res.error ? `⚠ ${res.error}` : `записей добавлено: ${res.inserted}`);
      qc.invalidateQueries({ queryKey: ["audit-discord", guildId] });
      qc.invalidateQueries({ queryKey: ["audit-discord-actions", guildId] });
    } catch (err) {
      setSyncMsg(`⚠ ${err instanceof Error ? err.message : "ошибка синхронизации"}`);
    } finally {
      setSyncing(false);
    }
  };

  const activeCount =
    (actionType ? 1 : 0) + (actor ? 1 : 0) + (target ? 1 : 0) + (dateFrom ? 1 : 0) + (dateTo ? 1 : 0) + (sort !== "desc" ? 1 : 0);

  const resetAll = () => {
    setActionType(0);
    setActor("");
    setTarget("");
    setDateFrom("");
    setDateTo("");
    setSort("desc");
    resetPage();
  };

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;
  const data = query.data;
  if (!data) return null;
  const pages = Math.max(1, Math.ceil(data.total / data.size));

  return (
    <Section title={`Журнал аудита Discord (${data.total})`}>
      <div className="toolbar">
        {toolbarExtra}
        <Button
          className={showFilters || activeCount ? "chip active" : "chip"}
          onClick={() => setShowFilters((v) => !v)}
        >
          Фильтры
          {activeCount > 0 && <span className="count-badge">{activeCount}</span>}
        </Button>
        <Button className="chip" icon="pi pi-refresh" loading={syncing} onClick={refresh}>
          Обновить из Discord
        </Button>
        {syncMsg && <span className="muted tiny">{syncMsg}</span>}
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
            <h4>Действие</h4>
            <Dropdown
              value={actionType}
              options={[
                { label: "все действия", value: 0 },
                ...(facets.data?.items ?? []).map((a) => ({ label: `${a.action} (${a.count})`, value: a.actionType })),
              ]}
              optionValue="value"
              onChange={(e) => {
                setActionType(e.value as number);
                resetPage();
              }}
            />
          </div>
          <div className="filter-block">
            <h4>Кто сделал</h4>
            <TargetUserPicker
              guildId={guildId}
              placeholder="инициатор"
              value={actor}
              onChange={(id) => {
                setActor(id);
                resetPage();
              }}
            />
          </div>
          <div className="filter-block">
            <h4>Над кем</h4>
            <TargetUserPicker
              guildId={guildId}
              placeholder="цель действия"
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
            <SelectButton
              className="chip-group"
              value={sort}
              options={[
                { label: "новые ↓", value: "desc" },
                { label: "старые ↑", value: "asc" },
              ]}
              optionValue="value"
              onChange={(e) => {
                setSort(e.value as (typeof DSort)[number]);
                resetPage();
              }}
            />
          </div>
          {activeCount > 0 && (
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
        <Empty>
          В локальной копии журнала Discord пока нет записей. Нажмите «Обновить из Discord», чтобы
          подтянуть последние события.
        </Empty>
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
            {data.items.map((entry) => (
              <tr key={entry.id}>
                <td>{fmtDate(entry.at)}</td>
                <td>
                  {entry.actorUserId ? (
                    <UserLink userId={entry.actorUserId} name={entry.actorName || undefined} />
                  ) : (
                    "—"
                  )}
                </td>
                <td title={`Действие ${entry.actionType}`}>
                  <DiscordActionDetails entry={entry} />
                </td>
                <td>
                  {entry.targetUserId ? (
                    <UserLink userId={entry.targetUserId} name={entry.targetUserName || undefined} />
                  ) : entry.targetId ? (
                    <span title={entry.targetId}>
                      <DName kind="role" id={entry.targetId} />
                    </span>
                  ) : (
                    "—"
                  )}
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
