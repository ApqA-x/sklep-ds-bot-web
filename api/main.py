from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from . import read as read_api
from .config import WebConfig, load_config
from .queries import ensure_web_indexes

API_DIR = Path(__file__).resolve().parent
UI_DIST = (API_DIR.parent / "ui" / "dist").resolve()

VERSION = "0.2.0"


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _mongo_ping(app: FastAPI) -> dict[str, object]:
    mongo = getattr(app.state, "mongo", None)
    if mongo is None:
        return {"ok": False, "error": "not configured"}
    try:
        mongo.admin.command("ping")
        return {"ok": True}
    except Exception as exc:  # serverSelectionTimeoutMS bounds the wait
        return {"ok": False, "error": type(exc).__name__}


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
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    log = logging.getLogger("api")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = getattr(app.state, "db", None)
        if database is not None:
            try:
                log.info("web indexes: %s", ensure_web_indexes(database))
            except Exception:
                log.warning("web index creation failed", exc_info=True)
        sync_task: asyncio.Task | None = None
        if database is not None and cfg.discord_token:
            from . import audit_sync

            sync_task = asyncio.create_task(audit_sync.run_loop(app))
        try:
            yield
        finally:
            if sync_task is not None:
                sync_task.cancel()

    app = FastAPI(
        title="sklep-ds-bot-web",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config = cfg

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
        mongo = _mongo_ping(request.app)
        payload = {
            "status": "ok" if mongo["ok"] else "degraded",
            "service": "web",
            "version": VERSION,
            "mongo": mongo,
            "auth_enabled": cfg.auth_enabled,
        }
        return JSONResponse(payload, status_code=200)

    from . import auth as auth_api
    from . import bot as bot_api
    from . import write as write_api

    app.include_router(auth_api.router)
    app.include_router(auth_api.guilds_router)
    app.include_router(read_api.router)
    app.include_router(write_api.router)
    app.include_router(bot_api.router)

    # Раздача сохранённых вложений (картинки, скачанные ботом в общий volume).
    # Пути содержат sha256 содержимого — наружу не угадать; доступ без сессии
    # осознанно: это те же картинки, что лежали открытыми на CDN Discord.
    media_dir = _clean(cfg.media_dir)
    if media_dir:
        media_root = Path(media_dir)
        media_root.mkdir(parents=True, exist_ok=True)
        app.mount("/media", StaticFiles(directory=str(media_root)), name="media")

    # SPA catch-all: serve built ui/dist assets, fall back to index.html for client routes.
    @app.get("/{full_path:path}", response_model=None)
    def spa(request: Request, full_path: str) -> Response:
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        static = _static_file(full_path)
        if static is not None:
            return FileResponse(static)
        index = UI_DIST / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {"detail": "ui not built — run `npm ci && npm run build` in ui/ or use the docker image"},
            status_code=503,
        )

    return app


app = create_app()
