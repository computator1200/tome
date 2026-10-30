import mimetypes
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import URL_PREFIX

# Python reads file types from the Windows registry, which often maps .js to
# text/plain; browsers then refuse to run the bundle and the app is a blank page.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
from db.database import SessionLocal, init_db
from logging_config import configure_logging, get_logger
from routers import agent, export, files, novelupdates, process, series, settings, volumes
from services import memory, provider
from services.agent import recover_orphans as recover_agent_orphans
from services.worker import start_worker

configure_logging()
logger = get_logger("main")

_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def _version_file() -> str:
    """The version the deployment ships, read once at import."""
    try:
        root = Path(__file__).resolve().parent.parent
        return (root / "VERSION").read_text().strip() or "0.0.0"
    except OSError:
        return "0.0.0"
_STARTED_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Tome API starting (prefix=%r)", URL_PREFIX)
    await init_db()           # create tables (idempotent; dev-friendly)
    await provider.load_from_db()  # apply UI-saved provider overrides over .env
    async with SessionLocal() as s:  # re-key memory aliases if normalisation changed
        await memory.upgrade_norms(s)
    await start_worker()      # start the background worker + recover orphaned jobs
    await recover_agent_orphans()  # mark agent runs killed by the restart
    yield


app = FastAPI(title="Tome API", version=_version_file(), lifespan=lifespan)

# Allow LAN devices in dev — tighten for production. Wildcard origins require
# allow_credentials=False per the CORS spec.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Every route is mounted under URL_PREFIX so the app can live at
# https://host/tome without the proxy stripping the path.
app.include_router(series.router, prefix=URL_PREFIX)
app.include_router(novelupdates.router, prefix=URL_PREFIX)
app.include_router(volumes.router, prefix=URL_PREFIX)
app.include_router(process.router, prefix=URL_PREFIX)
app.include_router(process.series_router, prefix=URL_PREFIX)
app.include_router(files.router, prefix=URL_PREFIX)
app.include_router(settings.router, prefix=URL_PREFIX)
app.include_router(export.router, prefix=URL_PREFIX)
app.include_router(agent.router, prefix=URL_PREFIX)


_ROOT = Path(__file__).resolve().parent.parent


def _version() -> dict:
    """What is actually running, so a deploy can be identified from outside.

    A demo where nobody can tell which build is live is a demo you cannot debug:
    the version file, the git commit the deploy was built from (if the deploy
    stamped one), and the process start time are the three facts worth having.
    """
    version = "0.0.0"
    try:
        version = (_ROOT / "VERSION").read_text().strip() or version
    except OSError:
        pass
    return {
        "version": version,
        "commit": os.environ.get("TOME_COMMIT", ""),
        "started_at": _STARTED_AT,
    }


@app.get(URL_PREFIX + "/api/version")
async def version():
    return _version()


def _capabilities() -> dict:
    """What this deployment can actually do.

    The UI offers local OCR as an engine, but it needs torch and manga-ocr in
    this venv; without them the option exists only to fail, so the server says
    whether it is available rather than letting a user pick a dead path.
    """
    import importlib.util

    return {
        "local_ocr": importlib.util.find_spec("manga_ocr") is not None,
        "pdf": importlib.util.find_spec("pypdf") is not None,
    }


@app.get(URL_PREFIX + "/api/health")
async def health():
    return {"status": "ok", **_version(), "capabilities": _capabilities()}


# --- built SPA ---------------------------------------------------------------
if _DIST.is_dir():
    app.mount(URL_PREFIX + "/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get(URL_PREFIX + "/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str):
        """Serve the built frontend; unknown paths fall back to index.html.

        API paths are excluded: a typo or a wrong verb under /api used to return
        the SPA with a 200, which reads as success to any client.
        """
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Unknown API route")
        if full_path:
            candidate = _DIST / full_path
            if candidate.is_file():
                return FileResponse(candidate)
        return FileResponse(_DIST / "index.html")
