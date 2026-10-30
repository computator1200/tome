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
_FIELDS = ("base_url", "api_key", "model", "vision_model", "timeout", "concurrency", "ocr_backend",
           "price_in", "price_out", "volume_concurrency")

_cfg: dict[str, str] = {
    "base_url": OPENAI_BASE_URL,
    "api_key": OPENAI_API_KEY,
    "model": OPENAI_MODEL,
    "vision_model": OPENAI_VISION_MODEL,
    "timeout": str(OPENAI_TIMEOUT),
    "concurrency": str(OPENAI_CONCURRENCY),
    "ocr_backend": OCR_BACKEND,
    # USD per million input / output tokens. Blank means unknown: usage is still
    # counted, but no cost estimate is shown.
    "price_in": "",
    "price_out": "",
    # Volumes processed at once, never two of the same series.
    "volume_concurrency": "1",
}


def _float(value, default: float | None) -> float | None:
    try:
        return float(value) if str(value).strip() != "" else default
    except ValueError:
        return default


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
    try:
        c["volume_concurrency"] = max(1, int(c.get("volume_concurrency") or 1))
    except ValueError:
        c["volume_concurrency"] = 1
    c["price_in"] = _float(c.get("price_in"), None)
    c["price_out"] = _float(c.get("price_out"), None)
    return c


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float | None:
    """Estimated USD for this many tokens, or None when prices are not set."""
    c = get()
    if c["price_in"] is None or c["price_out"] is None:
        return None
    return round((prompt_tokens * c["price_in"] + completion_tokens * c["price_out"]) / 1e6, 4)


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
        "price_in": c["price_in"],
        "price_out": c["price_out"],
        "volume_concurrency": c["volume_concurrency"],
        "api_key_set": bool(key),
        "api_key_hint": ("…" + key[-4:]) if len(key) >= 4 else "",
    }
