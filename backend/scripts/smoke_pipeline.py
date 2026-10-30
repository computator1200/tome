"""End-to-end smoke test for the processing pipeline.

Builds a Series + Volume from real sample images, runs the pipeline, and prints
the resulting Job/Page/Region state. By default the LLM is mocked so it runs
offline; pass --live to hit the real OPENAI_BASE_URL endpoint.

Run from backend/:  python scripts/smoke_pipeline.py [--live] [--engine vision|ocr_text]
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import SessionLocal, init_db  # noqa: E402
from db.models import EngineMode, Job, JobStatus, Page, Series, Volume, VolumeStatus  # noqa: E402
from services import llm  # noqa: E402
from storage_backend import get_store  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[2] / "samples"


def _mock_vision(series_ctx, glossary, image_bytes):
    return {"regions": [{"id": "col_0", "order": 0, "x": 70, "y": 10, "width": 8, "height": 80,
                         "japanese_text": "テスト", "english_text": "Test", "confidence": 90.0}],
            "new_terms": {"characters": {"美羽": "Miu"}}}


def _mock_text(series_ctx, glossary, pages_payload):
    results = [{"page_index": p["page_index"],
                "translations": {r["id"]: f"[EN] {r['japanese_text'][:20]}" for r in p["regions"]}}
               for p in pages_payload]
    return {"results": results, "new_terms": {}}


async def main(live: bool, engine: str, limit: int):
    if not live:
        llm.vision_ocr_translate = _mock_vision
        llm.translate_text = _mock_text

    images = sorted(p for p in SAMPLES.rglob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png"})[:limit]
    if not images:
        print(f"No sample images under {SAMPLES}")
        return

    await init_db()
    store = get_store()

    async with SessionLocal() as s:
        series = Series(title="Smoke Test Series", description="LN about a smoke test.")
        s.add(series)
        await s.flush()
        vol = Volume(series_id=series.id, engine_mode=engine, status=VolumeStatus.QUEUED.value,
                     page_count=len(images))
        s.add(vol)
        await s.flush()
        for i, img in enumerate(images):
            key = f"volumes/{vol.id}/pages/{i:04d}{img.suffix.lower()}"
            store.put(key, img.read_bytes(), "image/jpeg")
            s.add(Page(volume_id=vol.id, index=i, image_key=key))
        s.add(Job(volume_id=vol.id, status=JobStatus.QUEUED.value))
        await s.commit()
        vid = vol.id

    print(f"Created volume {vid} with {len(images)} pages; engine={engine}; live={live}")

    from services.pipeline import process_volume
    await process_volume(vid)

    async with SessionLocal() as s:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        vol = (await s.execute(
            select(Volume).where(Volume.id == vid).options(
                selectinload(Volume.job), selectinload(Volume.pages).selectinload(Page.regions))
        )).scalar_one()
        print(f"\nVolume status: {vol.status}")
        print(f"Job: {vol.job.status} | {vol.job.step} | {vol.job.message} | err={vol.job.error[:80]!r}")
        for p in vol.pages:
            print(f"  page {p.index} [{p.page_type}] regions={len(p.regions)}"
                  + (f"  e.g. {p.regions[0].japanese_text!r}→{p.regions[0].english_text!r}" if p.regions else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="hit the real OPENAI_BASE_URL endpoint")
    ap.add_argument("--engine", default="vision", choices=["vision", "ocr_text"])
    ap.add_argument("--limit", type=int, default=8)
    args = ap.parse_args()
    asyncio.run(main(args.live, args.engine, args.limit))
