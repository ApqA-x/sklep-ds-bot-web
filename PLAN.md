# План: независимая веб-часть для voice_tracker (чтение + управление)

> Дата: 2026-09-22. Репозиторий: `sklep-ds-bot-web` (новый, пока пустой).
> Родственный проект: `sklep-ds-bot` (Discord voice-трекер, Python, 6 микросервисов + MongoDB 7 + NATS).
> Предыстория: `docs/website-plan.md` в sklep-ds-bot (этапы 0–6 реализованы в 09/2026 в read-only варианте).
> ⚠️ Код того дашборда **утерян**: репозиторий `ApqA-x/sklep-bot-web` (переименован в `ApqA-x/sklep-ds-bot-web`) на GitHub пуст (push коммита `aff0211` не состоялся), локальных копий нет.
> Настоящий план восстанавливает его контракты из `website-plan.md` §4/§10/§11 и расширяет область: сайт получает **запись в БД и управление ботом**.

---

## 1. Цель и объём

Одно независимое веб-приложение (монообраз: React SPA + JSON API в одном контейнере), которое:

1. **Читает** MongoDB `voice_tracker` — дашборды: лидерборд, сессии, профили, инвайты (как старый read-only план).
2. **Пишет** в БД — редактирование `guild_settings` (режим трекинга, каналы, autorole, trusted/unmute списки, activity-настройки) и `stalker_subscriptions`.
3. **Управляет ботом через Discord API** — точечные действия от имени бота: выдать/снять роль, мут/анмут, переместить/кикнуть участника, отправить сообщение в канал, пересоздать/удалить инвайт.

Не входит в объём: перезапуск сервисов бота (доступ к Swarm/хосту), произвольные slash-команды, управление токенами.

### Мульти-guild
Селектор сервера: **гильдии, в которых присутствует бот** (backend получает их бот-токеном через `GET /users/@me/guilds` — это «бот-глаз», видит все серверы бота независимо от того, состоит в них ли пользователь; кэш 60 с). Пользователю из этого списка показываются только те гильдии, где у него есть права: читать — при `MANAGE_GUILD`, писать/действовать — при `ADMINISTRATOR` (решение 2026-09-22). Все эндпоинты принимают `guildId` и проверяют права пользователя **именно в этой гильдии** (двойная проверка, §3.3). Вайтлист `WEB_GUILD_IDS` не вводим.

---

## 2. Выбор backend'а — РЕШЕНО: FastAPI (2026-09-22)

| Критерий | **A. FastAPI (Python)** ✅ рекомендация | B. NestJS/Express (Node+TS) |
|---|---|---|
| Контракты данных | `voice_tracker/domain.py` + `repository.py` — готовые dataclass-контракты и слой чтения; портятся копией в `api/models.py` (Pydantic) и остаются синхронными по стилю | контракты пришлось бы писать заново на TS/Zod; единый язык с фронтом |
| Готовность | старый дашборд (этапы 0–6) был именно FastAPI + pymongo; тесты/CI-шаблоны (`web-tests.yml`, `publish.yml`) описаны под него | весь бэкенд с нуля, шаблоны CI не переиспользуются |
| Запись в Discord API | `aiohttp` уже в зависимостях проекта; Discord REST — обычный HTTP | нативный fetch/axios |
| Развёртывание | один контейнер: FastAPI отдаёт `/api/*` + статику SPA (same-origin, без CORS) — проверено в smoke-тесте 2026-09-22 | два процесса или nginx-сайдкар; либо бандлинг статки в образ Node |
| Риски | синхронный pymongo в threadpool — приемлемо при нагрузке дашборда | отрыв от экосистемы бота: две правды о схеме БД |

**Решение: A — FastAPI.** Схема БД принадлежит `sklep-ds-bot`, и единственный её актуальный источник — `domain.py`; API-контракты (`api/models.py`, Pydantic) копируются из него и потому не могут «разойтись» с реальностью, как это произошло бы при отдельной реализации на Node. Раздел сравнения оставлен как справка.

