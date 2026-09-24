import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PrimeReactProvider, addLocale } from "primereact/api";
import "primereact/resources/primereact.min.css";
import "primeicons/primeicons.css";
import "primereact/resources/themes/lara-dark-purple/theme.css";
import "./styles.css";
import Layout from "./Layout";
import { GuildProvider } from "./guild";
import Home from "./screens/Home";
import Leaderboard from "./screens/Leaderboard";
import Active from "./screens/Active";
import Sessions from "./screens/Sessions";
import SessionDetail from "./screens/SessionDetail";
import UserProfile from "./screens/UserProfile";
import Settings from "./screens/Settings";
import Audit from "./screens/Audit";
import Chat from "./screens/Chat";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

const ruLocale = {
  dayNames: ["Воскресенье", "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"],
  dayNamesShort: ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"],
  dayNamesMin: ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"],
  monthNames: [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
  ],
  monthNamesShort: ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"],
  today: "Сегодня",
  clear: "Очистить",
  firstDayOfWeek: 1,
  emptyFilterMessage: "Совпадений нет",
  emptyMessage: "Нет данных",
};

addLocale("ru", ruLocale);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <PrimeReactProvider value={{ ripple: true, locale: "ru" }}>
        <BrowserRouter>
        <GuildProvider>
          <Routes>
            <Route element={<Layout />}>
              <Route path="/" element={<Home />} />
              <Route path="/g/:guildId">
                <Route index element={<Navigate to="leaderboard" replace />} />
                <Route path="leaderboard" element={<Leaderboard />} />
                <Route path="chat-leaderboard" element={<Navigate to="../leaderboard" replace />} />
                <Route path="active" element={<Active />} />
                <Route path="sessions" element={<Sessions />} />
                <Route path="sessions/:sessionId" element={<SessionDetail />} />
                <Route path="users/:userId" element={<UserProfile />} />
                <Route path="invites" element={<Navigate to="../leaderboard" replace />} />
                <Route path="settings" element={<Settings />} />
                <Route path="audit" element={<Audit />} />
                <Route path="chat" element={<Chat />} />
              </Route>
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </GuildProvider>
        </BrowserRouter>
      </PrimeReactProvider>
    </QueryClientProvider>
  </StrictMode>,
);
