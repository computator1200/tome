"""Runtime LLM provider configuration.

The provider settings (endpoint, key, models) start from .env (config.py) and can
be overridden at runtime from the UI; overrides are persisted in the app_settings
table. The current values live in an in-memory dict so the blocking LLM calls
(run via asyncio.to_thread) can read them synchronously without touching the DB.
"""
from __future__ import annotations

from sqlalchemy import select

from config import (
    OCR_BACKEND,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_CONCURRENCY,
    OPENAI_MODEL,
    OPENAI_TIMEOUT,
    OPENAI_VISION_MODEL,
)
from db.database import SessionLocal
from db.models import AppSetting

_PREFIX = "provider."
_FIELDS = ("base_url", "api_key", "model", "vision_model", "timeout", "concurrency", "ocr_backend")

_cfg: dict[str, str] = {
    "base_url": OPENAI_BASE_URL,
    "api_key": OPENAI_API_KEY,
    "model": OPENAI_MODEL,
    "vision_model": OPENAI_VISION_MODEL,
    "timeout": str(OPENAI_TIMEOUT),
    "concurrency": str(OPENAI_CONCURRENCY),
    "ocr_backend": OCR_BACKEND,
}


def get() -> dict:
    """Current effective provider config (sync; safe inside worker threads).
    vision_model falls back to the text model when unset."""
    c = dict(_cfg)
    if not c.get("vision_model"):
        c["vision_model"] = c["model"]
    try:
        c["timeout"] = float(c.get("timeout") or 180)
    except ValueError:
        c["timeout"] = 180.0
    try:
        c["concurrency"] = max(1, int(c.get("concurrency") or 4))
    except ValueError:
        c["concurrency"] = 4
    return c


def set_runtime(**patch) -> None:
    """Update the in-memory config only (no DB). For scripts/tests."""
    for f, v in patch.items():
        if f in _FIELDS and v is not None:
            _cfg[f] = str(v)


async def load_from_db() -> None:
    """Hydrate overrides from the DB at startup; .env values remain the fallback."""
    async with SessionLocal() as s:
        rows = (await s.execute(select(AppSetting))).scalars().all()
    for r in rows:
        if r.key.startswith(_PREFIX):
            field = r.key[len(_PREFIX):]
            if field in _FIELDS and r.value != "":
                _cfg[field] = r.value


async def update(patch: dict) -> None:
    """Persist + apply provider overrides. An empty/omitted api_key keeps the
    existing key (so the UI never has to re-send the secret)."""
    async with SessionLocal() as s:
        for field in _FIELDS:
            if field not in patch:
                continue
            value = patch[field]
            if field == "api_key" and not value:
                continue  # keep existing key
            value = "" if value is None else str(value)
            _cfg[field] = value
            row = (await s.execute(
                select(AppSetting).where(AppSetting.key == _PREFIX + field)
            )).scalar_one_or_none()
            if row is None:
                s.add(AppSetting(key=_PREFIX + field, value=value))
            else:
                row.value = value
        await s.commit()


def masked() -> dict:
    """Provider config for display: the API key is never returned, only whether
    it is set and its last 4 characters."""
    c = get()
    key = c.get("api_key") or ""
    return {
        "base_url": c["base_url"],
        "model": c["model"],
        "vision_model": _cfg.get("vision_model", ""),  # raw (may be blank = use model)
        "timeout": c["timeout"],
        "concurrency": c["concurrency"],
        "ocr_backend": c.get("ocr_backend", "vision"),
        "api_key_set": bool(key),
        "api_key_hint": ("…" + key[-4:]) if len(key) >= 4 else "",
    }
