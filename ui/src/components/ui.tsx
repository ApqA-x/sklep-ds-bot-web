import { type ReactNode } from "react";
import { Dropdown } from "primereact/dropdown";
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
  const renderItem = (o: PickerOption | null) =>
    o ? (
      <span className="opt-row">
        <span className="opt-dot" style={{ background: roleColorCss(o.color) ?? "var(--overlay1)" }} />
        <span className="opt-label">{o.name}</span>
        {o.note && <span className="opt-note">{o.note}</span>}
      </span>
    ) : (
      <span className="opt-label muted">{placeholder}</span>
    );

  return (
    <Dropdown
      className="opt-select"
      placeholder={placeholder}
      options={options}
      optionValue="id"
      optionLabel="name"
      optionDisabled={(o) => !!o.disabled}
      value={value || null}
      disabled={disabled}
      onChange={(e) => onChange((e.value as string) ?? "")}
      valueTemplate={renderItem}
      itemTemplate={renderItem}
    />
  );
}
