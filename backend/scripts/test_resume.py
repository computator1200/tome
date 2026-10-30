"""Verify resume-skip and cancel->pause logic without a slow live run.

Builds a volume from sample images, pre-marks some text pages as already
translated, then runs the pipeline with a mocked vision call and asserts only
the unfinished pages are processed. Also checks that a pending cancel pauses.

Run from backend/:  python scripts/test_resume.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from db.database import SessionLocal, init_db
from db.models import (EngineMode, JobStatus, Page, Region, Series, Volume,
                       VolumeStatus, PageType)
from services import llm, pipeline

SAMPLES = Path(__file__).resolve().parents[2] / "samples"
_calls = {"n": 0}


def _mock_vision(ctx, gloss, img):
    _calls["n"] += 1
    return {"regions": [{"id": "col_0", "order": 0, "x": 70, "y": 8, "width": 8, "height": 80,
                         "japanese_text": "テスト", "english_text": "Test line.", "confidence": 90.0}],
            "new_terms": {}}


async def _mk_volume(session, images):
    from storage_backend import get_store
    store = get_store()
    ser = Series(title="Resume Test"); session.add(ser); await session.flush()
    vol = Volume(series_id=ser.id, engine_mode=EngineMode.VISION.value,
                 status=VolumeStatus.QUEUED.value, page_count=len(images))
    session.add(vol); await session.flush()
    for i, img in enumerate(images):
        key = f"volumes/{vol.id}/pages/{i:04d}{img.suffix.lower()}"
        store.put(key, img.read_bytes(), "image/jpeg")
        # force every page to be a text page so all are candidates
        session.add(Page(volume_id=vol.id, index=i, image_key=key, page_type=PageType.TEXT.value))
    from db.models import Job
    session.add(Job(volume_id=vol.id, status=JobStatus.QUEUED.value))
    await session.commit()
    return vol.id


async def main():
    llm.vision_ocr_translate = _mock_vision
    await init_db()
    # Use dense text pages (the pa1_01x.png scans) so the classifier keeps them
    # as text pages and vision actually runs on each.
    images = sorted(p for p in SAMPLES.rglob("pa1_01[0-4].png"))[:5]

    # --- Test 1: resume skips already-translated pages ---
    async with SessionLocal() as s:
        vid = await _mk_volume(s, images)
        # Pre-translate the first 3 pages (simulate work done before a pause).
        v = (await s.execute(select(Volume).where(Volume.id == vid)
             .options(selectinload(Volume.pages)))).scalar_one()
        for p in v.pages[:3]:
            s.add(Region(page_id=p.id, order=0, japanese_text="x", english_text="already done"))
        await s.commit()

    _calls["n"] = 0
    await pipeline.process_volume(vid, resume=True)
    async with SessionLocal() as s:
        v = (await s.execute(select(Volume).where(Volume.id == vid)
             .options(selectinload(Volume.job)))).scalar_one()
        print(f"RESUME: vision called {_calls['n']} times (expected 2 = 5 pages - 3 done); "
              f"status={v.status}; job={v.job.status}")
        assert _calls["n"] == 2, "resume should only process the 2 unfinished pages"
        assert v.status == VolumeStatus.DONE.value

    # --- Test 2: a pending cancel pauses cleanly ---
    async with SessionLocal() as s:
        vid2 = await _mk_volume(s, images)
    pipeline.request_cancel(vid2)            # request stop before it runs
    await pipeline.process_volume(vid2, resume=False)
    async with SessionLocal() as s:
        v = (await s.execute(select(Volume).where(Volume.id == vid2)
             .options(selectinload(Volume.job)))).scalar_one()
        print(f"CANCEL: status={v.status}; job={v.job.status}; msg={v.job.message!r}")
        assert v.status == VolumeStatus.PAUSED.value
        assert v.job.status == JobStatus.CANCELLED.value

    # Clean up the throwaway series this script created (it runs against the dev
    # DB), so it never leaves "Resume Test" pollution behind.
    async with SessionLocal() as s:
        for ser in (await s.execute(select(Series).where(Series.title == "Resume Test"))).scalars().all():
            await s.delete(ser)
        await s.commit()

    print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
