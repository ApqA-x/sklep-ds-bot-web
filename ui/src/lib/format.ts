export function fmtDuration(ms: number | null | undefined): string {
  if (!ms || ms < 60_000) {
    const s = Math.floor((ms || 0) / 1000);
    return `${s}s`;
  }
  const totalMinutes = Math.floor(ms / 60_000);
  const days = Math.floor(totalMinutes / 1440);
  const hours = Math.floor((totalMinutes % 1440) / 60);
  const minutes = totalMinutes % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  return `${minutes}m`;
}

export function toHours(ms: number): number {
  return Math.round((ms / 3_600_000) * 10) / 10;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function fmtDay(iso: string): string {
  return iso.slice(0, 10);
}

export function discordUserUrl(userId: string): string {
  return `https://discord.com/users/${userId}`;
}
