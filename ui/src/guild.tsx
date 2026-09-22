import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";

type GuildContextValue = {
  guildId: string | null;
  pickGuild: (id: string) => void;
};

const GuildContext = createContext<GuildContextValue>({ guildId: null, pickGuild: () => {} });

const STORAGE_KEY = "web.guildId";

export function GuildProvider({ children }: { children: ReactNode }) {
  const [guildId, setGuildId] = useState<string | null>(() => localStorage.getItem(STORAGE_KEY));
  const navigate = useNavigate();

  const pickGuild = useCallback(
    (id: string) => {
      const clean = id.trim();
      if (!/^\d{5,25}$/.test(clean)) return;
      localStorage.setItem(STORAGE_KEY, clean);
      setGuildId(clean);
      navigate(`/g/${clean}/leaderboard`);
    },
    [navigate],
  );

  const value = useMemo(() => ({ guildId, pickGuild }), [guildId, pickGuild]);
  return <GuildContext.Provider value={value}>{children}</GuildContext.Provider>;
}

export function useGuild() {
  return useContext(GuildContext);
}
