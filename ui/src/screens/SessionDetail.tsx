import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate, fmtDuration } from "../lib/format";

export default function SessionDetail() {
  const { guildId = "", sessionId = "" } = useParams();
  const query = useQuery({
    queryKey: ["session", guildId, sessionId],
    queryFn: () => api.sessionDetail(guildId, sessionId),
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const s = query.data;
  if (!s) return null;
  return (
    <>
      <Section title={`Сессия на канале ${s.channelId}`}>
        <dl className="kv">
          <dt>Статус</dt>
          <dd>{s.status}</dd>
          <dt>Начало</dt>
          <dd>{fmtDate(s.startedAt)}</dd>
          <dt>Конец</dt>
          <dd>{fmtDate(s.endedAt)}</dd>
        </dl>
      </Section>
      <Section title={`Участники (${s.participants.length})`}>
        <table>
          <thead>
            <tr>
              <th>Пользователь</th>
              <th>Зашёл</th>
              <th>Вышел</th>
              <th>Время</th>
            </tr>
          </thead>
          <tbody>
            {s.participants.map((p) => (
              <tr key={`${p.userId}-${p.joinedAt}`}>
                <td>
                  <Link to={`/g/${guildId}/users/${p.userId}`}>{p.userName}</Link>
                </td>
                <td>{fmtDate(p.joinedAt)}</td>
                <td>{p.active ? "в канале" : fmtDate(p.leftAt)}</td>
                <td>{fmtDuration(p.durationMs)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      {s.summaryMessage && (
        <Section title="Саммари">
          <pre className="summary">{s.summaryMessage}</pre>
        </Section>
      )}
    </>
  );
}
