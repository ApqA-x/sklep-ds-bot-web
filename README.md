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
| 1. Read API | ✅ текущий (`api/queries.py`, `api/read.py`, web-индексы `web_*` при старте) |
| 2. SPA v1 | ⬜ |
| 3. OAuth + права | ⬜ |
| 4. Write-слой | ⬜ |
| 5. Discord-действия | ⬜ |
| 6. UI управления | ⬜ |
| 7. Прод | ⬜ |
