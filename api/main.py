from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.responses import Response

from . import read as read_api
from . import readiness
from .config import ConfigError, WebConfig, load_config
from .queries import ensure_web_indexes
from .supervise import LoopMonitor, Supervisor

API_DIR = Path(__file__).resolve().parent
UI_DIST = (API_DIR.parent / "ui" / "dist").resolve()

VERSION = "0.2.0"


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _mongo_ping(app: FastAPI) -> dict[str, object]:
    return readiness.mongo_ping(getattr(app.state, "mongo", None))


def _static_file(full_path: str) -> Path | None:
    if not full_path:
        return None
    candidate = (UI_DIST / full_path).resolve()
    if not candidate.is_file():
        return None
    try:
        candidate.relative_to(UI_DIST)
    except ValueError:
        return None
    return candidate


def create_app(
    config: WebConfig | None = None,
    mongo_client: object | None = None,
    db: object | None = None,
) -> FastAPI:
    cfg = config or load_config()
    if cfg.is_production and (cfg.dev_bypass_auth or not cfg.auth_enabled):
        # load_config already rejects this; keep the guard so no code path can
        # start a production app with the auth gate disabled.
        raise ConfigError(
            "WEB_ENV=production requires full OAuth/session configuration "
            "and forbids WEB_DEV_BYPASS_AUTH"
        )
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    log = logging.getLogger("api")
    if cfg.dev_bypass_auth:
        log.warning("auth bypass enabled via WEB_DEV_BYPASS_AUTH (mode=%s); never expose this instance", cfg.app_env)
    elif not cfg.auth_enabled and not cfg.is_production:
        log.warning("auth gate disabled: OAuth/session config incomplete (mode=%s)", cfg.app_env)

    # T12: создаём до lifespan — /api/readyz должен уметь отвечать и вне его
    supervisor = Supervisor()
    loop_monitor = LoopMonitor()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = getattr(app.state, "db", None)
        if database is not None:
            try:
                log.info("web indexes: %s", ensure_web_indexes(database))
            except Exception:
                log.warning("web index creation failed", exc_info=True)
            # T10: контракт индексов (сверка по спецификации); нарушение не скрывается
            from .schema_contract import verify_web_schema

            schema_report = verify_web_schema(database)
            app.state.schema_report = schema_report
            if schema_report.get("acceptedAlias"):
                log.info("schema: эквивалентные индексы под другими именами: %s",
                         schema_report["acceptedAlias"])
            if not schema_report.get("skipped") and not schema_report.get("ok"):
                (log.warning if cfg.app_env != "production" else log.error)(
                    "schema: индексы web-контракта отсутствуют: %s — выполните migration runner",
                    schema_report["missing"])
            try:
                from .queries import migrate_settings_revision

                migrated = migrate_settings_revision(database)
                if migrated:
                    log.info("guild_settings revision backfill: %s docs", migrated)
            except Exception:
                log.warning("guild_settings revision backfill failed", exc_info=True)

        # T12: фоновые циклы под надзором — исключение наблюдаемо, respawn с
        # backoff+jitter, повторяющийся критичный отказ снимает readiness.
        supervisor.spawn("web-loop-monitor", loop_monitor.run)
        if database is not None:
            async def schema_recheck() -> None:
                from .schema_contract import verify_web_schema as _verify

                while True:
                    await asyncio.sleep(readiness.SCHEMA_RECHECK_INTERVAL_S)
                    try:
                        app.state.schema_report = await asyncio.to_thread(_verify, database)
                        supervisor.beat("web-schema-recheck")
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.warning("schema recheck failed", exc_info=True)

            supervisor.spawn("web-schema-recheck", schema_recheck)
            if cfg.discord_token:
                from . import audit_sync

                supervisor.spawn(
                    "discord-audit-sync",
                    lambda: audit_sync.run_loop(app, on_cycle=lambda: supervisor.beat("discord-audit-sync")),
                    critical=True,
                )
            supervisor.spawn(
                "web-diagnostics",
                lambda: readiness.diagnostics_loop(app),
            )
        try:
            yield
        finally:
            # R05: drain — cancel+await всех надзираемых задач, затем закрываем Mongo.
            await supervisor.shutdown()
            close = getattr(getattr(app.state, "mongo", None), "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    log.warning("mongo client close failed", exc_info=True)

    app = FastAPI(
        title="sklep-ds-bot-web",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.state.supervisor = supervisor
    app.state.loop_monitor = loop_monitor
    app.state.schema_report = None

    if db is None and mongo_client is None and cfg.mongo_uri:
        try:
            from pymongo import MongoClient

            mongo_client = MongoClient(
                cfg.mongo_uri,
                serverSelectionTimeoutMS=2000,
                connect=False,
            )
        except Exception:
            mongo_client = None
    app.state.mongo = mongo_client
    if db is not None:
        app.state.db = db
    elif mongo_client is not None and cfg.mongo_db:
        app.state.db = mongo_client[cfg.mongo_db]
    else:
        app.state.db = None

    if cfg.auth_enabled:
        from starlette.middleware.sessions import SessionMiddleware

        app.add_middleware(
            SessionMiddleware,
            secret_key=cfg.web_session_secret,
            https_only=cfg.web_public_url.startswith("https://"),
            same_site="lax",
        )

    @app.get("/api/healthz")
    def healthz(request: Request) -> JSONResponse:
        # T12: liveness — «процесс жив и обслуживает запросы». Mongo-статус
        # остаётся в payload для совместимости, но на код ответа не влияет:
        # отвал зависимости не должен выглядеть как смерть процесса (R01).
        mongo = _mongo_ping(request.app)
        payload = {
            "status": "ok",
            "role": "liveness",
            "service": "web",
            "version": VERSION,
            "env": cfg.app_env,
            "mongo": mongo,
            "loop": loop_monitor.snapshot(),
            "auth_enabled": cfg.auth_enabled,
        }
        return JSONResponse(payload, status_code=200)

    @app.get("/api/readyz")
    def readyz(request: Request) -> JSONResponse:
        # T12: readiness — «готов выполнять работу»: bounded Mongo ping, схема,
        # media mount, живые критичные фоновые циклы. 503 = не готов (R01/R02).
        ready, payload = readiness.evaluate(request.app)
        payload.update({"service": "web", "version": VERSION, "env": cfg.app_env})
        return JSONResponse(payload, status_code=200 if ready else 503)

    from . import auth as auth_api
    from . import bot as bot_api
    from . import media as media_api
    from . import write as write_api

    app.include_router(auth_api.router)
    app.include_router(auth_api.guilds_router)
    app.include_router(read_api.router)
    app.include_router(write_api.router)
    app.include_router(bot_api.router)
    # T05: вместо публичного StaticFiles — авторизованная выдача вложений
    # (доказательство связи файла с гильдией через метаданные чата).
    app.include_router(media_api.guild_router)
    app.include_router(media_api.router)

    # SPA catch-all: serve built ui/dist assets, fall back to index.html for client routes.
    # Хэшированные ассеты (assets/index-<hash>.js) можно кэшировать навечно, а index.html
    # — никогда, иначе браузер держит старый бандл после пересборки.
    @app.get("/{full_path:path}", response_model=None)
    def spa(request: Request, full_path: str) -> Response:
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        static = _static_file(full_path)
        if static is not None:
            if static.parent == UI_DIST / "assets":
                cache = "public, max-age=31536000, immutable"
            else:
                cache = "no-cache, no-store, must-revalidate"
            return FileResponse(static, headers={"Cache-Control": cache})
        index = UI_DIST / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
        return JSONResponse(
            {"detail": "ui not built — run `npm ci && npm run build` in ui/ or use the docker image"},
            status_code=503,
        )

    return app


app = create_app()
