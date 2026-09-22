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
| 5. Discord-действия | ✅ (`api/bot.py`: роли/timeout/move/kick/сообщение/инвайты, rate-limit, audit) |
| 6. UI управления | ✅ (форма настроек, списки trusted/autoUnmute/stalker, панель действий, страница аудита) |
| 6b. UI-правки 2026-09-22 | ✅ (Catppuccin Mocha; имена вместо id через `GET /names`; `origin` web/discord в аудите + фильтр; экран «Чат» — см. ниже) |
| 7. Прод | ⬜ требуется на хосте: OAuth env, reverse proxy, `docker stack deploy` (чеклист ниже) |

> **История чата**: экран и read-API (`GET /chat/channels`, `GET /chat`) готовы и читают коллекцию
> `chat_messages` (в базе бота). Writer добавлен в живой `dsbot-gateway` (`D:\dsbot`:
> `voice_tracker/repository.py` + `services/gateway.py`, на момент 2026-09-22 — не закоммичен в
> тот репозиторий): обычные сообщения, правки и удаления пишутся с фильтром по гильдиям из конфига.
> До его перезапуска сообщения не сохранялись, поэтому страница «Чат» пустая (в UI есть заглушка).

## Чеклист прода-выката (этап 7, выполняется на Swarm-хосте)

1. Discord Developer Portal: у приложения включить OAuth2, redirect URI `https://<домен>/api/auth/callback`, скопировать client id/secret.
2. В `bot/.env` добавить: `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `DISCORD_REDIRECT_URI`, `WEB_SESSION_SECRET` (32+ случайных байт, например `openssl rand -hex 32`), `WEB_PUBLIC_URL`.
3. Запушить тег образа и заменить `:latest` на `v*` в `bot/docker-stack.yaml`: `image: ghcr.io/apqa-x/sklep-ds-bot-web:v0.1.0`.
4. Reverse proxy (Caddy/nginx/Traefik): TLS для `<домен>` → `web:8000` (сеть оверлея). Наружу публикация порта **не** нужна.
5. `docker stack deploy -c docker-stack.yaml bot`.
6. Проверка: `curl https://<домен>/api/healthz` → `status: ok`; вход через Discord; сервер виден только если пользователь в нём и имеет Manage Guild.

Пока `DISCORD_CLIENT_*`/`WEB_SESSION_SECRET` не заданы, сайт работает в dev-режиме (без авторизации) — в проде не публиковать порт наружу до шага 2.
