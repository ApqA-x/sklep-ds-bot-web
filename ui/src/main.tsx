import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "./styles.css";
import Layout from "./Layout";
import { GuildProvider } from "./guild";
import Home from "./screens/Home";
import Leaderboard from "./screens/Leaderboard";
import Active from "./screens/Active";
import Sessions from "./screens/Sessions";
import SessionDetail from "./screens/SessionDetail";
import UserProfile from "./screens/UserProfile";
import Invites from "./screens/Invites";
import Settings from "./screens/Settings";
import Audit from "./screens/Audit";
import Chat from "./screens/Chat";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <GuildProvider>
          <Routes>
            <Route element={<Layout />}>
              <Route path="/" element={<Home />} />
              <Route path="/g/:guildId">
                <Route index element={<Navigate to="leaderboard" replace />} />
                <Route path="leaderboard" element={<Leaderboard />} />
                <Route path="active" element={<Active />} />
                <Route path="sessions" element={<Sessions />} />
                <Route path="sessions/:sessionId" element={<SessionDetail />} />
                <Route path="users/:userId" element={<UserProfile />} />
                <Route path="invites" element={<Invites />} />
                <Route path="settings" element={<Settings />} />
                <Route path="audit" element={<Audit />} />
                <Route path="chat" element={<Chat />} />
              </Route>
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        </GuildProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