---

## 3. Архитектура

```
Browser (React SPA)
   │  HTTPS (reverse proxy + TLS на хосте)
   ▼
web-контейнер (один образ, порт 8000)
 ├─ SPA статика (Vite build)
 ├─ /api/auth/*   Discord OAuth2 (identify, guilds) → сессия (WEB_SESSION_SECRET)
 ├─ /api/read/*   MongoDB read (aggregation, кэш 60 c)
 ├─ /api/write/*  MongoDB write (только allowlist полей + audit-log)
 └─ /api/bot/*    Discord REST от имени бота (DISCORD_TOKEN, server-side только)
        │                    │                 │
        ▼                    ▼                 ▼
     mongo:27017      mongo:27017      discord.com/api/v10
```

Микросервисы бота **не трогаем**: они читают `guild_settings` из живой БД на каждое действие, поэтому правки сайта подхватываются без участия бота. NATS-командный контур (подпись HMAC, новый consumer в gateway) — сознательно **не** делаем в v1: он нужен только для действий, требующих внутреннего состояния gateway; для v1 достаточно Discord REST + БД. (Задел — в §8, этап 8.)

### 3.1 Путь записи в БД — правила
- **Allowlist**: редактируются только поля `guild_settings.trackingMode, trackedChannelIds, summaryChannelId, fallbackSummaryChannelId, autoRoleId, autoUnmuteUserIds, trustedUserIds, soundboardEnforcementEnabled, activityChannelId, activityCategoryChannelIds, activityEventTypes` + `stalker_subscriptions` (добавить/удалить подписку). Всё остальное (`managedVoiceChannelId`, invite-флаги, `*Snapshot*`) — только чтение: их пишет бот.
- **Валидация по контракту `domain.GuildSettings`**: snowflake-строки, enum'ы, длина списков. Записываем `$set` только указанных полей + `updatedAt` — никогда не заменяем документ целиком.
- **Идемпотентность/гонки**: перед `$set` сверяем ожидаемый `updatedAt` клиента (если передан) — конфликт → 409, UI перезагружает форму (защита от last-write-wins против гонок с ботом).
- **Audit-log**: каждая запись — новый документ в `web_audit_logs` (`{action, guildId, actorUserId, actorName, before, after, at, source:"web"}`); коллекция создаётся web-сервисом, бот её не читает. Индекс `{guildId:1, at:-1}`.
- Веб создаёт **только свои** индексы с префиксом `web_*` / `web_audit_*` при старте — к индексам бота не прикасается (как в старом плане).

### 3.2 Путь действий в Discord — правила
- Все действия — **белый список операций** (см. контракт `/api/bot/*`), никаких обобщённых «вызови любой эндпоинт».
- Права проверяются **дважды**: (1) сессия пользователя + его permissions в гильдии (из OAuth-ответа / `GET /users/@me/guilds`), (2) серверный пересмотр через Discord API перед mutating-действием (кэш прав ≤60 с) — права могли измениться после логина.
- Бот-токен (`DISCORD_TOKEN`) живёт только в env контейнера; в ответе API не появляется.
- Rate-limit Discord: ответы 429 пробрасываем как 502 с `Retry-After`; локально — простой tocken-bucket на гильдию.
- Каждое действие пишет `web_audit_logs` (без before/after, но с аргументами).

### 3.3 Авторизация и роли доступа
- Логин: Discord OAuth2, scope `identify guilds`. Сессия — signed cookie (`WEB_SESSION_SECRET`), same-origin.
- **Read** доступ к гильдии: `MANAGE_GUILD` (bit 5), как в старом плане.
- **Write/бот-действия**: `ADMINISTRATOR` (bit 3) — совпадает с `default_member_permissions` админ-команд бота (`voice_tracker/discord_models.py`). Для `/api/bot/kick` дополнительно требовать `KICK_MEMBERS`.
- Пользователь без прав на гильдию → гильдия не видна в селекторе; прямой запрос → 403.
- Исключений доступа по `trustedUserIds` для сайта не делается (решение 2026-09-22).

