# sklep-ds-bot-web — веб-часть voice_tracker (чтение + управление)

Независимое приложение: React SPA + FastAPI в одном контейнере.
Полный план и контракты: [`PLAN.md`](PLAN.md) (этапы, API, безопасность).

## Стек

- **Backend:** Python ≥3.11, FastAPI, pymongo (sync), Discord REST через aiohttp
- **Frontend:** React 19 + TypeScript, Vite; далее react-router / @tanstack/react-query / recharts (этап 2)
- **Данные:** MongoDB `voice_tracker` (создаёт только собственные индексы `web_*`)

## Структура

```
api/           FastAPI (main, config; далее auth/queries/mutations/discord_api)
api/tests/     pytest (self-made fake mongo)
ui/            React SPA (Vite)
Dockerfile     multi-stage: npm build → python:3.12-slim (SPA отдаётся FastAPI, same-origin)
docker-compose.yml   локальный dev (mongo + web)
.github/workflows/publish.yml  образ ghcr.io/apqa-x/sklep-ds-bot-web (amd64+arm64)
```

## Локальный запуск

```bash
cp .env.example .env            # при необходимости поправить
docker compose up -d --build    # web на http://127.0.0.1:8000
curl http://127.0.0.1:8000/api/healthz
```

Если локальный mongod уже занимает 27017 (Windows-сервис), use `--no-deps`:

```bash
MONGO_URI=mongodb://host.docker.internal:27017 docker compose up -d --no-deps --build web
```

Без Docker (dev):

```bash
# API
python -m pip install -r api/requirements.txt
uvicorn api.main:app --reload --port 8000
# UI (отдельный терминал; vite проксирует /api на :8000)
cd ui && npm ci && npm run dev
```

## Картинки чата (`/media`)

Web читает external volume `dsbot-media`, куда бот (dsbot-gateway, rw) скачивает
новые картинки-вложения ≤20 МБ в `<guildId>/<YYYY-MM>/<sha256>.<ext>`; метаданные —
в `chat_messages.attachments`. Один раз на хосте до `up`:

```bash
docker volume create dsbot-media
```

`MEDIA_DIR` пусто — раздача `/media` выключена, UI показывает ссылки Discord.
Отдельный Nginx перед `/media` не обязателен: пути содержат sha256 содержимого
и не угадываются извне.

## Тесты

```bash
python -m pip install -r api/requirements.txt pytest httpx
python -m pytest api/tests -q
```

На этой машине (WSL): `/mnt/c/Users/ApqA/AppData/Local/Programs/Python/Python312/python.exe -m pytest api/tests -q`

## Переменные окружения

| Переменная | Обязательная | Значение |
|---|---|---|
| `MONGO_URI` / `MONGO_DB` | да (иначе degraded) | подключение к `voice_tracker` |
| `WEB_PORT` | нет | по умолчанию 8000 |
| `DISCORD_TOKEN` | этап 4-5 | список гильдий бота + bot-действия |
| `DISCORD_APPLICATION_ID` | этап 7 | |
| `DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` / `DISCORD_REDIRECT_URI` | этап 3 | Discord OAuth2 (identify, guilds) |
| `WEB_SESSION_SECRET` | этап 3 | подпись session-cookie; без него auth-роуты и SessionMiddleware выключены |
| `WEB_PUBLIC_URL` | этап 7 | публичный https-адрес сайта |

Значение есть → функция включена; пусто → безопасно выключена (healthz остаётся открытым, write/bot-роуты появятся с этапами 4-5 и будут требовать авторизацию).

## Публикация образа

```bash
git tag v0.1.0 && "/mnt/c/Program Files/Git/cmd/git.exe" push origin v0.1.0
```

workflow `publish.yml` соберёт `ghcr.io/apqa-x/sklep-ds-bot-web:0.1.0` (+ `:latest`).
В проде сервис `web` стека `bot/docker-stack.yaml` пинить тегом `v*`, не `:latest`.

## Статус этапов

