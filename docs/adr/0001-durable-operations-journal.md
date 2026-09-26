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
5. **Исходы транспорта**: «соединение не установлено» → `failed` (эффекта точно не было,
   повтор разрешён); обрыв после отправки/timeout → `unknown` — не ложный failed,
   слепой повтор неидемпотентного kind запрещён (takeover тоже `unknown`).
   Истечение lease ≠ отсутствие эффекта.
6. **Replay**: терминальная операция возвращает сохранённый факт (200/502/504 по state),
   effect не повторяется; новая попытка — новый key. Авторизация текущая (require_guild_admin
   выполняется до чтения журнала), чужая guild → 404.
7. **Audit — проекция**: пишется после финала операции, содержит `operationId`; сбой audit
   помечает `auditError` на документе (успех не маскируется), факт операции устойчив.
8. **Батчи**: `batchId` в заголовке `X-Batch-Id`; дочерние операции — самостоятельные документы,
   `GET /api/guild/{g}/bot/operations?batchId=` даёт child-статусы; «общего success» нет.
9. **Slash-журнал бота**: поле `stage`: `invocation` / `rejected` / `effect`
   (консервативный список `MUTATING_ROUTES`; неизвестный маршрут — не выдаёт эффект).

## Последствия
- Хранение ключей/журнала: `RETENTION_DAYS=30` (TTL-индекс — в T10); volume журнала растёт
  с числом попыток, не с числом повторов.
- UI обязан слать `Idempotency-Key` (повтор доставки — прежний key); без key дедуп
  невозможен, но journal-факт остаётся.
- Индекс выборки `(guildId,batchId)`, `(guildId,createdAt)` — `ensure_web_indexes`.
