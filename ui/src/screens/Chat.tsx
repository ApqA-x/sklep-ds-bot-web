import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import type { ChatAttachment, ChatMessage } from "../api/types";
import { TargetUserPicker } from "../components/userSearch";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtBytes, fmtDate } from "../lib/format";
import { nameOf, useNames } from "../names";

const TYPE_FILTERS: { key: string; label: string }[] = [
  { key: "", label: "всё" },
  { key: "text", label: "текст" },
  { key: "link", label: "ссылки" },
  { key: "file", label: "файлы" },
  { key: "image", label: "фото" },
];

const TYPE_LABELS: Record<string, string> = Object.fromEntries(TYPE_FILTERS.map((t) => [t.key, t.label]));

function attachmentUrl(a: ChatAttachment): string {
  if (a.stored && a.path) return `/media/${a.path}`;
  return a.url; // запасная ссылка Discord (протухает)
}

function Attachment({ a }: { a: ChatAttachment }) {
  const href = attachmentUrl(a);
  if (!href) return null;
  if (a.kind === "image") {
    return (
      <a href={href} target="_blank" rel="noreferrer" className="chat-attachment">
        <img src={href} alt={a.filename} loading="lazy" className="chat-image" />
      </a>
    );
  }
  if (a.kind === "video") {
    return (
      <video src={href} controls className="chat-video">
        <track kind="captions" />
      </video>
    );
  }
  return (
    <a href={href} target="_blank" rel="noreferrer" className="chat-attachment" download={a.filename || undefined}>
      📎 {a.filename} <span className="muted tiny">({fmtBytes(a.size)})</span>
    </a>
  );
}

type Filters = {
  channelId: string; // "" = все каналы
  userId: string;
  type: string;
  dateFrom: string;
  dateTo: string;
  sort: "asc" | "desc";
};

const EMPTY_FILTERS: Filters = { channelId: "", userId: "", type: "", dateFrom: "", dateTo: "", sort: "desc" };

