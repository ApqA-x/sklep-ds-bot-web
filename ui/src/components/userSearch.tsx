import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { DName } from "../names";

// поиск пользователя по имени с автоподсказкой (ручной ввод snowflake тоже принимается)
export function TargetUserPicker({
  guildId,
  placeholder,
  value,
  onChange,
}: {
  guildId: string;
  placeholder: string;
  value: string;
  onChange: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const id = setTimeout(() => setDebounced(query.trim()), 400);
    return () => clearTimeout(id);
  }, [query]);

  useEffect(() => {
    const onDocClick = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  const hits = useQuery({
    queryKey: ["member-search", guildId, debounced],
    queryFn: () => api.members(guildId, debounced),
    enabled: debounced.length >= 2,
  });

  if (value) {
    return (
      <span className="chip" title={value}>
        <DName kind="user" id={value} />
        <button type="button" onClick={() => onChange("")} aria-label="сбросить фильтр по пользователю">
          ×
        </button>
      </span>
    );
  }

  return (
    <div className="opt-select" ref={root} style={{ position: "relative" }}>
      <input
        placeholder={placeholder}
        value={query}
        style={{ width: "14rem" }}
        onChange={(e) => {
          const v = e.target.value;
          setQuery(v);
          setOpen(true);
          if (/^\d{5,25}$/.test(v.trim())) onChange(v.trim());
        }}
      />
      {open && debounced.length >= 2 && (hits.data?.items.length ?? 0) > 0 && (
        <div
          className="opt-list"
          role="listbox"
          style={{
            position: "absolute",
            zIndex: 10,
            background: "var(--panel)",
            border: "1px solid var(--border)",
            borderRadius: 8,
            width: "100%",
          }}
        >
          {hits.data!.items.map((m) => (
            <button
              key={m.userId}
              type="button"
              role="option"
              className="opt-item"
              onClick={() => {
                onChange(m.userId);
                setQuery("");
                setOpen(false);
              }}
            >
              {m.userName}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
