import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "primereact/button";
import { InputNumber } from "primereact/inputnumber";
import { api } from "../api/client";
import { fmtDate } from "../lib/format";
import { DName } from "../names";
import { ErrorBox, Section } from "./ui";
import { TargetUserPicker } from "./userSearch";

const STATUS_LABELS: Record<string, string> = {
  none: "таймер не задан",
  pending: "ожидает срока",
  executing: "исполняется",
  disconnected: "пользователь отключён",
  skipped: "отключение не потребовалось",
  cancelled: "отменён",
  unknown: "результат не подтверждён",
  failed: "не удалось отключить",
};

export function SleepTimerPanel({ guildId, initialUserId }: { guildId: string; initialUserId: string }) {
  const [targetUserId, setTargetUserId] = useState(initialUserId);
  const [hours, setHours] = useState(2);
  const [notice, setNotice] = useState("");
  const queryClient = useQueryClient();

  useEffect(() => {
    setTargetUserId(initialUserId);
    setNotice("");
  }, [guildId, initialUserId]);

  const timer = useQuery({
    queryKey: ["sleepTimer", guildId, targetUserId],
    queryFn: () => api.sleepTimer(guildId, targetUserId),
    enabled: Boolean(guildId && targetUserId),
  });
  const set = useMutation({
    mutationFn: ({ userId, delayHours }: { userId: string; delayHours: number }) => api.sleepSet(guildId, userId, delayHours),
    onSuccess: (_data, vars) => {
      setNotice("Таймер сохранён.");
      queryClient.invalidateQueries({ queryKey: ["sleepTimer", guildId, vars.userId] });
    },
    onError: (error: Error) => setNotice(error.message),
  });
  const cancel = useMutation({
    mutationFn: (userId: string) => api.sleepCancel(guildId, userId),
    onSuccess: (data, userId) => {
      setNotice(data.hadActiveTimer ? "Таймер отменён." : "Активного таймера не было.");
      queryClient.invalidateQueries({ queryKey: ["sleepTimer", guildId, userId] });
    },
    onError: (error: Error) => setNotice(error.message),
  });
  const busy = set.isPending || cancel.isPending;
  const current = timer.data;

  return (
    <Section title="Автоотключение от голосового канала">
      <p className="muted tiny">
        Выбери участника по нику или Discord ID. Срок считается от текущего времени;
        если к сроку человек вышел из войса и вернулся позднее, старый таймер его не отключит.
      </p>
      <div className="action-row">
        <TargetUserPicker
          guildId={guildId}
          placeholder="Ник или Discord ID"
          value={targetUserId}
          onChange={(id) => { setTargetUserId(id); setNotice(""); }}
        />
        {targetUserId && <span className="muted tiny"><DName kind="user" id={targetUserId} /> · {targetUserId}</span>}
      </div>
      {targetUserId && (
        <>
          {timer.isError ? <ErrorBox error={timer.error} /> : (
            <p className="muted">
              Статус: {timer.isLoading ? "загрузка…" : STATUS_LABELS[current?.status ?? "none"] ?? current?.status}
              {current?.status === "pending" && current.dueAt ? <> · отключить {fmtDate(current.dueAt)}</> : null}
              {current?.resultAt ? <> · итог {fmtDate(current.resultAt)}</> : null}
            </p>
          )}
          <div className="action-row">
            <label htmlFor="sleep-hours">Через часов</label>
            <InputNumber
              inputId="sleep-hours"
              value={hours}
              min={1}
              max={24}
              useGrouping={false}
              onValueChange={(event) => setHours(event.value ?? 2)}
              disabled={busy}
            />
            <Button disabled={busy || !Number.isInteger(hours) || hours < 1 || hours > 24} onClick={() => set.mutate({ userId: targetUserId, delayHours: hours })}>
              Поставить или заменить
            </Button>
            <Button severity="secondary" disabled={busy || current?.status !== "pending"} onClick={() => cancel.mutate(targetUserId)}>
              Отменить
            </Button>
          </div>
        </>
      )}
      {notice && <p className="hint" role="status">{notice}</p>}
    </Section>
  );
}
