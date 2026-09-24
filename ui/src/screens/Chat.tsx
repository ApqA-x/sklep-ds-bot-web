import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { Button } from "primereact/button";
import { confirmDialog } from "primereact/confirmdialog";
import { MultiSelect } from "primereact/multiselect";
import { SelectButton } from "primereact/selectbutton";
import { InputTextarea } from "primereact/inputtextarea";
import { api, useCanWrite } from "../api/client";
import type { ChatAttachment, ChatMessage, ChatPreset } from "../api/types";
import { TargetUserPicker } from "../components/userSearch";
import { DateField } from "../components/dateField";
import { Empty, ErrorBox, Loading, Section } from "../components/ui";
import { fmtBytes, fmtDate } from "../lib/format";
import { nameOf, useNames, usePicker } from "../names";

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
  channelIds: string[]; // [] = все каналы
  userId: string;
  type: string;
  dateFrom: string;
  dateTo: string;
  sort: "asc" | "desc";
};

const EMPTY_FILTERS: Filters = { channelIds: [], userId: "", type: "", dateFrom: "", dateTo: "", sort: "desc" };

// лимит Discord на длину текстового сообщения
const MESSAGE_MAX_LEN = 2000;
// лимиты вложений — как на бэкенде (api/bot.py)
const ATTACH_MAX_FILES = 10;
const ATTACH_MAX_FILE_BYTES = 25 * 1024 * 1024;
const ATTACH_MAX_TOTAL_BYTES = 50 * 1024 * 1024;
// бэкендный бакет на гилдию: 5 burst, 2/с — держим паузу между каналами
const SEND_PAUSE_MS = 550;

type SendResult = { channelId: string; ok: boolean; error?: string };

function validateAttachments(next: File[]): string | null {
  if (next.length > ATTACH_MAX_FILES) return `не больше ${ATTACH_MAX_FILES} файлов`;
  let total = 0;
  for (const f of next) {
    if (f.size > ATTACH_MAX_FILE_BYTES) return `файл «${f.name}» больше ${fmtBytes(ATTACH_MAX_FILE_BYTES)}`;
    total += f.size;
  }
  if (total > ATTACH_MAX_TOTAL_BYTES) return `суммарно больше ${fmtBytes(ATTACH_MAX_TOTAL_BYTES)}`;
  return null;
}

