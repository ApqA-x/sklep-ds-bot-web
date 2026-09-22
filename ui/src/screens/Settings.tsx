import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, useCanWrite } from "../api/client";
import type { VoiceChannelOption } from "../api/types";
import { ErrorBox, Loading, Section } from "../components/ui";
import { DName, type NameKind } from "../names";
import { usePicker } from "../names";

const ACTIVITY_EVENT_TYPES = [
  "member_join",
  "member_leave",
  "invite_create",
  "invite_delete",
  "invite_used",
  "message_create",
  "message_update",
  "message_delete",
  "reaction_add",
  "reaction_remove",
  "voice_join",
  "voice_leave",
  "voice_move",
  "profile_nickname_update",
  "profile_roles_update",
];

// категории activity-карточек, которые понимает бот (domain.ACTIVITY_CATEGORIES)
const ACTIVITY_CATEGORIES: [string, string][] = [
  ["join-leave", "Заходы/уходы и инвайты"],
  ["messages", "Сообщения и реакции"],
  ["voice-log", "Голосовой лог"],
  ["profile", "Профили (ник/роли)"],
];

// слэш-команды бота: корень, описание, дефолтный доступ
const BOT_COMMANDS: [string, string, "all" | "admin"][] = [
  ["jump", "перейти к голосовому каналу участника", "all"],
  ["dashboard", "топ по голосу на сервере", "all"],
  ["userinfo", "справка о пользователе", "all"],
  ["stalker", "подписки на участников", "all"],
  ["settings", "настройки бота (саммари, активность)", "admin"],
  ["inspect", "осмотр активных/исторических сессий", "admin"],
  ["autorole", "роль за приглашение", "admin"],
  ["unmute", "снять тайм-аут", "admin"],
  ["trusted", "доверенные пользователи", "admin"],
  ["connect", "подключить бота в канал", "admin"],
  ["disconnect", "отключить бота от канала", "admin"],
  ["status", "состояние бота", "admin"],
];

const EDITABLE_KEYS = [
  "trackingMode",
  "trackedChannelIds",
  "summaryChannelId",
  "fallbackSummaryChannelId",
  "autoRoleId",
  "soundboardEnforcementEnabled",
  "activityChannelId",
  "activityCategoryChannelIds",
  "activityEventTypes",
  "commandAccess",
] as const;

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function asRecord(value: unknown): Record<string, string> {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, String(v ?? "")]),
    );
  }
  return {};
}

function NameHint({ kind, value }: { kind: NameKind; value: unknown }) {
  const id = String(value ?? "");
  if (!/^\d{5,25}$/.test(id)) return null;
  return (
    <span className="muted">
      {" "}
      → <DName kind={kind} id={id} />
    </span>
  );
}

// Выпадающий список каналов: живые имена из /picker; выбранный id, которого нет
// в списке (бот вышел/канал удалён), сохраняется отдельной опцией.
function ChannelSelect({
  label,
  value,
  options,
  allowEmpty,
  disabled,
  onChange,
}: {
  label: string;
  value: unknown;
  options: VoiceChannelOption[];
  allowEmpty?: string;
  disabled: boolean;
  onChange: (id: string) => void;
}) {
  const current = String(value ?? "");
  const known = options.some((c) => c.id === current);
  return (
    <label className="field">
      <span>{label}</span>
      <select value={current} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        {allowEmpty !== undefined && <option value="">{allowEmpty}</option>}
        {current !== "" && !known && <option value={current}>{current} (нет в списке)</option>}
        {options.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
      {!known && <NameHint kind="channel" value={current} />}
    </label>
  );
}

function normalize(value: unknown): unknown {
  if (value === undefined || value === null) return "";
  return value;
}

function IdList({
  title,
  kind,
  ids,
  disabled,
  onAdd,
  onRemove,
  busy,
  options,
}: {
  title: string;
  kind: NameKind;
  ids: string[];
  disabled: boolean;
  onAdd: (id: string) => void;
  onRemove: (id: string) => void;
  busy: boolean;
  options?: VoiceChannelOption[];
}) {
  const [input, setInput] = useState("");
  const [manual, setManual] = useState(false);
  const select = options && options.length > 0;
  return (
    <div className="idlist">
      <div className="idlist-head">
        <strong>{title}</strong>
        {!disabled && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const clean = input.trim();
              if (!/^\d{5,25}$/.test(clean)) return;
              onAdd(clean);
              setInput("");
            }}
          >
            {select && !manual ? (
              <>
                <select value={input} onChange={(e) => setInput(e.target.value)} aria-label={`${title} канал`}>
                  <option value="">— канал —</option>
                  {options.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
                <button type="button" className="linklike" onClick={() => setManual(true)}>
                  id вручную
                </button>
              </>
            ) : (
              <>
                <input
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="user/channel id"
                  aria-label={`${title} id`}
                />
                {select && (
                  <button type="button" className="linklike" onClick={() => setManual(false)}>
                    из списка
                  </button>
                )}
              </>
            )}
            <button type="submit" disabled={busy || input.trim() === ""}>
              добавить
            </button>
          </form>
        )}
      </div>
      <div className="chips">
        {ids.length === 0 && <span className="muted">пусто</span>}
        {ids.map((id) => (
          <span className="chip" key={id} title={id}>
            <DName kind={kind} id={id} />
            {!disabled && (
              <button type="button" onClick={() => onRemove(id)} disabled={busy} aria-label={`удалить ${id}`}>
                ×
              </button>
            )}
          </span>
        ))}
      </div>
    </div>
  );
}

