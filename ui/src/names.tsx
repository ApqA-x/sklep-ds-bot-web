import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api } from "./api/client";
import type { NamesPayload } from "./api/types";

export function useNames(guildId: string | undefined) {
  return useQuery({
    queryKey: ["names", guildId],
    queryFn: () => api.names(guildId as string),
    enabled: !!guildId && /^\d{5,25}$/.test(guildId),
    staleTime: 300_000,
  });
}

export function usePicker(guildId: string) {
  return useQuery({
    queryKey: ["picker", guildId],
    queryFn: () => api.picker(guildId),
    staleTime: 300_000,
  });
}

// Живое состояние участника (роли/тайм-аут/войс) — только для записи, короткая свежесть.
export function useMemberState(guildId: string, userId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["memberState", guildId, userId],
    queryFn: () => api.memberState(guildId, userId),
    enabled: enabled && /^\d{5,25}$/.test(guildId) && /^\d{5,25}$/.test(userId),
    staleTime: 15_000,
  });
}

export type NameKind = "user" | "channel" | "role" | "guild";

export function nameOf(names: NamesPayload | undefined, kind: NameKind, id: string): string {
  if (!names) return id;
  if (kind === "guild") return names.guildName || id;
  const map = kind === "user" ? names.users : kind === "channel" ? names.channels : names.roles;
  return map[id] || id;
}

export function displayUserName(names: NamesPayload | undefined, id: string, archivedName?: string | null): string {
  for (const value of [names?.users[id], archivedName]) {
    const name = value?.trim();
    if (name && name !== id && name.toLowerCase() !== "unknown" && !/^\d{15,22}$/.test(name)) return name;
  }
  return id;
}

export function DName({ kind, id, fallback }: { kind: NameKind; id: string; fallback?: string | null }) {
  const { guildId = "" } = useParams();
  const names = useNames(guildId);
  return <>{kind === "user" ? displayUserName(names.data, id, fallback) : nameOf(names.data, kind, id)}</>;
}
