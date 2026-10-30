"""Compare translation quality across OpenAI-compatible (Ollama) models.

Pulls real, cleanly-OCR'd Japanese regions from a processed volume and runs the
exact translate_text path against several models, reporting validity, latency
and the English output so we can pick a non-vision model that translates well.

Run from backend/:  python scripts/model_bakeoff.py
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.database import SessionLocal
from db.models import Page, Volume
from services import llm

CANDIDATES = [
    "nemotron-3-nano:30b-cloud",  # current baseline
    "qwen3.5:cloud",              # confirmed available; Qwen is strong at JA
    "minimax-m3:cloud",           # confirmed available
    "gpt-oss:20b",                # local
    "gemma4:26b",                 # local
]

PER_MODEL_CAP = 90  # seconds; a model that pulls/hangs is skipped rather than stalling


async def get_sample(volume_id: str, page_index: int, limit: int):
    async with SessionLocal() as s:
        v = (await s.execute(
            select(Volume).where(Volume.id == volume_id)
            .options(selectinload(Volume.pages).selectinload(Page.regions))
        )).scalar_one_or_none()
        if not v:
            return None, None
        page = next((p for p in v.pages if p.index == page_index), None)
        if not page:
            return None, None
        regions = [{"id": r.id, "japanese_text": r.japanese_text} for r in page.regions[:limit]]
        return page, regions


async def main(volume_id, page_index, limit):
    page, regions = await get_sample(volume_id, page_index, limit)
    if not regions:
        print("No regions found; pass a processed volume id / text page index.")
        return

    print(f"Test input: volume {volume_id} page {page_index}, {len(regions)} regions\n")
    for r in regions:
        print(f"  JP: {r['japanese_text']}")
    print("\n" + "=" * 70)

    payload = [{"page_index": page_index, "regions": regions}]
    series_ctx = {"title": "Papa no Iukoto wo Kikinasai!", "genres": ["Comedy", "Romance"],
                  "description": "A college student becomes guardian of his three young nieces."}

    # Short timeout so unavailable models fail fast.
    from services import provider
    provider.set_runtime(timeout=75)
    llm._client = None
    for model in CANDIDATES:
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(llm.translate_text, series_ctx, [], payload, model),
                timeout=PER_MODEL_CAP,
            )
            dt = time.time() - t0
            res = result.get("results", [])
            trans = res[0]["translations"] if res else {}
            n = sum(1 for v in trans.values() if v.strip())
            print(f"\n### {model}   ({dt:.1f}s, {n}/{len(regions)} translated)", flush=True)
            for r in regions:
                print(f"   {trans.get(r['id'], '<<missing>>')}", flush=True)
        except asyncio.TimeoutError:
            print(f"\n### {model}   SKIPPED (exceeded {PER_MODEL_CAP}s; likely pulling)", flush=True)
        except Exception as e:
            print(f"\n### {model}   FAILED: {repr(e)[:160]}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--volume", default="88c1dd562883")
    ap.add_argument("--page", type=int, default=13)
    ap.add_argument("--limit", type=int, default=6)
    args = ap.parse_args()
    asyncio.run(main(args.volume, args.page, args.limit))
