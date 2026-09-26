import { Fragment, useState } from "react";
import type { ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "primereact/button";
import { Dropdown } from "primereact/dropdown";
import { SelectButton } from "primereact/selectbutton";
import { api } from "../api/client";
import type { DiscordAuditSyncStatus } from "../api/types";
import { DiscordAuditFull } from "../components/auditDiscordText";
import { UserLink } from "../components/userLink";
import { TargetUserPicker } from "../components/userSearch";
import { DateField } from "../components/dateField";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";
import { DName } from "../names";

const DSort = ["desc", "asc"] as const;

// T11/H09: честное состояние копии журнала. «Данные загружены» — только при
// backfillComplete (пустая страница от Discord доказала низ истории); при 403 —
// «нет доступа»; при свежих ошибках/задержке — предупреждение; недостижимую
// Discord-историю не обещаем.
function AuditSyncBanner({ s }: { s: DiscordAuditSyncStatus }) {
  const lastOk = s.lastSuccessAt ? `последний успех ${fmtDate(s.lastSuccessAt)}` : "успешных прогонов ещё не было";
  return (
    <div className="audit-sync-banner">
      {s.accessDenied ? (
        <div className="hint danger">
          ⚠ У бота нет доступа к журналу аудита (403) — локальная копия не обновляется.
          Выдайте роли бота разрешение «Просматривать журнал аудита».
        </div>
      ) : (
        <>
          {s.lastError && (
            <div className="hint danger">
              ⚠ Ошибка Discord {s.lastError.status}
              {s.lastError.at ? ` (${fmtDate(s.lastError.at)})` : ""} — повтор не раньше следующего
              тика (~{Math.max(1, Math.round(s.syncIntervalS / 60))} мин), плотных ретраев нет.
            </div>
          )}
          {!s.backfillComplete && (
            <div className="hint">
              ⏳ Загрузка истории продолжается: сохранено {s.storedEntries} записей — архив ещё не
              полон, дозагрузка идёт автоматически с сохранённой точки.
            </div>
          )}
          {s.backfillComplete && s.syncStale && (
            <div className="hint danger">
              ⚠ Синхронизация отстаёт ({lastOk}).
            </div>
          )}
          {s.backfillComplete && !s.syncStale && (
            <div className="hint">✓ Данные загружены ({lastOk}).</div>
          )}
        </>
      )}
      <div className="muted tiny">
        Доступная Discord-история ограничена политикой Discord (~45 дней / до 100 000 записей) —
        более старые события восстановить невозможно ни при каких курсорах.
      </div>
    </div>
  );
}

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
  const [openIds, setOpenIds] = useState<Set<string>>(new Set());

  const toggleRow = (id: string) =>
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

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

  const status = useQuery({
    queryKey: ["audit-discord-status", guildId],
    queryFn: () => api.auditDiscordStatus(guildId),
    refetchInterval: 30_000,
  });

  const refresh = async () => {
    setSyncing(true);
    setSyncMsg("");
    try {
      const res = await api.auditDiscordSync(guildId);
      if (res.error) setSyncMsg(`⚠ ${res.error}`);
      else if (res.skipped) setSyncMsg("синхронизация уже выполняется — повторится на следующем тике");
      else if (!res.ok) setSyncMsg(`⚠ Discord вернул ${res.discordStatus}`);
      else setSyncMsg(`записей добавлено: ${res.inserted}${res.backfillComplete ? "" : " (история дозагружается)"}`);
      qc.invalidateQueries({ queryKey: ["audit-discord", guildId] });
      qc.invalidateQueries({ queryKey: ["audit-discord-actions", guildId] });
      qc.invalidateQueries({ queryKey: ["audit-discord-status", guildId] });
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
      {status.data && <AuditSyncBanner s={status.data} />}
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
              <th>Дата</th>
              <th>Кто</th>
              <th>Действие</th>
              <th>Над кем</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((entry) => {
              const open = openIds.has(entry.id);
              return (
                <Fragment key={entry.id}>
                  <tr className={`audit-row${open ? " open" : ""}`} onClick={() => toggleRow(entry.id)}>
                    <td>{fmtDate(entry.at)}</td>
                    <td>
                      {entry.actorUserId ? (
                        <UserLink userId={entry.actorUserId} name={entry.actorName || undefined} />
                      ) : (
                        "—"
                      )}
                    </td>
                    <td title={`Действие ${entry.actionType}`}>
                      <span className={`caret${open ? " down" : ""}`} aria-hidden="true" />
                      {entry.action}
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
                  </tr>
                  {open && (
                    <tr className="audit-detail">
                      <td colSpan={4}>
                        <DiscordAuditFull entry={entry} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </Section>
  );
}
