import { useEffect, useState } from "react";

type Health = {
  status: string;
  service: string;
  version: string;
  mongo: { ok: boolean; error?: string };
  auth_enabled: boolean;
};

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/healthz")
      .then((r) => r.json() as Promise<Health>)
      .then((data) => {
        if (!cancelled) setHealth(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: "2rem", maxWidth: 720 }}>
      <h1>voice_tracker — web</h1>
      <p>Каркас этапа 0. Дашборд и экраны управления — следующие этапы плана.</p>
      <section>
        <h2>/api/healthz</h2>
        {error && <pre>fetch error: {error}</pre>}
        {health ? (
          <pre>{JSON.stringify(health, null, 2)}</pre>
        ) : (
          !error && <p>загрузка…</p>
        )}
      </section>
    </main>
  );
}
