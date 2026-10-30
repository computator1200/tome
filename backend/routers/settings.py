import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from services import llm, provider

router = APIRouter(prefix="/api/settings", tags=["settings"])


class ProviderOut(BaseModel):
    base_url: str
    model: str
    vision_model: str
    timeout: float
    concurrency: int
    ocr_backend: str
    api_key_set: bool
    api_key_hint: str


class ProviderIn(BaseModel):
    base_url: str | None = None
    model: str | None = None
    vision_model: str | None = None
    timeout: float | None = None
    concurrency: int | None = None
    ocr_backend: str | None = None
    # Omit or leave blank to keep the existing key.
    api_key: str | None = None


@router.get("", response_model=ProviderOut)
async def get_settings():
    return ProviderOut(**provider.masked())


@router.put("", response_model=ProviderOut)
async def update_settings(body: ProviderIn):
    await provider.update(body.model_dump(exclude_unset=True))
    return ProviderOut(**provider.masked())


@router.post("/test")
async def test_connection(body: ProviderIn | None = None):
    """Apply any provided overrides, then do a tiny chat to confirm the endpoint
    + text model work. Returns the honest LLMError message on failure."""
    if body is not None:
        await provider.update(body.model_dump(exclude_unset=True))
    try:
        def _ping():
            return llm.translate_text(
                {"source_language": "Japanese", "target_language": "English"},
                [],
                [{"page_index": 0, "regions": [{"id": "col_0", "japanese_text": "テスト"}]}],
            )
        result = await asyncio.to_thread(_ping)
        ok = bool(result.get("results"))
        return {"ok": ok, "message": "Connection OK" if ok else "Endpoint replied but returned no translation."}
    except Exception as e:  # noqa: BLE001 — surface the honest message
        return {"ok": False, "message": str(e)}
