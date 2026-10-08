import { useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate, useParams, Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "primereact/button";
import { ConfirmDialog } from "primereact/confirmdialog";
import { api } from "./api/client";
import { nameOf, useNames } from "./names";
import { useGuild } from "./guild";
import { clearDrafts } from "./screens/settingsDraft";
import { clearAllIntents } from "./api/intents";

export default function Layout() {
  const { guildId } = useParams<{ guildId: string }>();
  const { guildId: selectedGuild } = useGuild();
  const navigate = useNavigate();
  const location = useLocation();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const menuContainer = useRef<HTMLElement>(null);
  const queryClient = useQueryClient();
  const whoami = useQuery({ queryKey: ["whoami"], queryFn: api.whoami });
  const me = whoami.data;
  const names = useNames(guildId);
  const sections = [
    ["leaderboard", "Лидерборд"],
    ["active", "Активные"],
    ["sessions", "Сессии"],
    ["chat", "Чат"],
    ["settings", "Настройки"],
    ["audit", "Аудит"],
  ] as const;
  const currentSection = sections.find(([segment]) =>
    location.pathname.startsWith(`/g/${guildId}/${segment}`)
  )?.[1] ?? "Профиль";

  useEffect(() => {
    setMenuOpen(false);
  }, [location.pathname]);

  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: KeyboardEvent | PointerEvent) => {
      if (event.type === "keydown") {
        if ((event as KeyboardEvent).key !== "Escape") return;
      } else if (menuContainer.current?.contains(event.target as Node)) {
        return;
      }
      setMenuOpen(false);
      // Pointer default action may move focus after pointerdown has returned.
      if (event.type === "pointerdown") {
        window.setTimeout(() => menuButton.current?.focus(), 0);
      } else {
        menuButton.current?.focus();
      }
    };
    document.addEventListener("keydown", close);
    document.addEventListener("pointerdown", close);
    return () => {
      document.removeEventListener("keydown", close);
      document.removeEventListener("pointerdown", close);
    };
  }, [menuOpen]);

  return (
    <div className="layout">
      <ConfirmDialog />
      <header className="topbar" ref={menuContainer}>
        <span className="brand">Estera</span>
        {guildId && (
          <>
            <button
              ref={menuButton}
              className="mobile-menu-toggle"
              type="button"
              aria-controls="guild-navigation"
              aria-expanded={menuOpen}
              aria-label="Разделы сервера"
              onClick={() => setMenuOpen((open) => !open)}
            >
              Меню
            </button>
            <span className="mobile-current-section" aria-current="page">{currentSection}</span>
            <nav id="guild-navigation" className={`nav${menuOpen ? " nav-open" : ""}`} aria-label="Разделы сервера">
              {sections.map(([segment, label]) => (
                <NavLink key={segment} to={`/g/${guildId}/${segment}`} onClick={() => setMenuOpen(false)}>
                  {label}
                </NavLink>
              ))}
            </nav>
          </>
        )}
        <span className="topbar-spacer" />
        {guildId && (
          <Button
            className="guild-btn"
            type="button"
            title="сменить сервер"
            label={nameOf(names.data, "guild", guildId)}
            onClick={() => { setMenuOpen(false); navigate("/"); }}
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
                setMenuOpen(false);
                await api.logout();
                clearDrafts(); // U05: черновики настроек не переживают выход/смену пользователя
                clearAllIntents(); // R26-03.8: намерения/статусы операций — тоже
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
