import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { Button } from "primereact/button";
import { Checkbox } from "primereact/checkbox";
import { Dropdown } from "primereact/dropdown";
import { InputText } from "primereact/inputtext";
import { api, useCanWrite } from "../api/client";
import type { VoiceChannelOption } from "../api/types";
import { ErrorBox, Loading, Section } from "../components/ui";
import { TargetUserPicker } from "../components/userSearch";
import { DName, type NameKind } from "../names";
import { usePicker } from "../names";

// [ключ в activityEventTypes, название, подсказка что бот публикует]
const ACTIVITY_EVENT_TYPES: [string, string, string][] = [
  ["member_join", "Приход на сервер", "карточка, когда участник заходит на сервер"],
  ["member_leave", "Уход с сервера", "карточка, когда участник покидает сервер (ушёл, кик, бан)"],
  ["invite_create", "Создание инвайта", "кто и с какими параметрами создал приглашение"],
  ["invite_delete", "Удаление инвайта", "кто удалил приглашение (или оно истекло)"],
  ["invite_used", "Использование инвайта", "кто вошёл по чьей ссылке"],
  ["message_create", "Новые сообщения", "карточка на каждое новое сообщение"],
  ["message_update", "Редактирование сообщений", "сообщение изменено — старое и новое"],
  ["message_delete", "Удаление сообщений", "кто автор и кто удалил"],
  ["reaction_add", "Реакции: поставили", "кто добавил реакцию"],
  ["reaction_remove", "Реакции: убрали", "кто снял реакцию"],
  ["voice_join", "Заход в голос", "участник зашёл в голосовой канал"],
  ["voice_leave", "Выход из голоса", "участник покинул голосовой канал"],
  ["voice_move", "Перемещение в голосе", "участника перенесли между каналами"],
  ["profile_nickname_update", "Смена ника", "участник изменил ник на сервере"],
  ["profile_roles_update", "Изменение ролей", "кто получил/лишился ролей, с инициатором"],
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
  "autoRestoreRoles",
  "autoRestoreNicknames",
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

type SelectOpt = { label: string; value: string };

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
  const items: SelectOpt[] = [
    ...(allowEmpty !== undefined ? [{ value: "", label: allowEmpty }] : []),
    ...(current !== "" && !known ? [{ value: current, label: `${current} (нет в списке)` }] : []),
    ...options.map((c) => ({ value: c.id, label: c.name })),
  ];
  return (
    <label className="field">
      <span>{label}</span>
      <Dropdown
        value={current}
        options={items}
        optionValue="value"
        disabled={disabled}
        onChange={(e) => onChange(e.value as string)}
      />
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
  const { guildId = "" } = useParams();
  const [input, setInput] = useState("");
  const [manual, setManual] = useState(false);
  const [picked, setPicked] = useState("");
  const select = options && options.length > 0;
  const userSearch = kind === "user" && !select;
  return (
    <div className="idlist">
      <div className="idlist-head">
        <strong>{title}</strong>
        {!disabled &&
          (userSearch ? (
            <TargetUserPicker
              guildId={guildId}
              placeholder="имя пользователя (от 2 символов) или id"
              value={picked}
              onChange={(id) => {
                setPicked(id);
                if (id) onAdd(id);
              }}
            />
          ) : (
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
                <Dropdown
                  value={input}
                  options={[{ value: "", label: "— канал —" }, ...options.map((c) => ({ value: c.id, label: c.name }))]}
                  optionValue="value"
                  aria-label={`${title} канал`}
                  onChange={(e) => setInput(e.value as string)}
                />
                <Button className="linklike" type="button" onClick={() => setManual(true)}>
                  id вручную
                </Button>
              </>
            ) : (
              <>
                <InputText
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="user/channel id"
                  aria-label={`${title} id`}
                />
                {select && (
                  <Button className="linklike" type="button" onClick={() => setManual(false)}>
                    из списка
                  </Button>
                )}
              </>
            )}
            <Button type="submit" disabled={busy || input.trim() === ""}>
              добавить
            </Button>
          </form>
          ))}
      </div>
      <div className="chips">
        {ids.length === 0 && <span className="muted">пусто</span>}
        {ids.map((id) => (
          <span className="chip" key={id} title={id}>
            <DName kind={kind} id={id} />
            {!disabled && (
              <Button
                className="chip-x"
                type="button"
                label="×"
                onClick={() => onRemove(id)}
                disabled={busy}
                aria-label={`удалить ${id}`}
              />
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
  const allRoles = picker.data?.roles ?? [];
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
          <Dropdown
            value={String(form.trackingMode ?? "all")}
            options={[
              { value: "all", label: "all — все голосовые каналы" },
              { value: "none", label: "none — не трековать" },
              { value: "specific", label: "specific — только выбранные каналы" },
            ]}
            optionValue="value"
            disabled={!canWrite}
            onChange={(e) => set("trackingMode", e.value as string)}
          />
        </label>
        <p className="muted tiny">
          <strong>all</strong> — бот пишет голосовые сессии по всем голосовым каналам сервера.{" "}
          <strong>none</strong> — трекинг полностью выключен, сессии не создаются.{" "}
          <strong>specific</strong> — сессии считаются только в каналах из списка «Трекаемые каналы» ниже;
          остальные каналы игнорируются.
        </p>
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
          {ACTIVITY_EVENT_TYPES.map(([key, title, hint]) => (
            <label key={key} className="check" title={hint}>
              <Checkbox
                disabled={!canWrite}
                checked={eventTypes.includes(key)}
                onChange={() =>
                  set(
                    "activityEventTypes",
                    eventTypes.includes(key)
                      ? eventTypes.filter((x) => x !== key)
                      : [...new Set([...eventTypes, key])],
                  )
                }
              />
              <span>
                {title}
                <span className="muted tiny"> — {hint}</span>
              </span>
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
                  <Dropdown
                    value={commandAccess[name] ?? ""}
                    options={[
                      { value: "", label: "как по умолчанию" },
                      { value: "all", label: "все" },
                      { value: "admin", label: "ADMIN ONLY" },
                    ]}
                    optionValue="value"
                    disabled={!canWrite}
                    onChange={(e) => setCommandAccess(name, e.value as string)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      <Section title="Прочее">
        <label className="check">
          <Checkbox
            disabled={!canWrite}
            checked={Boolean(form.soundboardEnforcementEnabled)}
            onChange={() => set("soundboardEnforcementEnabled", !Boolean(form.soundboardEnforcementEnabled))}
          />
          <span>
            Soundboard-модерация
            <span className="muted tiny">
              {" "}
              — если участник включает звук саундборда в курируемом ботом голосовом канале, бот отключит его от
              голоса
            </span>
          </span>
        </label>
        <label className="check">
          <Checkbox
            disabled={!canWrite}
            checked={Boolean(form.autoRestoreRoles)}
            onChange={() => set("autoRestoreRoles", !Boolean(form.autoRestoreRoles))}
          />
          <span>
            Возвращать роли при входе
            <span className="muted tiny">
              {" "}
              — когда участник выходит с сервера, бот запоминает его роли и возвращает их при повторном входе
              (по умолчанию включено)
            </span>
          </span>
        </label>
        <label className="check">
          <Checkbox
            disabled={!canWrite}
            checked={Boolean(form.autoRestoreNicknames)}
            onChange={() => set("autoRestoreNicknames", !Boolean(form.autoRestoreNicknames))}
          />
          <span>
            Возвращать ник при входе
            <span className="muted tiny">
              {" "}
              — то же для никнейма на сервере: сохраняется при выходе и восстанавливается при входе (по умолчанию
              включено)
            </span>
          </span>
        </label>
        <label className="field">
          <span>Autorole</span>
          {(picker.data?.roles?.length ?? 0) > 0 ? (
            <Dropdown
              value={String(form.autoRoleId ?? "")}
              options={[
                { value: "", label: "— не задана —", disabled: false },
                ...(form.autoRoleId && !allRoles.some((r) => r.id === String(form.autoRoleId))
                  ? [{ value: String(form.autoRoleId), label: `${form.autoRoleId} (нет в списке)`, disabled: false }]
                  : []),
                ...allRoles.map((r) => ({
                  value: r.id,
                  label: r.assignable ? r.name : `${r.name} — выше роли бота`,
                  disabled: !r.assignable,
                })),
              ]}
              optionLabel="label"
              optionValue="value"
              optionDisabled="disabled"
              disabled={!canWrite}
              onChange={(e) => set("autoRoleId", e.value as string)}
            />
          ) : (
            <InputText
              value={String(form.autoRoleId ?? "")}
              disabled={!canWrite}
              onChange={(e) => set("autoRoleId", e.target.value)}
            />
          )}
        </label>
        <p className="muted tiny">
          Роль, которую бот выдаёт новичку при входе по чужому приглашению (автороль за реферал).{" "}
          {!canWrite && "Просмотр: для редактирования нужен ADMINISTRATOR."}
        </p>
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
                    <Button
                      severity="danger"
                      onClick={() =>
                        stalker.mutate({ watcher: s.watcherUserId, target: s.targetUserId, action: "remove" })
                      }
                    >
                      удалить
                    </Button>
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
              setStalkerForm({ watcher: "", target: "" });
            }}
          >
            <span className="muted tiny">Следит:</span>
            <TargetUserPicker
              guildId={guildId}
              placeholder="имя (от 2 символов) или id"
              value={stalkerForm.watcher}
              onChange={(id) => setStalkerForm((f) => ({ ...f, watcher: id }))}
            />
            <span className="muted tiny">За кем:</span>
            <TargetUserPicker
              guildId={guildId}
              placeholder="имя (от 2 символов) или id"
              value={stalkerForm.target}
              onChange={(id) => setStalkerForm((f) => ({ ...f, target: id }))}
            />
            <Button
              type="submit"
              disabled={
                !/^\d{5,25}$/.test(stalkerForm.watcher) || !/^\d{5,25}$/.test(stalkerForm.target)
              }
            >
              добавить
            </Button>
          </form>
        </Section>
      )}
      {canWrite && (
        <div className="toolbar">
          <Button
            disabled={Object.keys(changed).length === 0 || patch.isPending}
            onClick={() => patch.mutate(changed)}
          >
            {patch.isPending ? "сохранение…" : "Сохранить изменения"}
          </Button>
        </div>
      )}
    </>
  );
}
