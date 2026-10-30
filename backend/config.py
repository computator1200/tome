import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root (one level up from backend/)
_root = Path(__file__).resolve().parent.parent
load_dotenv(_root / ".env")

# --- Single OpenAI-compatible LLM endpoint ---------------------------------
# Any server that speaks the OpenAI Chat Completions API: OpenAI, a local
# Ollama (`http://localhost:11434/v1`), vLLM, LM Studio, OpenRouter, etc.
OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
# Vision-capable model for the one-pass OCR+translate engine. Falls back to
# OPENAI_MODEL when unset (fine if that model is itself multimodal).
OPENAI_VISION_MODEL: str = os.getenv("OPENAI_VISION_MODEL", "") or OPENAI_MODEL
OPENAI_TIMEOUT: float = float(os.getenv("OPENAI_TIMEOUT", "180"))
# Max concurrent LLM requests per processing stage (pages/batches in flight).
OPENAI_CONCURRENCY: int = int(os.getenv("OPENAI_CONCURRENCY", "4"))
# OCR backend for the ocr_text engine: "vision" (vision model reads the page) or
# "manga_ocr" (local local OCR + OpenCV column detection).
OCR_BACKEND: str = os.getenv("OCR_BACKEND", "vision")

# --- Database ---------------------------------------------------------------
_default_db = _root / "backend" / "data" / "ln.db"
DATABASE_URL: str = os.getenv(
    "DATABASE_URL", f"sqlite+aiosqlite:///{_default_db.as_posix()}"
)

# Normalise SQLite URLs: resolve relative file paths against the project root
# (not the cwd — uvicorn is launched from backend/) and ensure the parent dir
# exists. Without this, a relative URL like "..././backend/data/ln.db" would
# resolve to backend/backend/data and fail with "unable to open database file".
if DATABASE_URL.startswith("sqlite"):
    _prefix, _sep, _raw = DATABASE_URL.partition(":///")
    if _sep and _raw and _raw != ":memory:":
        _db = Path(_raw)
        if not _db.is_absolute():
            _db = (_root / _raw).resolve()
        _db.parent.mkdir(parents=True, exist_ok=True)
        DATABASE_URL = f"{_prefix}:///{_db.as_posix()}"

# --- Object store -----------------------------------------------------------
STORAGE_BACKEND: str = os.getenv("STORAGE_BACKEND", "local")  # local | s3

_storage_raw = os.getenv("LOCAL_STORAGE_DIR", os.getenv("STORAGE_DIR", "backend/storage"))
_storage_path = Path(_storage_raw)
if not _storage_path.is_absolute():
    _storage_path = _root / _storage_path
LOCAL_STORAGE_DIR: Path = _storage_path.resolve()

# Ensure the local storage dir exists (harmless when using S3 — just unused).
# The SQLite parent dir is already created during URL normalisation above.
LOCAL_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


# --- Deployment ---------------------------------------------------------------
# Served under a subpath (e.g. /tome) behind a reverse proxy that does
# NOT strip the prefix. Empty string keeps the app at the domain root.
URL_PREFIX: str = os.getenv("URL_PREFIX", "").rstrip("/")