---

## 4. API-контракт

### 4.1 Read (восстановлен из website-plan §4, без изменений + `guildId`)
```
GET  /api/healthz                                   # liveness + ping mongo
GET  /api/auth/whoami                               # профиль Discord + доступные гильдии + права
GET  /api/guilds                                    # гильдии бота ∩ права пользователя (read/write флаги на каждую)
GET  /api/guild/{guildId}/leaderboard?period=7d|30d|all&limit=50
GET  /api/guild/{guildId}/sessions/active
GET  /api/guild/{guildId}/sessions?status=closed&page=1&size=25
GET  /api/guild/{guildId}/sessions/{id}             # сессия + участники
GET  /api/guild/{guildId}/users/{userId}?period=30d # профиль: totals, роли, ники, атрибуция
GET  /api/guild/{guildId}/invites?period=30d        # атрибуция + сводка по инвайтерам
GET  /api/guild/{guildId}/settings                  # текущие настройки (+ только-read поля)
GET  /api/guild/{guildId}/members?q=...             # поиск участников (для форм: trusted/unmute)
```
Правила слоя чтения — как в old-плане §3: агрегации по индексам, никаких полных сканов `voice_session_participants` (запрет `list_voice_totals_by_guild()` в HTTP), кэш тяжёлых агрегаций 60 с, defensive-чтение удалённых ссылок (snowflake → `https://discord.com/users/{id}`).

### 4.2 Write (новый)
```
PATCH /api/guild/{guildId}/settings                 # body: allowlist-поля + expectedUpdatedAt?
                                                    # 200 → полный свежий документ; 409 → конфликт
POST  /api/guild/{guildId}/trusted                  # {userId, action: "add"|"remove"}
POST  /api/guild/{guildId}/autoUnmute               # {userId, action: "add"|"remove"}
POST  /api/guild/{guildId}/stalker                  # {watcherUserId, targetUserId, action: "add"|"remove"}
GET   /api/guild/{guildId}/audit?page=1&size=50     # последние изменения (admin)
```
PATCH валидируется Pydantic-моделью `GuildSettingsPatch` (копия полей `domain.GuildSettings`, camelCase wire-format).

### 4.3 Bot-действия (новый)
```
POST /api/guild/{guildId}/bot/member/{userId}/roles      # {roleId, action: "grant"|"revoke"}
POST /api/guild/{guildId}/bot/member/{userId}/timeout    # {mute: true|false}   (MODERATE/ADMIN)
POST /api/guild/{guildId}/bot/member/{userId}/move       # {channelId}          (переместить в voice)
POST /api/guild/{guildId}/bot/member/{userId}/kick       # {reason}             (KICK_MEMBERS)
POST /api/guild/{guildId}/bot/channel/{channelId}/message# {content}            (≤2000 симв.)
POST /api/guild/{guildId}/bot/invite                     # {channelId} → создать инвайт
DELETE /api/guild/{guildId}/bot/invite/{code}
```
Все — синхронный вызов Discord REST с бот-токеном; ответы нормализуем к `{ok, discordStatus, detail?}`. Для `kick` дополнительно требовать `KICK_MEMBERS` у пользователя (помимо `ADMINISTRATOR`, §3.3). Набор действий подтверждён 2026-09-22, изменений не требуется.

---

## 5. Frontend (React)

