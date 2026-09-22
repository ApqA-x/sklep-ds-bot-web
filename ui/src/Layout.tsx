import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet, useNavigate, useParams } from "react-router-dom";
import { Button } from "primereact/button";
import { ConfirmDialog } from "primereact/confirmdialog";
import { InputText } from "primereact/inputtext";
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
      <ConfirmDialog />
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
            <Button
              className="chip-x"
              type="button"
              label="выход"
              onClick={async () => {
                await api.logout();
                navigate("/");
              }}
            />
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
          <InputText
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="guild id"
            aria-label="guild id"
          />
          <Button type="submit" icon="pi pi-arrow-right" text aria-label="выбрать сервер" />
          {guildId && (
            <Button
              type="button"
              title="сменить сервер"
              label={nameOf(names.data, "guild", guildId)}
              onClick={() => navigate("/")}
            />
          )}
        </form>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
