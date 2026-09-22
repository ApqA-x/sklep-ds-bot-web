export type Health = {
  status: string;
  service: string;
  version: string;
  mongo: { ok: boolean; error?: string };
  auth_enabled: boolean;
};

export type Period = "7d" | "30d" | "all";

export type LeaderboardItem = {
  userId: string;
  userName: string;
  totalMs: number;
  appearances: number;
};

export type Leaderboard = {
  guildId: string;
  period: Period;
  limit: number;
  cached: boolean;
  items: LeaderboardItem[];
};

export type ParticipantBrief = {
  userId: string;
  userName: string;
  joinedAt: string;
  durationMs: number;
};

export type ActiveSession = {
  id: string;
  channelId: string;
  startedAt: string;
  participants: ParticipantBrief[];
};

export type SessionSummary = {
  id: string;
  channelId: string;
  startedAt: string;
  endedAt: string;
  endedByUserId: string | null;
  hasSummary: boolean;
};

export type SessionPage = {
  guildId: string;
  status: string;
  page: number;
  size: number;
  total: number;
  items: SessionSummary[];
};

export type ParticipantFull = {
  userId: string;
  userName: string;
  joinedAt: string;
  leftAt: string | null;
  durationMs: number;
  active: boolean;
};

export type SessionDetail = {
  id: string;
  guildId: string;
  channelId: string;
  status: string;
  startedAt: string;
  endedAt: string | null;
  endedByUserId: string | null;
  summaryMessage: string | null;
  summaryGeneratedAt: string | null;
  participants: ParticipantFull[];
};

export type DailyUsage = { date: string; ms: number };

export type NicknameChange = {
  nickname: string | null;
  previousNickname: string | null;
  changedAt: string;
  source: string | null;
};

export type JoinInfo = {
  inviteCode: string | null;
  inviteType: string | null;
  inviterUserId: string | null;
  inviterName: string | null;
  attributionStatus: string | null;
  joinedAt: string | null;
} | null;

export type UserProfile = {
  guildId: string;
  userId: string;
  userName: string;
  period: Period;
  totalMs: number;
  appearances: number;
  daily: DailyUsage[];
  roleIds: string[];
  nicknames: NicknameChange[];
  join: JoinInfo;
};

export type InviteAttribution = {
  userId: string;
  joinedAt: string;
  inviteCode: string | null;
  inviteType: string | null;
  inviterUserId: string | null;
  inviterName: string | null;
  attributionStatus: string | null;
  source: string | null;
};

export type InviteCatalogEntry = {
  code: string;
  channelId: string;
  inviteType: string | null;
  createdByUserId: string | null;
  createdByName: string | null;
  createdAt: string | null;
  deletedAt: string | null;
  lastSeenAt: string | null;
  source: string | null;
};

export type InviterCount = { userId: string; userName: string; count: number };

export type InvitesOverview = {
  guildId: string;
  period: Period;
  generatedAt: string;
  attributions: InviteAttribution[];
  catalog: InviteCatalogEntry[];
  byInviter: InviterCount[];
};

export type GuildSettingsDoc = Record<string, unknown> & { guildId: string };

export type MemberHit = { userId: string; userName: string };

export type GuildAccess = {
  guildId: string;
  name: string;
  canRead: boolean;
  canWrite: boolean;
};

export type Whoami = {
  authenticated: boolean;
  authEnabled: boolean;
  devMode?: boolean;
  loginUrl?: string;
  user?: { userId: string; userName: string };
  access: { guildId: string; canRead: boolean; canWrite: boolean }[];
};
