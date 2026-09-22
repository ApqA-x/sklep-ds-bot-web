import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import { ErrorBox, Loading, Section } from "../components/ui";

const HIDDEN_KEYS = new Set(["_id"]);

export default function Settings() {
  const { guildId = "" } = useParams();
  const query = useQuery({
    queryKey: ["settings", guildId],
    queryFn: () => api.settings(guildId),
  });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;

  const doc = query.data;
  if (!doc) return null;
  const entries = Object.entries(doc).filter(([key]) => !HIDDEN_KEYS.has(key));

  return (
    <Section title="Настройки гильдии">
      <table>
        <tbody>
          {entries.map(([key, value]) => (
            <tr key={key}>
              <td className="muted">{key}</td>
              <td>
                <pre className="inline">{JSON.stringify(value)}</pre>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="hint">Редактирование появится на этапе 4 (write-слой).</p>
    </Section>
  );
}