export default function Chat() {
  const { guildId = "" } = useParams();
  const names = useNames(guildId);
  const channels = useQuery({
    queryKey: ["chat-channels", guildId],
    queryFn: () => api.chatChannels(guildId),
  });

  const [tab, setTab] = useState<"feed" | "filters">("feed");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [cursor, setCursor] = useState<string | null | undefined>(undefined); // undefined: ещё не грузили
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }));

  const available = channels.data?.items ?? [];

  // фильтры со вкладки «Фильтры» (канал и порядок дат живут в ленте)
  const panelFiltersCount = [filters.userId, filters.type, filters.dateFrom, filters.dateTo].filter(Boolean).length;
  const panelSummary = [
    filters.userId && `автор: ${nameOf(names.data, "user", filters.userId) || filters.userId}`,
    filters.type && `тип: ${TYPE_LABELS[filters.type]}`,
    (filters.dateFrom || filters.dateTo) && `период: ${filters.dateFrom || "…"} — ${filters.dateTo || "…"}`,
  ]
    .filter(Boolean)
    .join(" · ");
  const hasFilters = panelFiltersCount > 0 || Boolean(filters.channelId);

  const fetchPage = useCallback(
    async (extra: { before?: string; after?: string }) => {
      const seq = ++requestSeq.current;
      setLoading(true);
      setError(null);
      try {
        const page = await api.chatMessages(guildId, {
          channelId: filters.channelId || undefined,
          userId: filters.userId || undefined,
          type: filters.type || undefined,
          dateFrom: filters.dateFrom || undefined,
          dateTo: filters.dateTo || undefined,
          sort: filters.sort,
          before: extra.before,
          after: extra.after,
        });
        if (seq !== requestSeq.current) return; // устаревший ответ (сменили фильтры)
        setMessages((prev) => {
          if (!extra.before && !extra.after) return page.items;
          return filters.sort === "desc" ? [...page.items, ...prev] : [...prev, ...page.items];
        });
        setCursor(page.hasMore ? (filters.sort === "desc" ? page.nextBefore : page.nextAfter) : null);
      } catch (err) {
        if (seq === requestSeq.current) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (seq === requestSeq.current) setLoading(false);
      }
    },
    [guildId, filters],
  );

  // первая страница при смене любых фильтров
  useEffect(() => {
    setMessages([]);
    setCursor(undefined);
    void fetchPage({});
  }, [fetchPage]);

  if (channels.isLoading) return <Loading />;
  if (channels.isError) return <ErrorBox error={channels.error} />;

  return (
    <Section title="История чата">
      {available.length === 0 ? (
        <Empty>
          История сообщений ещё не записывается в базу (коллекция chat_messages пуста). Как только бот начнёт её
          сохранять, переписка появится здесь.
        </Empty>
      ) : (
        <>
          <div className="tabs">
            <button className={tab === "feed" ? "tab active" : "tab"} onClick={() => setTab("feed")}>
              Сообщения
            </button>
            <button className={tab === "filters" ? "tab active" : "tab"} onClick={() => setTab("filters")}>
              Фильтры
              {panelFiltersCount > 0 && <span className="count-badge">{panelFiltersCount}</span>}
            </button>
          </div>

          {tab === "filters" ? (
            <div className="filters-panel">
              <div className="filter-block">
                <h4>Автор сообщений</h4>
                <TargetUserPicker
                  guildId={guildId}
                  placeholder="имя автора (от 2 символов) или id"
                  value={filters.userId}
                  onChange={(id) => set({ userId: id })}
                />
              </div>
              <div className="filter-block">
                <h4>Тип сообщения</h4>
                <div className="chips">
                  {TYPE_FILTERS.map((t) => (
                    <button
                      key={t.key || "all"}
                      className={t.key === filters.type ? "chip active" : "chip"}
                      onClick={() => set({ type: t.key })}
                    >
                      {t.label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="filter-block">
                <h4>Период (границы включительно)</h4>
                <div className="filter-row">
                  <label className="chart-control">
                    <span>с даты</span>
                    <input type="date" value={filters.dateFrom} onChange={(e) => set({ dateFrom: e.target.value })} />
                  </label>
                  <label className="chart-control">
                    <span>по дату</span>
                    <input type="date" value={filters.dateTo} onChange={(e) => set({ dateTo: e.target.value })} />
                  </label>
                </div>
              </div>
              <div className="filter-row">
                {panelFiltersCount > 0 && (
                  <button
                    className="linklike"
                    onClick={() => set({ userId: "", type: "", dateFrom: "", dateTo: "" })}
                  >
                    сбросить фильтры
                  </button>
                )}
                <button className="linklike" onClick={() => setTab("feed")}>
                  к сообщениям →
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="toolbar">
                <button
                  className={filters.channelId === "" ? "chip active" : "chip"}
                  onClick={() => set({ channelId: "" })}
                >
                  все каналы
                </button>
                {available.map((c) => (
                  <button
                    key={c.channelId}
                    className={c.channelId === filters.channelId ? "chip active" : "chip"}
                    onClick={() => set({ channelId: c.channelId })}
                  >
                    {nameOf(names.data, "channel", c.channelId)} ({c.count})
                  </button>
                ))}
                <button
                  className="sort-toggle"
                  title="порядок по дате"
                  onClick={() => set({ sort: filters.sort === "desc" ? "asc" : "desc" })}
                >
                  <span className="sort-arrow">{filters.sort === "desc" ? "↓" : "↑"}</span>
                  {filters.sort === "desc" ? "сначала новые" : "сначала старые"}
                </button>
              </div>
              {hasFilters && (
                <div className="toolbar">
                  <button className="chip active" title="изменить на вкладке «Фильтры»" onClick={() => setTab("filters")}>
                    🔎 {panelSummary || `канал: ${nameOf(names.data, "channel", filters.channelId)}`}
                  </button>
                  <button className="linklike" onClick={() => setFilters(EMPTY_FILTERS)}>
                    сбросить всё
                  </button>
                </div>
              )}
              {error && <ErrorBox error={new Error(error)} />}
              {cursor !== undefined && cursor !== null && (
                <div className="toolbar">
                  <button
                    disabled={loading}
                    onClick={() =>
                      void fetchPage(
                        filters.sort === "desc" ? { before: cursor ?? undefined } : { after: cursor ?? undefined },
                      )
                    }
                  >
                    {loading ? "загрузка…" : filters.sort === "desc" ? "показать более ранние" : "показать более новые"}
                  </button>
                </div>
              )}
              {loading && messages.length === 0 ? (
                <Loading />
              ) : messages.length === 0 && !error ? (
                <>
                  <Empty>Нет сообщений под выбранные фильтры.</Empty>
                  {panelFiltersCount > 0 && (
                    <p className="muted tiny">
                      <button className="linklike" onClick={() => setTab("filters")}>
                        ослабить фильтры
                      </button>
                    </p>
                  )}
                </>
              ) : (
                <div className="chat-list">
                  {messages.map((m) => (
                    <div className="chat-message" key={m.messageId} data-deleted={m.deletedAt ? "true" : undefined}>
                      <span className="chat-time">{fmtDate(m.sentAt)}</span>
                      <strong className="chat-author" title={m.authorUserId}>
                        {nameOf(names.data, "user", m.authorUserId) || m.authorName || m.authorUserId}
                      </strong>
                      <span className="chat-main">
                        {!filters.channelId && m.channelId && (
                          <span className="muted tiny">#{nameOf(names.data, "channel", m.channelId)} </span>
                        )}
                        <span className="chat-content">{m.content}</span>
                        {m.attachments?.length > 0 && (
                          <span className="chat-attachments">
                            {m.attachments.map((a) => (
                              <Attachment key={a.id || a.filename} a={a} />
                            ))}
                          </span>
                        )}
                      </span>
                      <span className="chat-flags">
                        {m.editedAt && <span className="chat-flag">изменено</span>}
                        {m.deletedAt && <span className="chat-flag">удалено</span>}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </>
      )}
    </Section>
  );
}