- Стек как решено ранее: **Vite + TS + react-router + @tanstack/react-query + recharts**.
- Экраны: Leaderboard · Active Sessions (polling 5–10 с) · Sessions история · Session detail · User profile · Invites · **Settings (read + edit)** · **Audit log**.
- Settings-экран = формы по allowlist-полям; кнопки Discord-действий в UI участника (роль/мут/переместить) — с confirm-диалогом.
- Общие типы: `ui/src/api/schema.ts` вручную синхронизируется с Pydantic-моделями (в v1 без генерации; опция — OpenAPI→TS на этапе 7).
- Права в UI: `whoami.permissions` скрывает write/bot-кнопки у read-only пользователей (это UX, безопасность — на сервере).

---

## 6. Структура репозитория

```
sklep-ds-bot-web/
├── api/                 # FastAPI
│   ├── main.py          # app, SessionMiddleware, SPA catch-all (response_model=None)
│   ├── config.py        # env: MONGO_URI/DB, DISCORD_*, WEB_*
│   ├── auth.py          # OAuth2 + permission gate
│   ├── models.py        # Pydantic: read-модели + GuildSettingsPatch
│   ├── queries.py       # read-агрегации
│   ├── mutations.py     # allowlist-записи + audit
│   ├── discord_api.py   # REST-обёртка над api/v10 (бот-токен)
│   ├── dockerize/…      # (не обязателен)
│   └── tests/           # pytest, self-made fake mongo по образцу sklep-ds-bot/tests
├── ui/                  # React SPA (Vite)
├── Dockerfile           # multi-stage: npm build → python:3.12-slim + static
├── docker-compose.yml   # локально: mongo + api
├── .github/workflows/publish.yml   # образ ghcr.io/apqa-x/sklep-ds-bot-web (pin v*)
└── README.md            # запуск, ENV
```

ENV (дополнить `bot/.env` на хосте): `MONGO_URI, MONGO_DB, DISCORD_TOKEN, DISCORD_APPLICATION_ID, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI, WEB_SESSION_SECRET, WEB_PUBLIC_URL`. `WEB_GUILD_IDS` не используется — список гильдий берётся у Discord (см. §1).

Известные грабли smoke-теста 2026-09-22 (повторить фиксы сразу): импорт `SessionMiddleware` из `starlette.middleware.sessions`; `response_model=None` на SPA catch-all; `.dockerignore` для `ui/node_modules|dist`; `npm ci` с лок-файлом.

---

## 7. Безопасность (обязательные инварианты)

1. Сайт не публичный: до закрытия OAuth от `.env` значением наружу не публикуем (как и советует комментарий в `bot/docker-stack.yaml`).
2. Ни один write/bot-эндпоинт не принимает «сырой» патч БД или URL/эндпоинт Discord — только типизированные allowlist-операции.
3. Бот-токен, client secret, session secret — только env; в логи не попадают; ответы API их не содержат.
4. Все Discord-модифицирующие действия идут от имени бота с серверной проверкой прав администратора-пользователя (двойная проверка, §3.3).
5. Audit-log пишется до выполнения действия (on-failure с пометкой `failed`), read через `/audit` только ADMINISTRATOR.
6. Rate-limit на `/api/bot/*`: на пользователя и на гильдию (защита от кликов/багов UI против лимитов Discord).

---

## 8. Этапы

| # | Этап | Содержание | Выход |
|---|---|---|---|
| 0 | Каркас | репо, FastAPI-приложение, config, healthz, Dockerfile, compose, CI publish | контейнер жив, `/api/healthz` 200 |
| 1 | Read API | §4.1 + тесты (fake mongo) | read-слой с контрактом старого дашборда |
| 2 | SPA v1 | маршруты, Leaderboard/Active/Sessions/User/Invites, whoami-шлюз | дашборд под OAuth |
| 3 | OAuth+gate | Discord OAuth2, read=Manage Guild, write=ADMINISTRATOR | приватный доступ |
| 4 | **Write-слой** | §4.2: PATCH settings, trusted/unmute/stalker списки, `$set`+conflict, `web_audit_logs` + тесты | безопасная запись |
| 5 | **Discord-действия** | §4.3: роли/мут/переместить/кикнуть/сообщение/инвайты + rate-limit + audit | управление ботом |
| 6 | UI управления | Settings-формы, панели участника, страница Audit | полный цикл из браузера |
| 7 | Прод | env на хосте, reverse proxy+TLS, `docker stack deploy`, пин `v*` образа в стеке | доступно снаружи |
| 8 | Опц. | SSE из NATS вместо polling; OpenAPI→TS-генерация типов; NATS control-подписанные события для действий, требующих состояния gateway | v2 |