function BotSendPanel({ guildId }: { guildId: string }) {
  const picker = usePicker(guildId);
  const queryClient = useQueryClient();
  const [targets, setTargets] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [presetError, setPresetError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [armed, setArmed] = useState(false); // первое нажатие при N>1 — только взводишь подтверждение
  const [results, setResults] = useState<SendResult[] | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const presets = useQuery({
    queryKey: ["chat-presets", guildId],
    queryFn: () => api.chatPresets(guildId),
  });

  const liveChannels = picker.data?.textChannels ?? [];
  const options = liveChannels.map((c) => ({ label: c.name, value: c.id }));
  const nameOfChannel = (id: string) => liveChannels.find((c) => c.id === id)?.name ?? `#${id}`;

  const trimmed = text.trim();
  const ready = !sending && (trimmed.length > 0 || files.length > 0) && targets.length > 0;
  const needsConfirm = targets.length > 1;

  const addFiles = (incoming: FileList | null) => {
    if (!incoming || incoming.length === 0) return;
    const next = [...files, ...Array.from(incoming)];
    const problem = validateAttachments(next);
    if (problem) {
      setAttachError(problem);
      return;
    }
    setAttachError(null);
    setFiles(next);
    setArmed(false);
  };

  const removeFile = (index: number) => {
    setFiles((prev) => prev.filter((_, i) => i !== index));
    setAttachError(null);
  };

  const dispatch = async (body: string, attach: File[], clearOnOk: boolean) => {
    setArmed(false);
    setSending(true);
    setResults([]);
    const out: SendResult[] = [];
    for (const channelId of targets) {
      try {
        await api.botMessage(guildId, channelId, body, attach);
        out.push({ channelId, ok: true });
      } catch (err) {
        out.push({ channelId, ok: false, error: err instanceof Error ? err.message : String(err) });
      }
      setResults([...out]);
      await new Promise((r) => setTimeout(r, SEND_PAUSE_MS));
    }
    setSending(false);
    if (clearOnOk && out.every((r) => r.ok)) {
      setText("");
      setFiles([]);
    }
  };

  const send = async () => {
    if (!ready) return;
    if (needsConfirm && !armed) {
      setArmed(true);
      return;
    }
    await dispatch(trimmed, files, true);
  };

  const sendPreset = (preset: ChatPreset) => {
    if (sending || targets.length === 0) return;
    if (targets.length > 1) {
      confirmDialog({
        header: "Отправка пресета",
        message: `Отправить пресет в ${targets.length} канала?`,
        icon: "pi pi-exclamation-triangle",
        acceptLabel: "Отправить",
        rejectLabel: "Отмена",
        accept: () => void dispatch(preset.text, [], false),
      });
      return;
    }
    void dispatch(preset.text, [], false);
  };

  const savePreset = async () => {
    if (!trimmed) return;
    try {
      setPresetError(null);
      await api.chatPresetAdd(guildId, trimmed);
      await queryClient.invalidateQueries({ queryKey: ["chat-presets", guildId] });
    } catch (err) {
      setPresetError(err instanceof Error ? err.message : String(err));
    }
  };

  const removePreset = async (presetId: string) => {
    try {
      setPresetError(null);
      await api.chatPresetRemove(guildId, presetId);
      await queryClient.invalidateQueries({ queryKey: ["chat-presets", guildId] });
    } catch (err) {
      setPresetError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="send-panel">
      <div className="send-row">
        <MultiSelect
          className="send-targets"
          value={targets}
          options={options}
          optionLabel="label"
          optionValue="value"
          onChange={(e) => {
            setTargets((e.value as string[]) ?? []);
            setArmed(false);
          }}
          placeholder="каналы для отправки"
          selectedItemsLabel="выбрано: {0}"
          maxSelectedLabels={2}
          display="chip"
          filter
          disabled={sending}
        />
        <span className="muted tiny">живые текстовые каналы сервера (из Discord, не из истории)</span>
      </div>
      <InputTextarea
        rows={3}
        value={text}
        maxLength={MESSAGE_MAX_LEN}
        placeholder="текст сообщения — придёт от имени бота (можно без текста, только файлы)"
        disabled={sending}
        onChange={(e) => {
          setText(String(e.target.value ?? ""));
          setArmed(false);
        }}
      />
      <div className="preset-row">
        <Button className="chip" icon="pi pi-bookmark" disabled={!trimmed || sending} onClick={() => void savePreset()}>
          сохранить как пресет
        </Button>
        {(presets.data?.items.length ?? 0) > 0 && (
          <span className="muted tiny">клик по пресету — отправить в выбранные каналы</span>
        )}
      </div>
      {presetError && <p className="hint danger">{presetError}</p>}
      {(presets.data?.items.length ?? 0) > 0 && (
        <div className="preset-list">
          {presets.data!.items.map((p) => (
            <span className="chip preset-chip" key={p.id} title={p.text}>
              <button
                type="button"
                className="preset-send"
                disabled={sending || targets.length === 0}
                onClick={() => sendPreset(p)}
              >
                {p.text.length > 70 ? `${p.text.slice(0, 70)}…` : p.text}
              </button>
              <Button className="chip-x" title="подставить в текст" disabled={sending} onClick={() => setText(p.text)}>
                ✎
              </Button>
              <Button className="chip-x" title="удалить пресет" disabled={sending} onClick={() => void removePreset(p.id)}>
                ×
              </Button>
            </span>
          ))}
        </div>
      )}
      <div className="send-row">
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            addFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <Button className="chip" disabled={sending} onClick={() => fileInput.current?.click()}>
          📎 вложения
        </Button>
        {files.length > 0 && (
          <span className="chips">
            {files.map((f, i) => (
              <span className="chip" key={`${f.name}-${i}`}>
                {f.name} <span className="muted tiny">({fmtBytes(f.size)})</span>
                <Button className="chip-x" title="убрать" disabled={sending} onClick={() => removeFile(i)}>
                  ×
                </Button>
              </span>
            ))}
          </span>
        )}
      </div>
      {attachError && <p className="hint danger">{attachError}</p>}
      <div className="send-row">
        <Button disabled={!ready} loading={sending} onClick={() => void send()}>
          {sending ? "отправка…" : armed ? `подтвердить отправку (${targets.length} каналов)` : "отправить"}
        </Button>
        <span className="muted tiny">
          {text.length}/{MESSAGE_MAX_LEN}
        </span>
        {needsConfirm && !armed && !sending && <span className="muted tiny">отправка попросит подтверждение</span>}
      </div>
      {results && results.length > 0 && (
        <ul className="send-results">
          {results.map((r) => (
            <li key={r.channelId} className={r.ok ? "ok" : "fail"}>
              {r.ok ? "✓" : "✗"} #{nameOfChannel(r.channelId)}
              {r.error ? ` — ${r.error}` : ""}
            </li>
          ))}
        </ul>
      )}
      {results && results.length > 0 && results.every((r) => r.ok) && (
        <p className="muted tiny">отправлено; в ленте сообщения появятся через несколько секунд</p>
      )}
    </div>
  );
}

export default function Chat() {
  const { guildId = "" } = useParams();
  const names = useNames(guildId);
  const canWrite = useCanWrite(guildId);
  const channels = useQuery({
    queryKey: ["chat-channels", guildId],
    queryFn: () => api.chatChannels(guildId),
  });

  const [showFilters, setShowFilters] = useState(false);
  const [showSend, setShowSend] = useState(false);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [cursor, setCursor] = useState<string | null | undefined>(undefined); // undefined: ещё не грузили
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const set = (patch: Partial<Filters>) => setFilters((f) => ({ ...f, ...patch }));

  const available = channels.data?.items ?? [];

  // панель фильтров: автор, тип, период (канал и порядок дат живут в ленте)
  const activeCount = [filters.userId, filters.type, filters.dateFrom, filters.dateTo].filter(Boolean).length;

  const fetchPage = useCallback(
    async (extra: { before?: string; after?: string }) => {
      const seq = ++requestSeq.current;
      setLoading(true);
      setError(null);
      try {
        const page = await api.chatMessages(guildId, {
          channelId: filters.channelIds,
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

  // бэкенд отдаёт страницу хронологически; при «сначала новые» разворачиваем ленту
  const feed = filters.sort === "desc" ? [...messages].reverse() : messages;

  const channelOptions = available.map((c) => ({
    value: c.channelId,
    label: `${nameOf(names.data, "channel", c.channelId)} (${c.count})`,
  }));

  return (
    <Section title="История чата">
      {canWrite && (
        <div className="toolbar">
          <Button
            className={showSend ? "chip active" : "chip"}
            icon="pi pi-send"
            onClick={() => setShowSend((v) => !v)}
          >
            {showSend ? "скрыть отправку" : "отправить сообщение"}
          </Button>
        </div>
      )}
      {canWrite && showSend && <BotSendPanel guildId={guildId} />}
      {available.length === 0 ? (
        <Empty>
          История сообщений ещё не записывается в базу (коллекция chat_messages пуста). Как только бот начнёт её
          сохранять, переписка появится здесь.
        </Empty>
      ) : (
        <>
          <div className="toolbar">
            <Button
              className={showFilters || activeCount > 0 ? "chip active" : "chip"}
              onClick={() => setShowFilters((v) => !v)}
            >
              Фильтры
              {activeCount > 0 && <span className="count-badge">{activeCount}</span>}
            </Button>
            <MultiSelect
              className="chat-channel-select"
              value={filters.channelIds}
              options={channelOptions}
              optionLabel="label"
              optionValue="value"
              onChange={(e) => set({ channelIds: (e.value as string[]) ?? [] })}
              placeholder="все каналы"
              selectedItemsLabel="каналов: {0}"
              maxSelectedLabels={2}
              display="chip"
              filter
            />
            <Button
              className="sort-toggle"
              title="порядок по дате"
              onClick={() => set({ sort: filters.sort === "desc" ? "asc" : "desc" })}
            >
              <span className="sort-arrow">{filters.sort === "desc" ? "↓" : "↑"}</span>
              {filters.sort === "desc" ? "сначала новые" : "сначала старые"}
            </Button>
          </div>
          {showFilters && (
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
                <SelectButton
                  className="chip-group"
                  value={filters.type}
                  options={TYPE_FILTERS.map((t) => ({ label: t.label, value: t.key }))}
                  optionValue="value"
                  onChange={(e) => set({ type: e.value as string })}
                />
              </div>
              <div className="filter-block">
                <h4>Период (границы включительно)</h4>
                <div className="filter-row">
                  <span className="chart-control">
                    <span>с</span>
                    <DateField
                      value={filters.dateFrom}
                      placeholder="дд.мм.гггг"
                      onChange={(v) => set({ dateFrom: v })}
                    />
                  </span>
                  <span className="chart-control">
                    <span>по</span>
                    <DateField
                      value={filters.dateTo}
                      placeholder="дд.мм.гггг"
                      onChange={(v) => set({ dateTo: v })}
                    />
                  </span>
                </div>
              </div>
              {activeCount > 0 && (
                <div className="filter-actions">
                  <span className="muted tiny">
                    {filters.type ? `тип: ${TYPE_LABELS[filters.type]} · ` : ""}
                    {filters.userId ? `автор: ${nameOf(names.data, "user", filters.userId) || filters.userId} · ` : ""}
                    {(filters.dateFrom || filters.dateTo) && `период: ${filters.dateFrom || "…"} — ${filters.dateTo || "…"}`}
                  </span>
                  <Button
                    className="linklike"
                    onClick={() => set({ userId: "", type: "", dateFrom: "", dateTo: "" })}
                  >
                    сбросить фильтры
                  </Button>
                </div>
              )}
            </div>
          )}
          {error && <ErrorBox error={new Error(error)} />}
          {cursor !== undefined && cursor !== null && (
            <div className="toolbar">
              <Button
                disabled={loading}
                onClick={() =>
                  void fetchPage(
                    filters.sort === "desc" ? { before: cursor ?? undefined } : { after: cursor ?? undefined },
                  )
                }
              >
                {loading ? "загрузка…" : filters.sort === "desc" ? "показать более ранние" : "показать более новые"}
              </Button>
            </div>
          )}
          {loading && messages.length === 0 ? (
            <Loading />
          ) : messages.length === 0 && !error ? (
            <>
              <Empty>Нет сообщений под выбранные фильтры.</Empty>
              {activeCount > 0 && (
                <p className="muted tiny">
                  <Button className="linklike" onClick={() => setShowFilters(true)}>
                    ослабить фильтры
                  </Button>
                </p>
              )}
            </>
          ) : (
            <div className="chat-list">
              {feed.map((m) => (
                <div className="chat-message" key={m.messageId} data-deleted={m.deletedAt ? "true" : undefined}>
                  <span className="chat-time">{fmtDate(m.sentAt)}</span>
                  <strong
                    className="chat-author"
                    title={m.authorUserId}
                    style={
                      names.data?.userColors?.[m.authorUserId]
                        ? { color: names.data.userColors[m.authorUserId] }
                        : undefined
                    }
                  >
                    {nameOf(names.data, "user", m.authorUserId) || m.authorName || m.authorUserId}
                  </strong>
                  <span className="chat-main">
                    {filters.channelIds.length !== 1 && m.channelId && (
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
    </Section>
  );
}
