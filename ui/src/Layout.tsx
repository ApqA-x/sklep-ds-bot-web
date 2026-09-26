import { NavLink, Outlet, useNavigate, useParams, Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "primereact/button";
import { ConfirmDialog } from "primereact/confirmdialog";
import { api } from "./api/client";
import { nameOf, useNames } from "./names";
import { useGuild } from "./guild";
import { clearDrafts } from "./screens/settingsDraft";

export default function Layout() {
  const { guildId } = useParams<{ guildId: string }>();
  const { guildId: selectedGuild } = useGuild();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const whoami = useQuery({ queryKey: ["whoami"], queryFn: api.whoami });
  const me = whoami.data;
  const names = useNames(guildId);

  return (
    <div className="layout">
      <ConfirmDialog />
      <header className="topbar">
        <span className="brand">Estera</span>
        {guildId && (
          <nav className="nav">
            <NavLink to={`/g/${guildId}/leaderboard`}>Лидерборд</NavLink>
            <NavLink to={`/g/${guildId}/active`}>Активные</NavLink>
            <NavLink to={`/g/${guildId}/sessions`}>Сессии</NavLink>
            <NavLink to={`/g/${guildId}/chat`}>Чат</NavLink>
            <NavLink to={`/g/${guildId}/settings`}>Настройки</NavLink>
            <NavLink to={`/g/${guildId}/audit`}>Аудит</NavLink>
          </nav>
        )}
        <span style={{ flex: 1 }} />
        {guildId && (
          <Button
            className="guild-btn"
            type="button"
            title="сменить сервер"
            label={nameOf(names.data, "guild", guildId)}
            onClick={() => navigate("/")}
          />
        )}
        {me?.authenticated && (
          <span className="user-chip">
            {selectedGuild && me.user?.userId ? (
              <Link to={`/g/${selectedGuild}/users/${me.user.userId}`}>{me.user?.userName}</Link>
            ) : (
              me.user?.userName
            )}
            <Button
              className="chip-x"
              type="button"
              label="выход"
              onClick={async () => {
                await api.logout();
                clearDrafts(); // U05: черновики настроек не переживают выход/смену пользователя
                queryClient.clear();
                navigate("/");
              }}
            />
          </span>
        )}
      </header>
      <main className="content">
        {/* T04.6: смена гильдии полностью перемонтирует экран — выбранные каналы,
            файлы, модалки и pending-операции предыдущей гильдии не переносятся */}
        <Outlet key={guildId} />
      </main>
    </div>
  );
}