Порядок 4→5→6 осознанный: запись в БД даёт основной эффект (управление поведением бота), Discord-действия — надстройка.

---

## 9. Риски

- **Гонки «бот vs сайт» за `guild_settings`**: бот пишет те же документы (например `/connect` меняет `managedVoiceChannelId`). Митигация: `$set` allowlist-полей + `updatedAt`-контент-чек; никогда не заменяем документ.
- **Права устарели между запросами**: серверная re-check перед bot-действиями (§3.3).
- **DISCORD_TOKEN в web-контейнере** — новая поверхность компрометации: контейнер без входящих портов кроме proxy, только allowlist-вызовы, audit.
- **Нагрузка на Mongo** — прежние правила (§4.1).
- **Ответственность за деструктивные действия** (kick/timeout): confirm в UI + причина обязательна + audit; при необходимости сузить вайтлистом действий.
- **Потеря кода** (уже случилась): с самого начала коммитим в `ApqA-x/sklep-ds-bot-web` после каждого этапа; push — Windows-git (GCM), не WSL.

---

## 10. Решения — ЗАФИКСИРОВАНО (2026-09-22)

- [x] Backend: **FastAPI** (Python) — контракты копируются из `domain.py` (см. §2).
- [x] Write-доступ и bot-действия: **только `ADMINISTRATOR`**; исключений по `trustedUserIds` нет; для kick — доп. проверка `KICK_MEMBERS`.
- [x] Bot-действия v1: **полный набор** §4.3 (роли, timeout, move, kick, сообщение, инвайты) — без сокращений.
- [x] Гильдии: **все, где присутствует бот** (список бот-токеном), с фильтром по правам пользователя; вайтлист `WEB_GUILD_IDS` не вводим.

Свободных вопросов нет — можно стартовать с этапа 0.

---

## 11. UI-требования 2026-09-22 (реализовано)

- **Catppuccin Mocha** — токены палитры в `ui/src/styles.css`; цвета графиков mauve/green.
- **Имена вместо snowflake** — `GET /api/guild/{g}/names` → `{guildName, channels, roles, users}`:
  каналы/роли/имя гильдии через бот-токен (кэш 300 с), пользователи из БД
  (`voice_session_participants` за 90 дней + overrides из `member_nickname_state`).
  UI: `useNames`/`DName` на всех экранах; без `DISCORD_TOKEN` каналы/роли деградируют до id.
- **Источник действия в аудите** — поле `origin` в `web_audit_logs`: `web` (правка через сайт)
  / `discord` (bot-действия через Discord API). `GET /audit?origin=`, бейджи и фильтр на странице Аудит.
  Исторически записей с origin нет до этого релиза — старые документы отображаются как `web`.
- **История чата** — просмотрщик реализован (`GET /chat/channels`, `GET /chat?channelId=&before=&limit=`,
  экран «Чат») поверх коллекции `voice_tracker.chat_messages`
  (`{guildId, channelId, messageId, authorUserId, authorName, content, sentAt, editedAt?, deletedAt?}`,
  индекс `web_chat_guildId_channelId_sentAt`). Writer добавлен в живой `dsbot-gateway`
  (`D:\dsbot`: `voice_tracker/repository.py` + `services/gateway.py`; на 2026-09-22 — не закоммичен
  в том репозитории): create/edit/delete сообщений пишутся с фильтром по гильдиям из конфига.
