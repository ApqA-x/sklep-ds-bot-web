export function fmtClock(ms: number): string {
  const total = Number.isFinite(ms) ? Math.max(0, Math.floor(ms / 1000)) : 0;
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

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

export function fmtBytes(size: number): string {
  if (!Number.isFinite(size) || size <= 0) return "0 Б";
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let value = size;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}
