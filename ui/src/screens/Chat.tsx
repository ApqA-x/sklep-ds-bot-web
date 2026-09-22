import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api } from "../api/client";
import type { ChatMessage } from "../api/types";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtDate } from "../lib/format";
import { nameOf, useNames } from "../names";

export default function Chat() {
  const { guildId = "" } = useParams();
  const names = useNames(guildId);
  const channels = useQuery({
    queryKey: ["chat-channels", guildId],
    queryFn: () => api.chatChannels(guildId),
  });

  const [channelId, setChannelId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [cursor, setCursor] = useState<string | null | undefined>(undefined); // undefined: ещё не грузили
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const available = channels.data?.items ?? [];
  const activeChannel = channelId ?? available[0]?.channelId ?? null;

  const load = useCallback(
    async (channel: string, before?: string) => {
      const seq = ++requestSeq.current;
      setLoading(true);
      setError(null);
      try {
        const page = await api.chatMessages(guildId, channel, before);
        if (seq !== requestSeq.current) return; // устаревший ответ (сменили канал)
        setMessages((prev) => (before ? [...page.items, ...prev] : page.items));
        setCursor(page.hasMore ? page.nextBefore : null);
      } catch (err) {
        if (seq === requestSeq.current) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (seq === requestSeq.current) setLoading(false);
      }
    },
    [guildId],
  );

  useEffect(() => {
    if (!activeChannel) return;
    setMessages([]);
    setCursor(undefined);
    void load(activeChannel);
  }, [activeChannel, load]);

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
          <div className="toolbar">
            {available.map((c) => (
              <button
                key={c.channelId}
                className={c.channelId === activeChannel ? "chip active" : "chip"}
                onClick={() => setChannelId(c.channelId)}
              >
                {nameOf(names.data, "channel", c.channelId)} ({c.count})
              </button>
            ))}
          </div>
          {error && <ErrorBox error={new Error(error)} />}
          {cursor !== undefined && cursor !== null && (
            <div className="toolbar">
              <button disabled={loading} onClick={() => activeChannel && void load(activeChannel, cursor)}>
                {loading ? "загрузка…" : "показать более ранние"}
              </button>
            </div>
          )}
          {loading && messages.length === 0 ? (
            <Loading />
          ) : messages.length === 0 && !error ? (
            <Empty>В этом канале нет сохранённых сообщений.</Empty>
          ) : (
            <div className="chat-list">
              {messages.map((m) => (
                <div className="chat-message" key={m.messageId} data-deleted={m.deletedAt ? "true" : undefined}>
                  <span className="chat-time">{fmtDate(m.sentAt)}</span>
                  <strong className="chat-author" title={m.authorUserId}>
                    {nameOf(names.data, "user", m.authorUserId) || m.authorName || m.authorUserId}
                  </strong>
                  <span className="chat-content">{m.content}</span>
                  {m.editedAt && <span className="chat-flag">изменено</span>}
                  {m.deletedAt && <span className="chat-flag">удалено</span>}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Section>
  );
}
