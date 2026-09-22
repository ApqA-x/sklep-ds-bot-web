import type {
  ActiveSession,
  GuildAccess,
  GuildSettingsDoc,
  Health,
  InvitesOverview,
  Leaderboard,
  MemberHit,
  Period,
  SessionDetail,
  SessionPage,
  UserProfile,
  Whoami,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: "same-origin" });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => apiGet<Health>("/api/healthz"),

  whoami: () => apiGet<Whoami>("/api/auth/whoami"),

  logout: async () => {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
  },

  guilds: () => apiGet<{ guilds: GuildAccess[] }>("/api/guilds"),

  leaderboard: (guildId: string, period: Period, limit = 50) =>
    apiGet<Leaderboard>(`/api/guild/${guildId}/leaderboard?period=${period}&limit=${limit}`),

  activeSessions: (guildId: string) =>
    apiGet<{ guildId: string; items: ActiveSession[] }>(`/api/guild/${guildId}/sessions/active`),

  sessionsHistory: (guildId: string, page: number, size = 25) =>
    apiGet<SessionPage>(`/api/guild/${guildId}/sessions?page=${page}&size=${size}`),

  sessionDetail: (guildId: string, sessionId: string) =>
    apiGet<SessionDetail>(`/api/guild/${guildId}/sessions/${sessionId}`),

  userProfile: (guildId: string, userId: string, period: Period) =>
    apiGet<UserProfile>(`/api/guild/${guildId}/users/${userId}?period=${period}`),

  invites: (guildId: string, period: Period) =>
    apiGet<InvitesOverview>(`/api/guild/${guildId}/invites?period=${period}`),

  settings: (guildId: string) => apiGet<GuildSettingsDoc>(`/api/guild/${guildId}/settings`),

  members: (guildId: string, q: string) =>
    apiGet<{ guildId: string; q: string; items: MemberHit[] }>(
      `/api/guild/${guildId}/members?q=${encodeURIComponent(q)}`,
    ),
};
