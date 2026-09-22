import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import type { SessionDetail as SessionDetailType } from "../api/types";
import { Empty, ErrorBox, Loading } from "../components/ui";
import { fmtClock, fmtDate, fmtDuration } from "../lib/format";
import { DName, nameOf, useNames } from "../names";

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
  return now - new Date(iso).getTime();
}

function fmtUtc(d: Date): string {
  return `${d.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

// интервалы: активный тикает в реальном времени, закрытые берём из durationMs
function intervalMs(p: SessionDetailType["participants"][number], now: number): number {
  if (p.active) return Math.max(0, msSince(now, p.joinedAt));
  return p.durationMs || Math.max(0, msSince(now, p.joinedAt));
}

function SessionExpanded({
  guildId,
  sessionId,
  now,
}: {
  guildId: string;
  sessionId: string;
  now: number;
}) {
  const query = useQuery({
    queryKey: ["session", guildId, sessionId],
    queryFn: () => api.sessionDetail(guildId, sessionId),
    refetchInterval: 15000,
  });
  const names = useNames(guildId);
  const [showOnline, setShowOnline] = useState(false);
  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;
  const s = query.data;
  if (!s) return null;

  const grouped = new Map<string, { userName: string; visits: number; totalMs: number; inChannel: boolean }>();
  for (const p of s.participants) {
    const g = grouped.get(p.userId) ?? { userName: p.userName, visits: 0, totalMs: 0, inChannel: false };
    g.visits += 1;
    g.totalMs += intervalMs(p, now);
    g.inChannel = g.inChannel || p.active;
    if (p.userName) g.userName = p.userName;
    grouped.set(p.userId, g);
  }
  const rows = [...grouped.entries()].sort((a, b) => b[1].totalMs - a[1].totalMs);
  const online = rows.filter(([, g]) => g.inChannel);

  return (
    <div className="card-details">
      <div className="session-summary muted tiny">
        Обновлено: {s.updatedAt ? fmtUtc(new Date(s.updatedAt)) : fmtUtc(new Date(query.dataUpdatedAt))} · Всего:{" "}
        {s.participants.length} ·{" "}
        <button
          type="button"
          className={showOnline ? "online-count active" : "online-count"}
          title="показать, кто сейчас в канале"
          onClick={() => setShowOnline((v) => !v)}
        >
          В сети: {online.length}
        </button>
      </div>
      {showOnline && (
        <div className="online-list">
          {online.length === 0 ? (
            <span className="muted tiny">Сейчас никого нет в канале.</span>
          ) : (
            online.map(([userId, g]) => (
              <Link key={userId} to={`/g/${guildId}/users/${userId}`} className="online-user" title={userId}>
                {g.userName || nameOf(names.data, "user", userId)}
                <span className="muted tiny">в канале {fmtClock(g.totalMs)}</span>
              </Link>
            ))
          )}
        </div>
      )}
      <table>
        <thead>
          <tr>
            <th>Участник</th>
            <th>Заходов</th>
            <th>Время за сессию</th>
            <th>Сейчас</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([userId, g]) => (
            <tr key={userId}>
              <td>
                <Link to={`/g/${guildId}/users/${userId}`} title={userId}>
                  {g.userName || nameOf(names.data, "user", userId)}
                </Link>
              </td>
              <td>{g.visits}</td>
              <td>{fmtDuration(g.totalMs)}</td>
              <td>{g.inChannel ? <span className="badge voice">в канале</span> : <span className="muted">вышел</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <details className="intervals">
        <summary>Все заходы ({s.participants.length})</summary>
        <table>
          <thead>
            <tr>
              <th>Участник</th>
              <th>Зашёл</th>
              <th>Вышел</th>
              <th>Длительность</th>
            </tr>
          </thead>
          <tbody>
            {s.participants.map((p) => (
              <tr key={`${p.userId}-${p.joinedAt}`}>
                <td title={p.userId}>{p.userName}</td>
                <td>{fmtDate(p.joinedAt)}</td>
                <td>{p.active ? "в канале" : fmtDate(p.leftAt)}</td>
                <td className={p.active ? "live-timer" : undefined}>
                  {p.active ? fmtClock(intervalMs(p, now)) : fmtDuration(p.durationMs)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

export default function Active() {
  const { guildId = "" } = useParams();
  const now = useNow(1000);
  const [openId, setOpenId] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["active", guildId],
    queryFn: () => api.activeSessions(guildId),
    refetchInterval: 7000,
  });

  const openIds = useMemo(() => {
    const alive = new Set((query.data?.items ?? []).map((s) => s.id));
    return openId && alive.has(openId) ? openId : null;
  }, [query.data, openId]);

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const sessions = query.data?.items ?? [];
  if (sessions.length === 0) return <Empty>В голосовых каналах сейчас никого нет.</Empty>;

  return (
    <div className="cards">
      {sessions.map((session) => (
        <div className={session.id === openIds ? "card open" : "card"} key={session.id}>
          <div className="card-head">
            <strong>
              Канал <DName kind="channel" id={session.channelId} />
            </strong>
            <span className="live-timer">
              <span className="live-dot" aria-hidden="true" />
              {fmtClock(msSince(now, session.startedAt))}
            </span>
          </div>
          <div className="card-sub muted">
            <span>с {fmtDate(session.startedAt)}</span>
            <button
              type="button"
              className="expander"
              onClick={() => setOpenId(session.id === openIds ? null : session.id)}
            >
              {session.id === openIds ? "скрыть подробности ▴" : "подробности ▾"}
            </button>
          </div>
          <table>
            <thead>
              <tr>
                <th>Участник</th>
                <th>Зашёл</th>
                <th>В сессии</th>
              </tr>
            </thead>
            <tbody>
              {session.participants.map((p) => (
                <tr key={p.userId}>
                  <td>
                    <Link to={`/g/${guildId}/users/${p.userId}`}>{p.userName}</Link>
                  </td>
                  <td>{fmtDate(p.joinedAt)}</td>
                  <td className="live-timer">{fmtClock(msSince(now, p.joinedAt))}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {session.id === openIds && <SessionExpanded guildId={guildId} sessionId={session.id} now={now} />}
        </div>
      ))}
    </div>
  );
}
