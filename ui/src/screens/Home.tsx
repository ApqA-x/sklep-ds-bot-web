import { useQuery } from "@tanstack/react-query";
import { Button } from "primereact/button";
import { api } from "../api/client";
import { ErrorBox, Loading } from "../components/ui";
import { useGuild } from "../guild";
import { hasUnsavedDraft } from "./settingsDraft";

export default function Home() {
  const { pickGuild } = useGuild();
  const whoami = useQuery({ queryKey: ["whoami"], queryFn: api.whoami });
  const guilds = useQuery({ queryKey: ["guilds"], queryFn: api.guilds });

  if (whoami.isLoading) return <Loading />;
  if (whoami.isError) return <ErrorBox error={whoami.error} />;

  const me = whoami.data;
  if (!me) return null;

  // T07.5: переход между guild при несохранённом черновике настроек — явное предупреждение
  const choose = (id: string) => {
    // черновик на любой ДРУГОЙ гильдии значит, что пользователь уходит, не сохранив
    if (hasUnsavedDraft(id) && !window.confirm("Есть несохранённые изменения настроек. Перейти на другой сервер и оставить их несохранёнными?")) {
      return;
    }
    pickGuild(id);
  };
  return (
    <div className="home">
      <h1>Estera</h1>
      {me.authEnabled && !me.authenticated && (
        <p>
          <a className="chip" href="/api/auth/login">
            Войти через Discord
          </a>
        </p>
      )}
      {me.devMode && <p className="hint">Режим разработки: авторизация не настроена.</p>}
      {me.authenticated && <p className="muted">Вы вошли как {me.user?.userName}</p>}
      {guilds.data && guilds.data.guilds.length > 0 && (
        <ul className="guild-list">
          {guilds.data.guilds.map((g) => (
            <li key={g.guildId}>
              <Button onClick={() => choose(g.guildId)}>{g.name}</Button>
            </li>
          ))}
        </ul>
      )}
      {me.authenticated && guilds.data && guilds.data.guilds.length === 0 && (
        <p className="hint">Нет серверов с правами Manage Guild, где присутствует бот.</p>
      )}
      <p className="hint">Или введите ID сервера вручную в поле сверху.</p>
    </div>
  );
}
