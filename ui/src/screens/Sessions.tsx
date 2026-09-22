import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtClock, fmtDate } from "../lib/format";
import { DName } from "../names";

function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

function msSince(now: number, iso: string | null | undefined): number {
  if (!iso) return 0;
  return Math.max(0, now - new Date(iso).getTime());
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString("ru-RU", { hour12: false });
}

export default function Sessions() {
  const { guildId = "" } = useParams();
  const [page, setPage] = useState(1);
  const now = useNow(1000);

  const active = useQuery({
    queryKey: ["sessions-active", guildId],
    queryFn: () => api.activeSessions(guildId),
    refetchInterval: 30_000,
  });

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
  const online = active.data?.items ?? [];

  return (
    <>
      <Section title={`Онлайн-сессии (${online.length})`}>
        {active.isError ? (
          <ErrorBox error={active.error} />
        ) : online.length === 0 ? (
          <Empty>Сейчас активных сессий нет.</Empty>
        ) : (
          <div className="online-sessions">
            {online.map((s) => (
              <div className="online-session" key={s.id}>
                <div className="online-session-head">
                  <Link to={`/g/${guildId}/sessions/${s.id}`}>
                    <DName kind="channel" id={s.channelId} />
                  </Link>
                  <span className="muted tiny">с {fmtTime(s.startedAt)}</span>
                  <span className="session-timer">{fmtClock(msSince(now, s.startedAt))}</span>
                  <Link className="linklike" to={`/g/${guildId}/sessions/${s.id}`}>
                    подробности ▾
                  </Link>
                </div>
                <div className="online-list">
                  {s.participants.length === 0 ? (
                    <span className="muted tiny">Сейчас никого нет в канале.</span>
                  ) : (
                    s.participants.map((p, i) => (
                      <div className="online-user" key={`${p.userId}-${i}`}>
                        <Link to={`/g/${guildId}/users/${p.userId}`}>
                          <DName kind="user" id={p.userId} />
                        </Link>
                        <span className="muted tiny">
                          в канале {fmtClock(msSince(now, p.joinedAt))}
                        </span>
                      </div>
                    ))
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Section>
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
                    <Link to={`/g/${guildId}/sessions/${s.id}`}>
                      <DName kind="channel" id={s.channelId} />
                    </Link>
                  </td>
                  <td>{fmtDate(s.startedAt)}</td>
                  <td>{fmtDate(s.endedAt)}</td>
                  <td>{s.endedByUserId ? <DName kind="user" id={s.endedByUserId} /> : "—"}</td>
                  <td>{s.hasSummary ? "есть" : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </>
  );
}
