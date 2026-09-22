import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError } from "../api/client";

export function Loading() {
  return <div className="state">загрузка…</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  const message =
    error instanceof ApiError
      ? `${error.status}: ${error.message}`
      : error instanceof Error
        ? error.message
        : String(error);
  return <div className="state error">{message}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="state empty">{children}</div>;
}

export function Section({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <section className="section">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

export type PickerOption = {
  id: string;
  name: string;
  color?: number;
  note?: string;
  disabled?: boolean;
};

export function roleColorCss(color: number | undefined): string | undefined {
  if (!color) return undefined;
  return `#${color.toString(16).padStart(6, "0")}`;
}

export function OptionSelect({
  placeholder,
  options,
  value,
  onChange,
  disabled,
}: {
  placeholder: string;
  options: PickerOption[];
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const selected = options.find((o) => o.id === value);

  return (
    <div className="opt-select" ref={root}>
      <button
        type="button"
        className="opt-trigger"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="opt-dot" style={{ background: roleColorCss(selected?.color) ?? "var(--overlay1)" }} />
        <span className="opt-label">{selected ? selected.name : placeholder}</span>
        <span className="opt-caret">▾</span>
      </button>
      {open && (
        <div className="opt-list" role="listbox">
          {options.map((o) => (
            <button
              key={o.id}
              type="button"
              role="option"
              aria-selected={o.id === value}
              disabled={o.disabled}
              className={o.id === value ? "opt-item active" : "opt-item"}
              onClick={() => {
                onChange(o.id);
                setOpen(false);
              }}
            >
              <span className="opt-dot" style={{ background: roleColorCss(o.color) ?? "var(--overlay1)" }} />
              <span className="opt-label">{o.name}</span>
              {o.note && <span className="opt-note">{o.note}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
