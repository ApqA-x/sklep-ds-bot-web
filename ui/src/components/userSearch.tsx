import { useState } from "react";
import { AutoComplete } from "primereact/autocomplete";
import { Button } from "primereact/button";
import { api } from "../api/client";
import { DName } from "../names";

type MemberHit = { userId: string; userName: string };

// PrimeReact AutoComplete: подсказки по имени (от 2 символов) + ручной ввод snowflake
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
  const [suggestions, setSuggestions] = useState<MemberHit[]>([]);

  const complete = async (event: { query: string }) => {
    const q = event.query.trim();
    if (q.length < 2) {
      setSuggestions([]);
      return;
    }
    try {
      const res = await api.members(guildId, q);
      setSuggestions(res.items);
    } catch {
      setSuggestions([]);
    }
  };

  if (value) {
    return (
      <span className="chip" title={value}>
        <DName kind="user" id={value} />
        <Button
          className="chip-x"
          type="button"
          label="×"
          aria-label="сбросить фильтр по пользователю"
          onClick={() => onChange("")}
        />
      </span>
    );
  }

  return (
    <AutoComplete
      value={query}
      suggestions={suggestions}
      completeMethod={complete}
      minLength={2}
      delay={400}
      field="userName"
      dropdown={false}
      placeholder={placeholder}
      inputStyle={{ width: "14rem" }}
      onChange={(e) => {
        const v = e.value as string | MemberHit | null;
        if (typeof v === "string") {
          setQuery(v);
          const trimmed = v.trim();
          if (/^\d{5,25}$/.test(trimmed)) onChange(trimmed);
        } else if (v && typeof v === "object") {
          const hit = v as MemberHit;
          onChange(hit.userId);
          setQuery("");
          setSuggestions([]);
        }
      }}
      onSelect={(e) => {
        const hit = e.value as MemberHit;
        onChange(hit.userId);
        setQuery("");
        setSuggestions([]);
      }}
    />
  );
}
