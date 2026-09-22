import type { ReactNode } from "react";
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
