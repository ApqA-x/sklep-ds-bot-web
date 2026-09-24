import { useQuery } from "@tanstack/react-query";
import type {
  ActiveSession,
  AuditPage,
  ChatChannel,
  ChatLeaderboard,
  ChatMessagesPage,
  ChatPresetPage,
  DiscordAuditPage,
  EmbedSpec,
  GuildAccess,
  GuildSettingsDoc,
  Health,
  InvitesOverview,
  Leaderboard,
  MemberHit,
  MemberState,
  NamesPayload,
  Period,
  PickerPayload,
  SessionDetail,
  SessionPage,
  StalkerSubscription,
  UserCard,
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

async function apiSend<T>(method: "POST" | "PATCH" | "DELETE", path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "content-type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

async function apiSendForm<T>(method: "POST", path: string, form: FormData): Promise<T> {
  const response = await fetch(path, { method, credentials: "same-origin", body: form });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail);
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

  leaderboard: (guildId: string, period: Period, limit = 50, page = 1, q = "") =>
    apiGet<Leaderboard>(
      `/api/guild/${guildId}/leaderboard?period=${period}&limit=${limit}&page=${page}${q ? `&q=${encodeURIComponent(q)}` : ""}`,
    ),

  chatLeaderboard: (guildId: string, period: Period, limit = 50, page = 1, q = "") =>
    apiGet<ChatLeaderboard>(
      `/api/guild/${guildId}/chat-leaderboard?period=${period}&limit=${limit}&page=${page}${q ? `&q=${encodeURIComponent(q)}` : ""}`,
    ),

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

  patchSettings: (guildId: string, patch: Record<string, unknown>) =>
    apiSend<GuildSettingsDoc>("PATCH", `/api/guild/${guildId}/settings`, patch),

  trusted: (guildId: string, userId: string, action: "add" | "remove") =>
    apiSend<GuildSettingsDoc>("POST", `/api/guild/${guildId}/trusted`, { userId, action }),

  autoUnmute: (guildId: string, userId: string, action: "add" | "remove") =>
    apiSend<GuildSettingsDoc>("POST", `/api/guild/${guildId}/autoUnmute`, { userId, action }),

  stalkerList: (guildId: string) =>
    apiGet<{ guildId: string; items: StalkerSubscription[] }>(`/api/guild/${guildId}/stalker`),

  stalker: (guildId: string, watcherUserId: string, targetUserId: string, action: "add" | "remove") =>
    apiSend<{ ok: boolean; subscriptionId: string }>("POST", `/api/guild/${guildId}/stalker`, {
      watcherUserId,
      targetUserId,
      action,
    }),

  names: (guildId: string) => apiGet<NamesPayload>(`/api/guild/${guildId}/names`),

  picker: (guildId: string) => apiGet<PickerPayload>(`/api/guild/${guildId}/picker`),

  memberState: (guildId: string, userId: string) =>
    apiGet<MemberState>(`/api/guild/${guildId}/users/${userId}/member`),

  userCard: (guildId: string, userId: string) =>
    apiGet<UserCard>(`/api/guild/${guildId}/users/${userId}/card`),

  chatChannels: (guildId: string) =>
    apiGet<{ guildId: string; items: ChatChannel[] }>(`/api/guild/${guildId}/chat/channels`),

  chatPresets: (guildId: string) => apiGet<ChatPresetPage>(`/api/guild/${guildId}/chat-presets`),

  chatPresetAdd: (guildId: string, text: string, name: string | null, channelIds: string[]) =>
    apiSend<{ ok: boolean; presetId: string }>("POST", `/api/guild/${guildId}/chat-presets`, {
      action: "add",
      text,
      name,
      channelIds,
    }),

  chatPresetAddEmbed: (guildId: string, embed: EmbedSpec, name: string | null, channelIds: string[]) =>
    apiSend<{ ok: boolean; presetId: string }>("POST", `/api/guild/${guildId}/chat-presets`, {
      action: "add",
      kind: "embed",
      embed,
      name,
      channelIds,
    }),

  chatPresetRemove: (guildId: string, presetId: string) =>
    apiSend<{ ok: boolean; presetId: string }>("POST", `/api/guild/${guildId}/chat-presets`, {
      action: "remove",
      presetId,
    }),

  chatMessages: (
    guildId: string,
    opts: {
      channelId?: string[];
      before?: string;
      after?: string;
      limit?: number;
      userId?: string;
      type?: string;
      dateFrom?: string;
      dateTo?: string;
      sort?: "asc" | "desc";
    } = {},
  ) => {
    const p = new URLSearchParams();
    for (const id of opts.channelId ?? []) p.append("channelId", id);
    if (opts.before) p.set("before", opts.before);
    if (opts.after) p.set("after", opts.after);
    p.set("limit", String(opts.limit ?? 50));
    for (const key of ["userId", "type", "dateFrom", "dateTo", "sort"] as const) {
      const value = opts[key];
      if (value) p.set(key, value);
    }
    return apiGet<ChatMessagesPage>(`/api/guild/${guildId}/chat?${p.toString()}`);
  },

  audit: (
    guildId: string,
    opts: {
      page?: number;
      size?: number;
      origin?: string;
      userId?: string;
      action?: string;
      ok?: string; // "" | "1" | "0"
      dateFrom?: string;
      dateTo?: string;
      sort?: "asc" | "desc";
    } = {},
  ) => {
    const p = new URLSearchParams();
    p.set("page", String(opts.page ?? 1));
    p.set("size", String(opts.size ?? 50));
    for (const key of ["origin", "userId", "action", "ok", "dateFrom", "dateTo", "sort"] as const) {
      const value = opts[key];
      if (value) p.set(key, value);
    }
    return apiGet<AuditPage>(`/api/guild/${guildId}/audit?${p.toString()}`);
  },

  auditActions: (guildId: string) =>
    apiGet<{ guildId: string; items: { action: string; count: number }[] }>(
      `/api/guild/${guildId}/audit/actions`,
    ),

  auditDiscord: (
    guildId: string,
    opts: {
      page?: number;
      size?: number;
      actionType?: number;
      actor?: string;
      target?: string;
      dateFrom?: string;
      dateTo?: string;
      sort?: "asc" | "desc";
    } = {},
  ) => {
    const p = new URLSearchParams();
    p.set("page", String(opts.page ?? 1));
    p.set("size", String(opts.size ?? 50));
    if (opts.actionType) p.set("actionType", String(opts.actionType));
    for (const key of ["actor", "target", "dateFrom", "dateTo", "sort"] as const) {
      const value = opts[key];
      if (value) p.set(key, value);
    }
    return apiGet<DiscordAuditPage>(`/api/guild/${guildId}/audit/discord?${p.toString()}`);
  },

  auditDiscordActions: (guildId: string) =>
    apiGet<{ guildId: string; items: { actionType: number; action: string; count: number }[] }>(
      `/api/guild/${guildId}/audit/discord/actions`,
    ),

  auditDiscordSync: (guildId: string) =>
    apiSend<{ guildId: string; ok: boolean; discordStatus: number; inserted: number; error?: string }>(
      "POST",
      `/api/guild/${guildId}/audit/discord/sync`,
    ),

  botRole: (guildId: string, userId: string, roleId: string, action: "grant" | "revoke") =>
    apiSend<{ ok: boolean }>("POST", `/api/guild/${guildId}/bot/member/${userId}/roles`, { roleId, action }),

  botTimeout: (guildId: string, userId: string, mute: boolean, seconds = 600) =>
    apiSend<{ ok: boolean }>("POST", `/api/guild/${guildId}/bot/member/${userId}/timeout`, { mute, seconds }),

  botMove: (guildId: string, userId: string, channelId: string) =>
    apiSend<{ ok: boolean }>("POST", `/api/guild/${guildId}/bot/member/${userId}/move`, { channelId }),

  botDisconnect: (guildId: string, userId: string) =>
    apiSend<{ ok: boolean }>("POST", `/api/guild/${guildId}/bot/member/${userId}/disconnect`, {}),

  botKick: (guildId: string, userId: string, reason: string) =>
    apiSend<{ ok: boolean }>("POST", `/api/guild/${guildId}/bot/member/${userId}/kick`, { reason }),

  botMessage: (
    guildId: string,
    channelId: string,
    content: string,
    files: File[] = [],
    embed: EmbedSpec | null = null,
  ) => {
    if (files.length === 0 && embed === null)
      return apiSend<{ ok: boolean }>("POST", `/api/guild/${guildId}/bot/channel/${channelId}/message`, { content });
    const form = new FormData();
    if (content) form.append("content", content);
    if (embed) form.append("embed", JSON.stringify(embed));
    for (const f of files) form.append("files", f, f.name);
    return apiSendForm<{ ok: boolean }>("POST", `/api/guild/${guildId}/bot/channel/${channelId}/message`, form);
  },

  botInviteCreate: (guildId: string, channelId: string) =>
    apiSend<{ ok: boolean }>("POST", `/api/guild/${guildId}/bot/invite`, { channelId }),

  botInviteDelete: (guildId: string, code: string) =>
    apiSend<{ ok: boolean }>("DELETE", `/api/guild/${guildId}/bot/invite/${encodeURIComponent(code)}`),
};

export function useCanWrite(guildId: string | undefined): boolean {
  const whoami = useQuery({ queryKey: ["whoami"], queryFn: api.whoami });
  const me = whoami.data;
  if (!me) return false;
  if (!me.authEnabled) return true; // dev mode
  return me.access.some((a) => a.guildId === guildId && a.canWrite);
}
