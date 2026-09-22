import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading } from "../components/ui";
import { fmtDate, fmtDuration } from "../lib/format";
import { DName } from "../names";

export default function Active() {
  const { guildId = "" } = useParams();
  const query = useQuery({
    queryKey: ["active", guildId],
    queryFn: () => api.activeSessions(guildId),
    refetchInterval: 7000,
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const sessions = query.data?.items ?? [];
  if (sessions.length === 0) return <Empty>В голосовых каналах сейчас никого нет.</Empty>;

  return (
    <div className="cards">
      {sessions.map((session) => (
        <div className="card" key={session.id}>
          <div className="card-head">
            <strong>Канал <DName kind="channel" id={session.channelId} /></strong>
            <span className="muted">с {fmtDate(session.startedAt)}</span>
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
                  <td>{fmtDuration(p.durationMs)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}
