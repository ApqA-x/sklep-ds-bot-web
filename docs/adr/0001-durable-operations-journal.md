# ADR-0001: Журнал операций (durable operations) — T08

## Статус
Принят, реализован в `api/operations.py` (web) и `voice_tracker/site_audit.py` (bot, stage-модель).

## Контекст
Baseline-дефект #4: Discord-эффект вызывался до записи в БД — при падении БД/процесса
эффект оставался без следа; повтор запроса мог создать второй эффект (неидемпотентные
message/kick/invite).

## Решение
1. **Каталог.kind'ов** (`operations.KINDS`): каждый kind помнит способ идемпотентности
   (`state` — повтор безопасен / `none` — только с доказательством) и способ сверки
   (`member-roles`, `invite-absent`, `manual` …).
2. **Document** = `{_id, guildId, actorUserId, kind, arguments, requestHash,
   idempotencyKey, batchId, state(requested→executing→succeeded|failed|unknown),
   attempts, leaseOwner, leaseExpiresAt, fenceVersion, result, error, auditError, timestamps}`.
   Секреты (токен) в журнал не пишутся — там только ID/тексты/размеры вложений.
3. **Дедупликация**: `_id = sha256(guildId|actorUserId|kind|idempotencyKey)`. Уникальность
   области ключа обеспечивается самим `_id` — работает и на unit-фейках, и на реальной
   Mongo без вторичного unique-индекса (T10 добавит индексы выборок, не дедуп).
   Один key + другой payload → 409 `idempotency_conflict` (по `requestHash`).
4. **Порядок**: `create_intent` (503 при сбое БД, ноль внешних эффектов) → атомарный
   `claim` (lease 90 s + `fenceVersion`) → Discord-вызов → `finish` (CAS по fence+owner).
5. **Исходы транспорта и HTTP** (R26-03.2): «соединение не установлено» → `failed` (эффекта
   точно не было, повтор разрешён); обрыв после отправки/timeout → `unknown`; ответ Discord
   **4xx** → `failed` (отказ до применения), **5xx** → `unknown` (запрос дошёл до Discord,
   эффект недоказуем) — не ложный failed. Слепой повтор неидемпотентного kind запрещён
   (takeover тоже `unknown`). Истечение lease ≠ отсутствие эффекта: зависший `executing`
   становится наблюдаемым `unknown` через bounded-recovery при чтении статуса (CAS с
   fence+1; чтение внешних эффектов не создаёт). Финал с потерянным fence (finish=False)
   отдаёт 504 `ownership_lost` с фактическим состоянием журнала и не пишет audit — старый
   worker не может объявить чужую попытку успешной; сбой журнала после внешнего эффекта —
   504 `journal_unavailable_after_effect` с operationId.
6. **Replay**: терминальная операция возвращает сохранённый факт (200/502/504 по state),
   effect не повторяется; новая попытка — новый key. `result` содержит детерминированные
   данные (message.id, invite.code/URL): первый успех и replay отдают одинаковый ответ
   (R26-03.4). Авторизация текущая (require_guild_admin выполняется до чтения журнала),
   чужая guild → 404.
7. **Audit — проекция**: пишется после финала операции, содержит `operationId`; сбой audit
   помечает `auditError` на документе (успех не маскируется), факт операции устойчив.
   Аргументы effect/audit читаются из документа журнала — единый источник намерения.
8. **Канонический запрос** (R26-03.3/4): `requestHash` считается по identity — полный
   нормализованный embed и SHA-256 байтов каждого файла (с именем/типом/размером), а не
   по audit-сокращению; абсолютный дедлайн `timeout.set` хранится в аргументах и
   повторяется при retry/takeover (в identity не входит — поздний повтор того же
   намерения не превращается в 409). Ключи: формат `[A-Za-z0-9._:-]`, длина ≤128;
   размер аргументов ≤64 KiB.
9. **Батчи**: `batchId` в заголовке `X-Batch-Id`; дочерние операции — самостоятельные
   документы со стабильным child-ключом `batchId:channelId` (R26-03.7);
   `GET /api/guild/{g}/bot/operations?batchId=` даёт child-статусы и `allSucceeded`
   (успешен, только когда все children терминальны и succeeded).
10. **Slash-журнал бота**: поле `stage`: `invocation` / `rejected` / `effect`
    (консервативный список `MUTATING_ROUTES`; неизвестный маршрут — не выдаёт эффект).
11. **Локальные DB-мутации** (R26-04): настройки, списки trusted/autoUnmute, stalker и
    чат-пресеты пишутся под тем же порядком intent→claim→effect→finish (kind'ы `db.*`);
    применённые `before`/`after` (включая revision CAS-записи) попадают в `result`,
    аудит — идемпотентная проекция с детерминированным `_id = sha256(audit|guildId|operationId)`.
    Сбой проекции остаётся видимым (`auditError`), bounded-recovery наблюдаем через
    `GET /bot/operations/{id}` (повторная проекция той же операции) и
    `reproject_pending_audits` — без повторной мутации.

## Последствия
- Хранение ключей/журнала: `RETENTION_DAYS=90` — одно обещание с migration M2
  (`OPERATIONS_TTL_SECONDS`, манифест схемы); volume журнала растёт с числом попыток,
  не с числом повторов.
- UI обязан слать `Idempotency-Key` (повтор доставки — прежний key); без key дедуп
  невозможен, но journal-факт остаётся. Намерение (batchId+отпечаток payload) фиксируется
  в sessionStorage **до** fetch и переживает только недоказанные исходы; unknown виден
  пользователю отдельным состоянием и сверяется через batch-status (R26-03.6/8). При
  logout намерения очищаются.
- Индекс выборки `(guildId,batchId)`, `(guildId,createdAt)` — `ensure_web_indexes`.