| Этап | Статус |
|---|---|
| 0. Каркас | ✅ |
| 1. Read API | ✅ (`api/queries.py`, `api/read.py`, web-индексы `web_*` при старте) |
| 2. SPA v1 | ✅ (роутинг, Leaderboard/Active/Sessions/User/Invites, recharts) |
| 3. OAuth + права | ✅ (`api/auth.py`, `api/discord_api.py`, read-гейт Manage Guild, dev-mode без env) |
| 4. Write-слой | ✅ (`api/models.py`, `api/mutations.py`, `api/write.py`, `web_audit_logs`, конфликт `expectedUpdatedAt`) |
| 5. Discord-действия | ✅ (`api/bot.py`: роли/timeout/move/kick/сообщение (текст + multipart-вложения)/инвайты, rate-limit, audit) |
| 6. UI управления | ✅ (форма настроек, списки trusted/autoUnmute/stalker, панель действий, страница аудита) |
| 6b. UI-правки 2026-09-22 | ✅ (Catppuccin Mocha; имена вместо id через `GET /names`; `origin` web/discord в аудите + фильтр; экран «Чат» — см. ниже) |
| 6c. Аудит-правки + фото 2026-09-22 | ✅ (`a4918d8`, `01756cc`: читаемые «Детали» аудита, фильтры аудита (над кем/действие/статус/период/сортировка) и чата (канал/автор/тип/период/сортировка), пагинация лидерборда по 50 + клик по графику → профиль, подсказки команд и сетка каналов в настройках, хранение картинок чата — см. «Картинки чата» выше) |
| 6d. Чат: мультивыбор каналов + отправка от бота 2026-09-23 | ✅ (фильтр ленты — выпадающий список с чекбоксами по историчным каналам, `channelId` — повторяемый параметр ≤100; панель «Отправить от бота»: текст ≤2000 + вложения ≤10 файлов/25 МБ (multipart), живые текстовые каналы из `/picker`, отправка в N каналов последовательно с двойным подтверждением) |
| 6e. Аудит-Discord + починка UI/данных 2026-09-23 | ✅ (тумблер «Журнал сайта / Журнал Discord» на экране аудита + `GET /audit/discord` (кик/бан/роли/сообщения…, последние ≤100 записей, читаемые ru-названия действий); починен выпадающий список автороли в «Настройках → Прочее» (роли выше бота — disabled, подсказка вынесена из `.field`); контрастнее подписи осей на графиках; причина мало-сообщений в профилях — ограниченный разовый импорт истории, дозагрузка — `scripts/backfill_chat_history.py`) |
| 6f. Аудит-Discord в БД + карточка пользователя 2026-09-23 | ✅ (Discord-аудит хранится в `discord_audit_logs`: фоновая синхронизация раз в минуту (`api/audit_sync.py`, дозаррузка ≤45 дней истории, идемпотентный upsert по `entryId`), фильтры действия/актора/цели/периода + пагинация, читаемые diff-ролей «выдано/снято»; кликабельные ники → профиль (`components/userLink.tsx`); экран профиля: аватар/банер/акцент из `GET /users/{id}/card` + копилка прошлых аватаров `user_avatar_history`, переключатель графиков часы/сообщения/инвайты; чёрный текст в тултипах графиков; «Настройки → Прочее»: чекбоксы автовозврата ролей/ника (`autoRestoreRoles`/`autoRestoreNicknames`), гейт в `dsbot services/gateway.py` (потребуется рестарт `dsbot-gateway`)) |
| 7. Прод | ⬜ требуется на хосте: OAuth env, reverse proxy, `docker stack deploy` (чеклист ниже) |

> **История чата**: экран и read-API (`GET /chat/channels`, `GET /chat`) готовы и читают коллекцию
> `chat_messages` (в базе бота). Writer добавлен в живой `dsbot-gateway` (`D:\dsbot`:
> `voice_tracker/repository.py` + `services/gateway.py`, на момент 2026-09-22 — не закоммичен в
> тот репозиторий): обычные сообщения, правки и удаления пишутся с фильтром по гильдиям из конфига;
> новые картинки-вложения дополнительно скачиваются в `dsbot-media` (см. «Картинки чата»).

## Чеклист прода-выката (этап 7, выполняется на Swarm-хосте)

1. Discord Developer Portal: у приложения включить OAuth2, redirect URI `https://<домен>/api/auth/callback`, скопировать client id/secret.
2. В `bot/.env` добавить: `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI`, `WEB_SESSION_SECRET` (32+ случайных байт, например `openssl rand -hex 32`), `WEB_PUBLIC_URL`.
3. Запушить тег образа и заменить `:latest` на `v*` в `bot/docker-stack.yaml`: `image: ghcr.io/apqa-x/sklep-ds-bot-web:v0.1.0`.
4. Картинки чата (опционально): на хосте `docker volume create dsbot-media`; в стеке — `MEDIA_DIR=/data/media` + монтирование тома gateway (rw) и web (ro). Без этого `/media` выключен, UI отдаёт ссылки Discord.
5. Reverse proxy (Caddy/nginx/Traefik): TLS для `<домен>` → `web:8000` (сеть оверлея). Наружу публикация порта **не** нужна. Для вложений в «Отправить от бота» (до 25 МБ на файл, до 50 МБ на запрос) поднять лимит тела: nginx — `client_max_body_size 52m;`, Caddy — `request_body { max_size 52428800 }`.
6. `docker stack deploy -c docker-stack.yaml bot`.
7. Проверка: `curl https://<домен>/api/healthz` → `status: ok`; вход через Discord; сервер виден только если пользователь в нём и имеет Manage Guild.

Пока `DISCORD_CLIENT_*`/`WEB_SESSION_SECRET` не заданы, сайт работает в dev-режиме (без авторизации) — в проде не публиковать порт наружу до шага 2.
