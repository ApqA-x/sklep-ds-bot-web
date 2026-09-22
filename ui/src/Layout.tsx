import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useNavigate, useParams } from "react-router-dom";
import { api } from "./api/client";
import { nameOf, useNames } from "./names";
import { useGuild } from "./guild";

export default function Layout() {
  const { guildId } = useParams<{ guildId: string }>();
  const { pickGuild } = useGuild();
  const navigate = useNavigate();
  const [input, setInput] = useState("");
  const whoami = useQuery({ queryKey: ["whoami"], queryFn: api.whoami });
  const me = whoami.data;
  const names = useNames(guildId);

  return (
    <div className="layout">
      <header className="topbar">
        <span className="brand">Estera</span>
        {guildId && (
          <nav className="nav">
            <NavLink to={`/g/${guildId}/leaderboard`}>Лидерборд</NavLink>
            <NavLink to={`/g/${guildId}/active`}>Активные</NavLink>
            <NavLink to={`/g/${guildId}/sessions`}>Сессии</NavLink>
            <NavLink to={`/g/${guildId}/invites`}>Инвайты</NavLink>
            <NavLink to={`/g/${guildId}/chat`}>Чат</NavLink>
            <NavLink to={`/g/${guildId}/settings`}>Настройки</NavLink>
            <NavLink to={`/g/${guildId}/audit`}>Аудит</NavLink>
          </nav>
        )}
        {me?.authenticated && (
          <span className="user-chip">
            {me.user?.userName}
            <button
              type="button"
              onClick={async () => {
                await api.logout();
                navigate("/");
              }}
            >
              выход
            </button>
          </span>
        )}
        <form
          className="guild-switch"
          onSubmit={(e) => {
            e.preventDefault();
            pickGuild(input);
            setInput("");
          }}
        >
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="guild id"
            aria-label="guild id"
          />
          <button type="submit">→</button>
          {guildId && (
            <button
              type="button"
              title="сменить сервер"
              onClick={() => navigate("/")}
            >
              {nameOf(names.data, "guild", guildId)}
            </button>
          )}
        </form>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
