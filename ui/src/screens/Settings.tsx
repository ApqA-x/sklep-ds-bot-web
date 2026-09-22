import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { api, useCanWrite } from "../api/client";
import { ErrorBox, Loading, Section } from "../components/ui";

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

const EDITABLE_KEYS = [
  "trackingMode",
  "trackedChannelIds",
  "summaryChannelId",
  "fallbackSummaryChannelId",
  "autoRoleId",
  "soundboardEnforcementEnabled",
  "activityChannelId",
  "activityEventTypes",
] as const;

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function normalize(value: unknown): unknown {
  if (value === undefined || value === null) return "";
  return value;
}

function IdList({
  title,
  ids,
  disabled,
  onAdd,
  onRemove,
  busy,
}: {
  title: string;
  ids: string[];
  disabled: boolean;
  onAdd: (id: string) => void;
  onRemove: (id: string) => void;
  busy: boolean;
}) {
  const [input, setInput] = useState("");
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
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="user/channel id"
              aria-label={`${title} id`}
            />
            <button type="submit" disabled={busy}>
              добавить
            </button>
          </form>
        )}
      </div>
      <div className="chips">
        {ids.length === 0 && <span className="muted">пусто</span>}
        {ids.map((id) => (
          <span className="chip" key={id}>
            {id}
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
            ids={trackedChannels}
            disabled={!canWrite}
            busy={patch.isPending}
            onAdd={(id) => set("trackedChannelIds", [...new Set([...trackedChannels, id])])}
            onRemove={(id) => set("trackedChannelIds", trackedChannels.filter((x) => x !== id))}
          />
        )}
        <label className="field">
          <span>summaryChannelId</span>
          <input
            value={String(form.summaryChannelId ?? "")}
            disabled={!canWrite}
            onChange={(e) => set("summaryChannelId", e.target.value)}
          />
        </label>
        <label className="field">
          <span>fallbackSummaryChannelId</span>
          <input
            value={String(form.fallbackSummaryChannelId ?? "")}
            disabled={!canWrite}
            onChange={(e) => set("fallbackSummaryChannelId", e.target.value)}
          />
        </label>
      </Section>
      <Section title="Activity-карточки">
        <label className="field">
          <span>activityChannelId</span>
          <input
            value={String(form.activityChannelId ?? "")}
            disabled={!canWrite}
            onChange={(e) => set("activityChannelId", e.target.value)}
          />
        </label>
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
          <input
            value={String(form.autoRoleId ?? "")}
            disabled={!canWrite}
            onChange={(e) => set("autoRoleId", e.target.value)}
          />
        </label>
      </Section>
      <Section title="Списки">
        <IdList
          title="Trusted"
          ids={trustedIds}
          disabled={!canWrite}
          busy={listAction.isPending}
          onAdd={(id) => listAction.mutate({ field: "trusted", userId: id, action: "add" })}
          onRemove={(id) => listAction.mutate({ field: "trusted", userId: id, action: "remove" })}
        />
        <IdList
          title="Auto-unmute"
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
                  <td>{s.watcherUserId}</td>
                  <td>{s.targetUserId}</td>
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
