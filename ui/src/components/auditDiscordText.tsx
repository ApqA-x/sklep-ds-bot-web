import type { ReactNode } from "react";
import type { DiscordAuditEntry } from "../api/types";
import { DName } from "../names";

function RoleChip({ id, name }: { id: string; name?: string }) {
  return <span className="chip tiny-chip">{name || <DName kind="role" id={id} />}</span>;
}

function roleList(value: unknown): ReactNode {
  if (!Array.isArray(value)) return null;
  const roles = value as { id?: string; name?: string }[];
  if (roles.length === 0) return null;
  return (
    <>
      {roles.map((r, i) => (
        <RoleChip key={r.id ?? i} id={String(r.id ?? "")} name={r.name} />
      ))}
    </>
  );
}

function fmtValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") {
    const obj = value as Record<string, unknown>;
    if (typeof obj.name === "string") return obj.name;
    if (Array.isArray(value)) return `${(value as unknown[]).length} шт`;
    return JSON.stringify(value);
  }
  return String(value);
}

// «Что» произошло в записи журнала Discord: метка действия + читаемые детали
export function DiscordActionDetails({ entry }: { entry: DiscordAuditEntry }) {
  const lines: ReactNode[] = [];
  const added = entry.changes.find((c) => c.key === "$+");
  const removed = entry.changes.find((c) => c.key === "$-");
  if (added?.new || removed?.new) {
    const parts: ReactNode[] = [];
    if (added?.new) parts.push(<span key="a">выдано: {roleList(added.new)}</span>);
    if (removed?.new) parts.push(<span key="r">снято: {roleList(removed.new)}</span>);
    lines.push(
      <span key="roles" className="diff-line">
        {parts.map((p, i) => (
          <span key={i}>
            {i > 0 && " · "}
            {p}
          </span>
        ))}
      </span>,
    );
  }

  const propChanges = entry.changes.filter((c) => c.key !== "$+" && c.key !== "$-");
  if (propChanges.length > 0) {
    const shown = propChanges.slice(0, 3);
    lines.push(
      <span key="props" className="diff-line muted tiny">
        {shown.map((c, i) => (
          <span key={i}>
            {i > 0 && "; "}
            {c.key}: {fmtValue(c.old)} → {fmtValue(c.new)}
          </span>
        ))}
        {propChanges.length > shown.length && `; +${propChanges.length - shown.length} ещё`}
      </span>,
    );
  }

  if (entry.deleteMessageDays && String(entry.deleteMessageDays) !== "0") {
    lines.push(
      <span key="dmd" className="diff-line muted tiny">
        удалено сообщений за {entry.deleteMessageDays} дн.
      </span>,
    );
  }
  if (entry.options.code) {
    lines.push(
      <span key="code" className="diff-line muted tiny">
        инвайт: {entry.options.code}
      </span>,
    );
  }

  return (
    <div className="discord-details">
      <div>{entry.action}</div>
      {lines}
    </div>
  );
}