export default function Settings() {
  const { guildId = "" } = useParams();
  const canWrite = useCanWrite(guildId);
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["settings", guildId], queryFn: () => api.settings(guildId) });
  const picker = usePicker(guildId);

  const [form, setForm] = useState<Record<string, unknown>>({});
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (query.data) setForm({ ...query.data });
  }, [query.data]);

  const changed = useMemo(() => {
    const doc = query.data;
    if (!doc) return {} as Record<string, unknown>;
    const fields: Record<string, unknown> = {};
    for (const key of EDITABLE_KEYS) {
      if (JSON.stringify(normalize(form[key])) !== JSON.stringify(normalize(doc[key]))) {
        fields[key] = normalize(form[key]);
      }
    }
    return fields;
  }, [form, query.data]);

  const patch = useMutation({
    mutationFn: (fields: Record<string, unknown>) =>
      api.patchSettings(guildId, {
        ...fields,
        expectedUpdatedAt: (query.data?.updatedAt as string | undefined) ?? undefined,
      }),
    onSuccess: () => {
      setNotice("сохранено");
      queryClient.invalidateQueries({ queryKey: ["settings", guildId] });
    },
    onError: (err: Error) => setNotice(err.message),
  });

  const listAction = useMutation({
    mutationFn: ({
      field,
      userId,
      action,
    }: {
      field: "trusted" | "autoUnmute";
      userId: string;
      action: "add" | "remove";
    }) => (field === "trusted" ? api.trusted(guildId, userId, action) : api.autoUnmute(guildId, userId, action)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["settings", guildId] }),
    onError: (err: Error) => setNotice(err.message),
  });

  const stalker = useMutation({
    mutationFn: ({ watcher, target, action }: { watcher: string; target: string; action: "add" | "remove" }) =>
      api.stalker(guildId, watcher, target, action),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["stalker", guildId] });
      setNotice("обновлено");
    },
    onError: (err: Error) => setNotice(err.message),
  });
  const stalkerList = useQuery({
    queryKey: ["stalker", guildId],
    queryFn: () => api.stalkerList(guildId),
    enabled: canWrite,
  });
  const [stalkerForm, setStalkerForm] = useState({ watcher: "", target: "" });

  if (query.isLoading) return <Loading />;
  if (query.isError) return <ErrorBox error={query.error} />;
  if (!query.data) return null;

  const set = (key: string, value: unknown) => setForm((f) => ({ ...f, [key]: value }));
  const trustedIds = asStringArray(form.trustedUserIds);
  const unmuteIds = asStringArray(form.autoUnmuteUserIds);
  const trackedChannels = asStringArray(form.trackedChannelIds);
  const eventTypes = asStringArray(form.activityEventTypes);
  const textChannels = picker.data?.textChannels ?? [];
  const voiceChannels = picker.data?.voiceChannels ?? [];
  const roleOptions = (picker.data?.roles ?? []).filter((r) => r.assignable);
  const categoryChannels = asRecord(form.activityCategoryChannelIds);
  const commandAccess = asRecord(form.commandAccess);
  const setCategoryChannel = (category: string, channelId: string) => {
    const next = { ...categoryChannels };
    if (channelId === "") delete next[category];
    else next[category] = channelId;
    set("activityCategoryChannelIds", next);
  };
  const setCommandAccess = (name: string, access: string) => {
    const next = { ...commandAccess };
    if (access === "") delete next[name];
    else next[name] = access;
    set("commandAccess", next);
  };

  return (
    <>
      {notice && <p className="hint">{notice}</p>}
      {!canWrite && <p className="hint">Просмотр: для редактирования нужен ADMINISTRATOR.</p>}
      <Section title="Трекинг">
        <label className="field">
          <span>trackingMode</span>
          <select
            value={String(form.trackingMode ?? "all")}
            disabled={!canWrite}
            onChange={(e) => set("trackingMode", e.target.value)}
          >
            <option value="all">all</option>
            <option value="none">none</option>
            <option value="specific">specific</option>
          </select>
        </label>
        {form.trackingMode === "specific" && (
          <IdList
            title="Трекаемые каналы"
            kind="channel"
            ids={trackedChannels}
            disabled={!canWrite}
            busy={patch.isPending}
            options={voiceChannels}
            onAdd={(id) => set("trackedChannelIds", [...new Set([...trackedChannels, id])])}
            onRemove={(id) => set("trackedChannelIds", trackedChannels.filter((x) => x !== id))}
          />
        )}
        <ChannelSelect
          label="Канал саммари сессий"
          value={form.summaryChannelId}
          options={textChannels}
          allowEmpty="— не задан —"
          disabled={!canWrite}
          onChange={(id) => set("summaryChannelId", id)}
        />
        <ChannelSelect
          label="Запасной канал саммари"
          value={form.fallbackSummaryChannelId}
          options={textChannels}
          allowEmpty="— не задан —"
          disabled={!canWrite}
          onChange={(id) => set("fallbackSummaryChannelId", id)}
        />
      </Section>
      <Section title="Activity-карточки">
        <ChannelSelect
          label="Канал активности (по умолчанию)"
          value={form.activityChannelId}
          options={textChannels}
          allowEmpty="— выключено —"
          disabled={!canWrite}
          onChange={(id) => set("activityChannelId", id)}
        />
        <p className="muted tiny">
          Куда выводит результат — можно задать отдельно для каждого типа событий; без настройки идёт в канал выше.
        </p>
        <div className="category-grid">
          {ACTIVITY_CATEGORIES.map(([category, title]) => (
            <ChannelSelect
              key={category}
              label={title}
              value={categoryChannels[category] ?? ""}
              options={textChannels}
              allowEmpty="как канал по умолчанию"
              disabled={!canWrite}
              onChange={(id) => setCategoryChannel(category, id)}
            />
          ))}
        </div>
        <div className="events-grid">
          {ACTIVITY_EVENT_TYPES.map((t) => (
            <label key={t} className="check">
              <input
                type="checkbox"
                disabled={!canWrite}
                checked={eventTypes.includes(t)}
                onChange={(e) =>
                  set(
                    "activityEventTypes",
                    e.target.checked
                      ? [...new Set([...eventTypes, t])]
                      : eventTypes.filter((x) => x !== t),
                  )
                }
              />
              {t}
            </label>
          ))}
        </div>
      </Section>
      <Section title="Команды бота">
        <p className="muted tiny">
          «Все» — команду могут звать обычные участники, «ADMIN» — только с разрешением Administrator. Пусто =
          как по умолчанию у бота. Бот проверяет это при каждом вызове.
        </p>
        <table>
          <thead>
            <tr>
              <th>Команда</th>
              <th>Что делает</th>
              <th>По умолчанию</th>
              <th>Доступ</th>
            </tr>
          </thead>
          <tbody>
            {BOT_COMMANDS.map(([name, description, def]) => (
              <tr key={name}>
                <td>
                  <code>/{name}</code>
                </td>
                <td>{description}</td>
                <td className="muted">{def === "admin" ? "ADMIN" : "все"}</td>
                <td>
                  <select
                    value={commandAccess[name] ?? ""}
                    disabled={!canWrite}
                    onChange={(e) => setCommandAccess(name, e.target.value)}
                  >
                    <option value="">как по умолчанию</option>
                    <option value="all">все</option>
                    <option value="admin">ADMIN ONLY</option>
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      <Section title="Прочее">
        <label className="check">
          <input
            type="checkbox"
            disabled={!canWrite}
            checked={Boolean(form.soundboardEnforcementEnabled)}
            onChange={(e) => set("soundboardEnforcementEnabled", e.target.checked)}
          />
          soundboardEnforcementEnabled
        </label>
        <label className="field">
          <span>autoRoleId</span>
          {roleOptions.length > 0 ? (
            <select
              value={String(form.autoRoleId ?? "")}
              disabled={!canWrite}
              onChange={(e) => set("autoRoleId", e.target.value)}
            >
              <option value="">— не задана —</option>
              {(form.autoRoleId && !roleOptions.some((r) => r.id === String(form.autoRoleId)) ? [{ id: String(form.autoRoleId), name: `${form.autoRoleId} (нет в списке)`, color: 0, position: 0, managed: false, assignable: true }, ...roleOptions] : roleOptions).map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </select>
          ) : (
            <input
              value={String(form.autoRoleId ?? "")}
              disabled={!canWrite}
              onChange={(e) => set("autoRoleId", e.target.value)}
            />
          )}
          <NameHint kind="role" value={form.autoRoleId} />
        </label>
      </Section>
      <Section title="Списки">
        <IdList
          title="Trusted"
          kind="user"
          ids={trustedIds}
          disabled={!canWrite}
          busy={listAction.isPending}
          onAdd={(id) => listAction.mutate({ field: "trusted", userId: id, action: "add" })}
          onRemove={(id) => listAction.mutate({ field: "trusted", userId: id, action: "remove" })}
        />
        <IdList
          title="Auto-unmute"
          kind="user"
          ids={unmuteIds}
          disabled={!canWrite}
          busy={listAction.isPending}
          onAdd={(id) => listAction.mutate({ field: "autoUnmute", userId: id, action: "add" })}
          onRemove={(id) => listAction.mutate({ field: "autoUnmute", userId: id, action: "remove" })}
        />
      </Section>
      {canWrite && (
        <Section title="Stalker-подписки">
          <table>
            <thead>
              <tr>
                <th>Следит</th>
                <th>За кем</th>
                <th>С</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(stalkerList.data?.items ?? []).map((s) => (
                <tr key={s.id}>
                  <td title={s.watcherUserId}>
                    <DName kind="user" id={s.watcherUserId} />
                  </td>
                  <td title={s.targetUserId}>
                    <DName kind="user" id={s.targetUserId} />
                  </td>
                  <td>{s.createdAt?.slice(0, 10) ?? "—"}</td>
                  <td>
                    <button
                      onClick={() =>
                        stalker.mutate({ watcher: s.watcherUserId, target: s.targetUserId, action: "remove" })
                      }
                    >
                      удалить
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <form
            className="toolbar"
            onSubmit={(e) => {
              e.preventDefault();
              stalker.mutate({ watcher: stalkerForm.watcher, target: stalkerForm.target, action: "add" });
            }}
          >
            <input
              placeholder="watcher id"
              value={stalkerForm.watcher}
              onChange={(e) => setStalkerForm((f) => ({ ...f, watcher: e.target.value }))}
            />
            <input
              placeholder="target id"
              value={stalkerForm.target}
              onChange={(e) => setStalkerForm((f) => ({ ...f, target: e.target.value }))}
            />
            <button
              type="submit"
              disabled={
                !/^\d{5,25}$/.test(stalkerForm.watcher) || !/^\d{5,25}$/.test(stalkerForm.target)
              }
            >
              добавить
            </button>
          </form>
        </Section>
      )}
      {canWrite && (
        <div className="toolbar">
          <button
            className="chip active"
            disabled={Object.keys(changed).length === 0 || patch.isPending}
            onClick={() => patch.mutate(changed)}
          >
            {patch.isPending ? "сохранение…" : "Сохранить изменения"}
          </button>
        </div>
      )}
    </>
  );
}
