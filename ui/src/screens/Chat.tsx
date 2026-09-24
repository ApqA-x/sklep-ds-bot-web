import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { Button } from "primereact/button";
import { MultiSelect } from "primereact/multiselect";
import { SelectButton } from "primereact/selectbutton";
import { InputTextarea } from "primereact/inputtextarea";
import { InputText } from "primereact/inputtext";
import { TabView, TabPanel } from "primereact/tabview";
import { ColorPicker } from "primereact/colorpicker";
import { api, useCanWrite } from "../api/client";
import type { ChatAttachment, ChatMessage, ChatPreset, EmbedSpec } from "../api/types";
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
  const [presetName, setPresetName] = useState("");
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
  const textPresets = (presets.data?.items ?? []).filter((p) => p.kind !== "embed");

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

  const dispatch = async (body: string, attach: File[], clearOnOk: boolean, channels: string[]) => {
    setArmed(false);
    setSending(true);
    setResults([]);
    const out: SendResult[] = [];
    for (const channelId of channels) {
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
    await dispatch(trimmed, files, true, targets);
  };

  const applyPreset = (preset: ChatPreset) => {
    setPresetError(null);
    setText(preset.text);
    setTargets(preset.channelIds);
    setArmed(false);
  };

  const savePreset = async () => {
    if (!trimmed || targets.length === 0) return;
    try {
      setPresetError(null);
      await api.chatPresetAdd(guildId, trimmed, presetName.trim() || null, targets);
      setPresetName("");
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
        <InputText
          value={presetName}
          maxLength={100}
          placeholder="название пресета"
          disabled={sending}
          onChange={(e) => setPresetName(String(e.target.value ?? ""))}
        />
        <Button
          className="chip"
          icon="pi pi-bookmark"
          disabled={!trimmed || targets.length === 0 || sending}
          onClick={() => void savePreset()}
        >
          сохранить как пресет
        </Button>
        {trimmed && targets.length === 0 ? (
          <span className="muted tiny">для пресета выбери каналы</span>
        ) : (
          textPresets.length > 0 && (
            <span className="muted tiny">клик по пресету — заполнит текст и каналы, отправка кнопкой</span>
          )
        )}
      </div>
      {presetError && <p className="hint danger">{presetError}</p>}
      {textPresets.length > 0 && (
        <div className="preset-list">
          {textPresets.map((p) => (
            <span
              className={sending ? "chip preset-chip disabled" : "chip preset-chip"}
              key={p.id}
              title={`${p.text}\nканалы: ${p.channelIds.map(nameOfChannel).join(", ") || "не сохранены"}`}
              onClick={() => {
                if (!sending) applyPreset(p);
              }}
            >
              <span className="preset-label">
                {p.name ?? (p.text.length > 70 ? `${p.text.slice(0, 70)}…` : p.text)}
              </span>
              <span className="muted tiny">{p.channelIds.map(nameOfChannel).join(" ")}</span>
              <Button
                className="chip-x"
                title="удалить пресет"
                disabled={sending}
                onClick={(e) => {
                  e.stopPropagation();
                  void removePreset(p.id);
                }}
              >
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

type EmbedSlot = "image" | "thumbnail" | "authorIcon" | "footerIcon";

type ImageRef = { file: File; name: string; url: string };

const EMPTY_EMBED_FORM = {
  caption: "",
  title: "",
  description: "",
  color: "#000000",
  authorName: "",
  footerText: "",
};

const SLOT_LABELS: Record<EmbedSlot, string> = {
  image: "Image (большая картинка)",
  thumbnail: "Thumbnail (миниатюра справа)",
  authorIcon: "Author — иконка",
  footerIcon: "Footer — иконка",
};

function uniqueAttachmentName(base: string, taken: Set<string>): string {
  if (!taken.has(base)) return base;
  const dot = base.lastIndexOf(".");
  const stem = dot > 0 ? base.slice(0, dot) : base;
  const ext = dot > 0 ? base.slice(dot) : "";
  for (let i = 1; ; i++) {
    const candidate = `${stem}-${i}${ext}`;
    if (!taken.has(candidate)) return candidate;
  }
}

function intToHex(value: number): string {
  return `#${(value & 0xffffff).toString(16).padStart(6, "0")}`;
}

// ColorPicker (format=hex) отдаёт значение БЕЗ "#" (ff0000) — нормализуем к "#ff0000"
function normalizeHexColor(value: unknown): string {
  const raw = String(value ?? "").trim().toLowerCase();
  if (raw === "") return "#000000";
  return raw.startsWith("#") ? raw : `#${raw}`;
}

function hexToInt(hex: string): number | null {
  const clean = hex.replace(/^#/, "");
  if (!/^[0-9a-f]{6}$/i.test(clean)) return null;
  return parseInt(clean, 16);
}

// 422 от сервера -> поле, которое нужно подсветить
function fieldOfApiError(message: string): string | null {
  const m = message.toLowerCase();
  if (m.includes("title")) return "title";
  if (m.includes("description")) return "description";
  if (m.includes("author")) return "authorName";
  if (m.includes("footer")) return "footerText";
  if (m.includes("color")) return "color";
  if (m.includes("channel")) return "targets";
  if (m.includes("embed is empty") || m.includes("content or embed")) return "content";
  if (m.includes("6000")) return "content";
  if (m.includes("attachment")) return "files";
  if (m.includes("content")) return "caption";
  return null;
}

function EmbedPreview({ form, slots }: { form: typeof EMPTY_EMBED_FORM; slots: Record<EmbedSlot, ImageRef | null> }) {
  return (
    <div className="embed-preview-card" style={{ borderLeftColor: form.color || "#000000" }}>
      {(slots.authorIcon || form.authorName) && (
        <div className="embed-author">
          {slots.authorIcon && <img src={slots.authorIcon.url} alt="" className="embed-icon" />}
          {form.authorName && <span className="embed-author-name">{form.authorName}</span>}
        </div>
      )}
      {form.title && <div className="embed-title">{form.title}</div>}
      <div className="embed-body">
        {form.description && <div className="embed-description">{form.description}</div>}
        {slots.thumbnail && <img src={slots.thumbnail.url} alt="" className="embed-thumbnail" />}
      </div>
      {slots.image && <img src={slots.image.url} alt="" className="embed-image" />}
      {(form.footerText || slots.footerIcon) && (
        <div className="embed-footer">
          {slots.footerIcon && <img src={slots.footerIcon.url} alt="" className="embed-icon" />}
          {form.footerText && <span>{form.footerText}</span>}
        </div>
      )}
    </div>
  );
}

function EmbedSendPanel({ guildId }: { guildId: string }) {
  const picker = usePicker(guildId);
  const queryClient = useQueryClient();
  const [targets, setTargets] = useState<string[]>([]);
  const [form, setForm] = useState(EMPTY_EMBED_FORM);
  const [slots, setSlots] = useState<Record<EmbedSlot, ImageRef | null>>({
    image: null,
    thumbnail: null,
    authorIcon: null,
    footerIcon: null,
  });
  const [embedError, setEmbedError] = useState<string | null>(null);
  const [sendError, setSendError] = useState<string | null>(null);
  const [errFields, setErrFields] = useState<Set<string>>(new Set());
  const [presetError, setPresetError] = useState<string | null>(null);
  const [presetName, setPresetName] = useState("");
  const [sending, setSending] = useState(false);
  const [armed, setArmed] = useState(false);
  const [results, setResults] = useState<SendResult[] | null>(null);
  const fileInputs = useRef<Record<EmbedSlot, HTMLInputElement | null>>({
    image: null,
    thumbnail: null,
    authorIcon: null,
    footerIcon: null,
  });
  const slotsRef = useRef(slots);
  slotsRef.current = slots;
  useEffect(
    () => () => {
      Object.values(slotsRef.current).forEach((ref) => ref && URL.revokeObjectURL(ref.url));
    },
    [],
  );

  const presets = useQuery({
    queryKey: ["chat-presets", guildId],
    queryFn: () => api.chatPresets(guildId),
  });

  const liveChannels = picker.data?.textChannels ?? [];
  const options = liveChannels.map((c) => ({ label: c.name, value: c.id }));
  const nameOfChannel = (id: string) => liveChannels.find((c) => c.id === id)?.name ?? `#${id}`;
  const embedPresets = (presets.data?.items ?? []).filter((p) => p.kind === "embed");

  const set = (patch: Partial<typeof EMPTY_EMBED_FORM>) => {
    setForm((f) => ({ ...f, ...patch }));
    setArmed(false);
    setErrFields(new Set());
    setSendError(null);
  };

  const markError = (fields: string[], message: string) => {
    setErrFields(new Set(fields));
    setSendError(message);
  };

  const ef = (name: string) => (errFields.has(name) ? "embed-field field-error" : "embed-field");

  const hasAnyImage = Object.values(slots).some(Boolean);
  const hasAnyText = Boolean(
    form.title.trim() || form.description.trim() || form.authorName.trim() || form.footerText.trim(),
  );
  const hasContent = hasAnyImage || hasAnyText;
  const needsConfirm = targets.length > 1;

  const attachFile = (slot: EmbedSlot, incoming: File | null) => {
    if (!incoming) return;
    if (!incoming.type.startsWith("image/")) {
      setEmbedError("в блок можно приложить только картинки");
      return;
    }
    if (incoming.size > ATTACH_MAX_FILE_BYTES) {
      setEmbedError(`файл «${incoming.name}» больше ${fmtBytes(ATTACH_MAX_FILE_BYTES)}`);
      return;
    }
    setEmbedError(null);
    const taken = new Set(
      Object.entries(slotsRef.current)
        .filter(([s]) => s !== slot)
        .map(([, ref]) => ref?.name)
        .filter((n): n is string => Boolean(n)),
    );
    const name = uniqueAttachmentName(incoming.name, taken);
    setSlots((prev) => {
      const old = prev[slot];
      if (old) URL.revokeObjectURL(old.url);
      return { ...prev, [slot]: { file: incoming, name, url: URL.createObjectURL(incoming) } };
    });
    setArmed(false);
  };

  const detachSlot = (slot: EmbedSlot) => {
    setSlots((prev) => {
      const old = prev[slot];
      if (old) URL.revokeObjectURL(old.url);
      return { ...prev, [slot]: null };
    });
    setArmed(false);
  };

  const specFor = (withFiles: boolean): EmbedSpec => {
    const color = hexToInt(form.color);
    const spec: EmbedSpec = {
      title: form.title.trim() || null,
      description: form.description.trim() || null,
      color,
      authorName: form.authorName.trim() || null,
      footerText: form.footerText.trim() || null,
    };
    if (withFiles) {
      spec.image = slots.image?.name ?? null;
      spec.thumbnail = slots.thumbnail?.name ?? null;
      spec.authorIcon = slots.authorIcon?.name ?? null;
      spec.footerIcon = slots.footerIcon?.name ?? null;
    }
    return spec;
  };

  const send = async () => {
    if (sending) return;
    const missing: string[] = [];
    if (targets.length === 0) missing.push("targets");
    if (!hasContent) missing.push("content");
    if (missing.length > 0) {
      const messages: string[] = [];
      if (targets.length === 0) messages.push("не выбран канал для отправки");
      if (!hasContent) messages.push("блок пуст: заполни хотя бы одно поле или приложи картинку");
      markError(missing, messages.join("; "));
      return;
    }
    if (needsConfirm && !armed) {
      setArmed(true);
      return;
    }
    setArmed(false);
    setErrFields(new Set());
    setSendError(null);
    setSending(true);
    setResults([]);
    const spec = specFor(true);
    const files = Object.values(slots)
      .filter((r): r is ImageRef => r !== null)
      .map((r) => (r.file.name === r.name ? r.file : new File([r.file], r.name, { type: r.file.type })));
    const caption = form.caption.trim();
    const out: SendResult[] = [];
    for (const channelId of targets) {
      try {
        await api.botMessage(guildId, channelId, caption, files, spec);
        out.push({ channelId, ok: true });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        out.push({ channelId, ok: false, error: message });
        const field = fieldOfApiError(message);
        setErrFields(new Set(field ? [field] : []));
        setSendError(message);
      }
      setResults([...out]);
      await new Promise((r) => setTimeout(r, SEND_PAUSE_MS));
    }
    setSending(false);
  };

  const applyPreset = (preset: ChatPreset) => {
    setPresetError(null);
    const e = preset.embed ?? {};
    setForm({
      caption: "",
      title: e.title ?? "",
      description: e.description ?? "",
      color: typeof e.color === "number" ? intToHex(e.color) : "#000000",
      authorName: e.authorName ?? "",
      footerText: e.footerText ?? "",
    });
    setTargets(preset.channelIds);
    setArmed(false);
  };

  const savePreset = async () => {
    if (!hasAnyText || targets.length === 0) return;
    try {
      setPresetError(null);
      await api.chatPresetAddEmbed(guildId, specFor(false), presetName.trim() || null, targets);
      setPresetName("");
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
      <div className={errFields.has("targets") ? "send-row field-error" : "send-row"}>
        <MultiSelect
          className="send-targets"
          value={targets}
          options={options}
          optionLabel="label"
          optionValue="value"
          onChange={(e) => {
            setTargets((e.value as string[]) ?? []);
            setArmed(false);
            setErrFields((prev) => {
              if (!prev.has("targets")) return prev;
              const next = new Set(prev);
              next.delete("targets");
              return next;
            });
          }}
          placeholder="каналы для отправки"
          selectedItemsLabel="выбрано: {0}"
          maxSelectedLabels={2}
          display="chip"
          filter
          disabled={sending}
        />
        <span className="muted tiny">живые текстовые каналы сервера</span>
      </div>
      <div className={errFields.has("content") || errFields.has("files") ? "embed-form-card field-error" : "embed-form-card"}>
        <div className="embed-form-head">
          Блок (embed) — <strong>все поля необязательны</strong>, но должно быть заполнено хотя бы одно
        </div>
        <div className="embed-grid">
        <div className="embed-fields">
          <div className={ef("title")}>
            <h4>Title <span className="muted tiny">— необязательно</span></h4>
            <InputText
              value={form.title}
              maxLength={256}
              placeholder="заголовок блока"
              disabled={sending}
              onChange={(e) => set({ title: String(e.target.value ?? "") })}
            />
          </div>
          <div className={ef("description")}>
            <h4>Description <span className="muted tiny">— необязательно</span></h4>
            <InputTextarea
              rows={4}
              value={form.description}
              maxLength={4000}
              placeholder="описание"
              disabled={sending}
              onChange={(e) => set({ description: String(e.target.value ?? "") })}
            />
          </div>
          <div className={ef("color")}>
            <h4>Color <span className="muted tiny">— необязательно</span></h4>
            <div className="embed-file-row">
              <ColorPicker
                value={form.color}
                format="hex"
                disabled={sending}
                onChange={(e) => set({ color: normalizeHexColor(e.value) })}
              />
              <span className="muted tiny">{form.color.toUpperCase()} (по умолчанию чёрный)</span>
            </div>
          </div>
          {(["thumbnail", "image"] as EmbedSlot[]).map((slot) => (
            <div className={ef("files")} key={slot}>
              <h4>{SLOT_LABELS[slot]} <span className="muted tiny">— необязательно</span></h4>
              <div className="embed-file-row">
                <input
                  ref={(el) => {
                    fileInputs.current[slot] = el;
                  }}
                  type="file"
                  accept="image/*"
                  hidden
                  onChange={(e) => {
                    attachFile(slot, e.target.files?.[0] ?? null);
                    e.target.value = "";
                  }}
                />
                <Button className="chip" disabled={sending} onClick={() => fileInputs.current[slot]?.click()}>
                  {slots[slot] ? "заменить файл" : "📎 выбрать картинку"}
                </Button>
                {slots[slot] && (
                  <span className="chip">
                    {slots[slot]!.name} <span className="muted tiny">({fmtBytes(slots[slot]!.file.size)})</span>
                    <Button className="chip-x" title="убрать" disabled={sending} onClick={() => detachSlot(slot)}>
                      ×
                    </Button>
                  </span>
                )}
              </div>
            </div>
          ))}
          <div className={ef("authorName")}>
            <h4>Author <span className="muted tiny">— необязательно</span></h4>
            <div className="embed-file-row">
              <InputText
                value={form.authorName}
                maxLength={256}
                placeholder="имя автора"
                disabled={sending}
                onChange={(e) => set({ authorName: String(e.target.value ?? "") })}
              />
              <input
                ref={(el) => {
                  fileInputs.current.authorIcon = el;
                }}
                type="file"
                accept="image/*"
                hidden
                onChange={(e) => {
                  attachFile("authorIcon", e.target.files?.[0] ?? null);
                  e.target.value = "";
                }}
              />
              <Button className="chip" disabled={sending} onClick={() => fileInputs.current.authorIcon?.click()}>
                {slots.authorIcon ? `иконка: ${slots.authorIcon.name}` : "иконка"}
              </Button>
              {slots.authorIcon && (
                <Button className="chip-x" title="убрать иконку" disabled={sending} onClick={() => detachSlot("authorIcon")}>
                  ×
                </Button>
              )}
            </div>
          </div>
          <div className={ef("footerText")}>
            <h4>Footer <span className="muted tiny">— необязательно</span></h4>
            <div className="embed-file-row">
              <InputText
                value={form.footerText}
                maxLength={2048}
                placeholder="текст футера"
                disabled={sending}
                onChange={(e) => set({ footerText: String(e.target.value ?? "") })}
              />
              <input
                ref={(el) => {
                  fileInputs.current.footerIcon = el;
                }}
                type="file"
                accept="image/*"
                hidden
                onChange={(e) => {
                  attachFile("footerIcon", e.target.files?.[0] ?? null);
                  e.target.value = "";
                }}
              />
              <Button className="chip" disabled={sending} onClick={() => fileInputs.current.footerIcon?.click()}>
                {slots.footerIcon ? `иконка: ${slots.footerIcon.name}` : "иконка"}
              </Button>
              {slots.footerIcon && (
                <Button className="chip-x" title="убрать иконку" disabled={sending} onClick={() => detachSlot("footerIcon")}>
                  ×
                </Button>
              )}
            </div>
          </div>
          <div className={ef("caption")}>
            <h4>Подпись <span className="muted tiny">— необязательно</span></h4>
            <InputTextarea
              rows={2}
              value={form.caption}
              maxLength={MESSAGE_MAX_LEN}
              placeholder="обычный текст над блоком"
              disabled={sending}
              onChange={(e) => set({ caption: String(e.target.value ?? "") })}
            />
          </div>
        </div>
        <div className="embed-preview-wrap">
          <h4 className="muted tiny">предпросмотр</h4>
          {hasContent ? (
            <>
              {form.caption.trim() && <div className="embed-caption">{form.caption}</div>}
              <EmbedPreview form={form} slots={slots} />
            </>
          ) : (
            <p className="muted tiny">заполните поля — блок появится здесь</p>
          )}
        </div>
      </div>
      </div>
      {embedError && <p className="hint danger">{embedError}</p>}
      {sendError && <p className="hint danger">{sendError}</p>}
      <div className="preset-row">
        <InputText
          value={presetName}
          maxLength={100}
          placeholder="название пресета"
          disabled={sending}
          onChange={(e) => setPresetName(String(e.target.value ?? ""))}
        />
        <Button
          className="chip"
          icon="pi pi-bookmark"
          disabled={!hasAnyText || targets.length === 0 || sending}
          onClick={() => void savePreset()}
        >
          сохранить блок как пресет
        </Button>
        <span className="muted tiny">картинки в пресет не сохраняются — после применения приложите их заново</span>
      </div>
      {presetError && <p className="hint danger">{presetError}</p>}
      {embedPresets.length > 0 && (
        <div className="preset-list">
          {embedPresets.map((p) => (
            <span
              className={sending ? "chip preset-chip disabled" : "chip preset-chip"}
              key={p.id}
              title={`${p.embed?.title ?? ""}\n${p.embed?.description ?? ""}\nканалы: ${p.channelIds.map(nameOfChannel).join(", ") || "не сохранены"}`}
              onClick={() => {
                if (!sending) applyPreset(p);
              }}
            >
              <span className="preset-label">{p.name ?? p.embed?.title ?? "(без названия)"}</span>
              <span className="muted tiny">{p.channelIds.map(nameOfChannel).join(" ")}</span>
              <Button
                className="chip-x"
                title="удалить пресет"
                disabled={sending}
                onClick={(e) => {
                  e.stopPropagation();
                  void removePreset(p.id);
                }}
              >
                ×
              </Button>
            </span>
          ))}
        </div>
      )}
      <div className="send-row">
        <Button disabled={sending} loading={sending} onClick={() => void send()}>
          {sending ? "отправка…" : armed ? `подтвердить отправку (${targets.length} каналов)` : "отправить блок"}
        </Button>
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

function SendTabs({ guildId }: { guildId: string }) {
  return (
    <TabView className="send-tabs">
      <TabPanel header="Текст">
        <BotSendPanel guildId={guildId} />
      </TabPanel>
      <TabPanel header="Отправить блок">
        <EmbedSendPanel guildId={guildId} />
      </TabPanel>
    </TabView>
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
            className={showSend ? "chip active send-open" : "chip send-open"}
            icon="pi pi-send"
            onClick={() => setShowSend((v) => !v)}
          >
            {showSend ? "скрыть отправку" : "отправить сообщение"}
          </Button>
        </div>
      )}
      {canWrite && showSend && <SendTabs guildId={guildId} />}
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
