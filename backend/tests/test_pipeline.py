"""Pipeline failure handling, run end to end against an in-memory database.

The model, the object store and the page classifier are replaced with fakes, so
these exercise process_volume's own bookkeeping: what counts as a failure, what
the volume ends as, and what a re-run picks up.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import Base  # noqa: E402
from db.models import (  # noqa: E402
    Job, Page, PageType, Region, Series, Volume, VolumeKind, VolumeStatus,
)
from services import llm, pipeline, provider  # noqa: E402


class _Store:
    def get(self, key):
        return b"image:" + key.encode()


def _harness(monkeypatch, *, engine_mode="vision", ocr_backend="vision", pages=3,
             kind=VolumeKind.SCANS.value, texts=None):
    """A series with one volume of `pages` pages, wired to fakes. Returns
    (run, load): run() processes the volume, load() reads it back."""
    # A file, not :memory:, with no pooling: the pipeline opens several sessions
    # at once, as it does in production, and each run is its own event loop.
    path = Path(tempfile.mkdtemp()) / "tome.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}", poolclass=NullPool,
                                 connect_args={"timeout": 30})
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", Session)
    monkeypatch.setattr(pipeline, "get_store", lambda: _Store())
    monkeypatch.setattr(pipeline, "classify_page", lambda img, name: PageType.TEXT)
    cfg = dict(provider.get(), concurrency=2, ocr_backend=ocr_backend)
    monkeypatch.setattr(provider, "get", lambda: cfg)
    monkeypatch.setattr(llm, "summarize", lambda ctx, text: "")
    ids = {}

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            series = Series(title="T", source_language="Japanese")
            s.add(series)
            await s.flush()
            vol = Volume(series_id=series.id, engine_mode=engine_mode, kind=kind, order=1)
            s.add(vol)
            await s.flush()
            for i in range(pages):
                page = Page(volume_id=vol.id, index=i,
                            image_key="" if kind == VolumeKind.TEXT.value else f"p{i}.jpg")
                s.add(page)
                if texts:
                    await s.flush()
                    s.add(Region(page_id=page.id, order=0, japanese_text=texts[i]))
            await s.commit()
            ids["volume"] = vol.id

    asyncio.run(setup())

    def run(resume=False):
        asyncio.run(pipeline.process_volume(ids["volume"], resume=resume))

    def load():
        async def go():
            async with Session() as s:
                vol = (await s.execute(
                    select(Volume).where(Volume.id == ids["volume"])
                    .options(selectinload(Volume.pages).selectinload(Page.regions)))).scalar_one()
                job = (await s.execute(select(Job).where(Job.volume_id == vol.id))).scalar_one()
                return vol, job
        return asyncio.run(go())

    return run, load


def _vision_ok(ctx, glossary, img):
    return {"regions": [{"id": "col_0", "order": 0, "japanese_text": "原文",
                         "english_text": "text"}], "new_terms": {}, "memory": {}}


def test_a_failed_vision_page_fails_alone(monkeypatch):
    def vision(ctx, glossary, img):
        if img.endswith(b"p1.jpg"):
            raise llm.LLMError("bad page")
        return _vision_ok(ctx, glossary, img)
    monkeypatch.setattr(llm, "vision_ocr_translate", vision)
    run, load = _harness(monkeypatch)
    run()
    vol, job = load()
    assert vol.status == VolumeStatus.ERROR.value
    assert job.error == "1 page(s) failed"
    translated = [p.index for p in vol.pages if p.regions]
    assert translated == [0, 2]


def test_an_unexpected_exception_is_counted_not_swallowed(monkeypatch):
    def vision(ctx, glossary, img):
        if img.endswith(b"p2.jpg"):
            raise RuntimeError("not an LLMError")
        return _vision_ok(ctx, glossary, img)
    monkeypatch.setattr(llm, "vision_ocr_translate", vision)
    run, load = _harness(monkeypatch)
    run()
    vol, job = load()
    assert vol.status == VolumeStatus.ERROR.value
    assert job.error == "1 page(s) failed"


def test_a_page_the_ocr_model_cannot_read_is_a_failure(monkeypatch):
    def ocr(ctx, img):
        if img.endswith(b"p0.jpg"):
            raise llm.LLMError("unreadable")
        return [{"id": "col_0", "order": 0, "japanese_text": "原文"}]

    def translate(ctx, glossary, payload):
        return {"results": [{"page_index": p["page_index"],
                             "translations": {r["id"]: "text" for r in p["regions"]}}
                            for p in payload], "new_terms": {}, "memory": {}}
    monkeypatch.setattr(llm, "ocr_page_vision", ocr)
    monkeypatch.setattr(llm, "translate_text", translate)
    run, load = _harness(monkeypatch, engine_mode="ocr_text")
    run()
    vol, job = load()
    assert vol.status == VolumeStatus.ERROR.value, "an unread page must not finish as done"
    assert job.error == "1 page(s) failed"


def test_local_ocr_failure_is_counted(monkeypatch):
    import types

    def ocr_page(img):
        if img.endswith(b"p1.jpg"):
            raise RuntimeError("model crashed")
        return [{"id": "col_0", "order": 0, "japanese_text": "原文"}]
    fake = types.ModuleType("services.ocr")
    fake.ocr_page = ocr_page
    fake.unload_ocr = lambda: None
    monkeypatch.setitem(sys.modules, "services.ocr", fake)
    monkeypatch.setattr(llm, "translate_text", lambda ctx, g, payload: {
        "results": [{"page_index": p["page_index"],
                     "translations": {r["id"]: "text" for r in p["regions"]}} for p in payload],
        "new_terms": {}, "memory": {}})
    run, load = _harness(monkeypatch, engine_mode="ocr_text", ocr_backend="manga_ocr")
    run()
    vol, job = load()
    assert job.error == "1 page(s) failed"


def test_text_volume_failures_are_reported_as_chunks(monkeypatch):
    def translate(ctx, glossary, payload):
        raise llm.LLMError("down")
    monkeypatch.setattr(llm, "translate_text", translate)
    run, load = _harness(monkeypatch, kind=VolumeKind.TEXT.value, pages=2,
                         texts=["一章", "二章"])
    run()
    vol, job = load()
    assert job.error == "2 chunk(s) failed"


def test_resume_retries_only_what_failed(monkeypatch):
    calls = []
    broken = {"p1": True}

    def flaky(ctx, glossary, img):
        calls.append(img)
        if img.endswith(b"p1.jpg") and broken["p1"]:
            raise llm.LLMError("first time only")
        return _vision_ok(ctx, glossary, img)
    monkeypatch.setattr(llm, "vision_ocr_translate", flaky)
    run, load = _harness(monkeypatch)
    run()
    calls.clear()
    broken["p1"] = False
    run(resume=True)
    vol, job = load()
    assert calls == [b"image:p1.jpg"]
    assert vol.status == VolumeStatus.DONE.value


# --- bounding boxes -------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ([10, 20, 30, 40], {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0}),
    ([0.1, 0.2, 0.3, 0.4], {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0}),
    ([100, 200, 300, 400], {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0}),
    ({"x": 5, "y": 5, "w": 10, "h": 10}, {"x": 5.0, "y": 5.0, "width": 10.0, "height": 10.0}),
    ([60, 10, 45, 20], {"x": 60.0, "y": 10.0, "width": 40.0, "height": 20.0}),  # clamped
])
def test_box_scales_and_clamps(raw, expected):
    assert llm._box(raw) == expected


@pytest.mark.parametrize("raw", [None, [], [1, 2, 3], ["a", 1, 2, 3], [10, 10, 0, 5], [5000, 1, 1, 1]])
def test_unusable_box_is_no_box(raw):
    assert llm._box(raw) == {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0}


def test_vision_regions_keep_their_boxes(monkeypatch):
    monkeypatch.setattr(llm, "_chat", lambda model, content: {"regions": [
        {"source_text": "原文", "english_text": "text", "box": [10, 20, 30, 40]},
        {"source_text": "無箱", "english_text": "no box"}]})
    out = llm.vision_ocr_translate({}, [], b"img")
    assert out["regions"][0]["width"] == 30.0
    assert out["regions"][1]["width"] == 0.0


# --- usage --------------------------------------------------------------------

def test_model_usage_is_saved_on_the_job(monkeypatch):
    from types import SimpleNamespace

    def vision(ctx, glossary, img):
        resp = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20))
        llm._record(resp, 0.5)  # what _chat does after each response
        return _vision_ok(ctx, glossary, img)
    monkeypatch.setattr(llm, "vision_ocr_translate", vision)
    run, load = _harness(monkeypatch)
    run()
    _, job = load()
    assert (job.llm_calls, job.prompt_tokens, job.completion_tokens) == (3, 300, 60)
    assert job.llm_seconds == 1.5 and job.wall_seconds > 0
    run(resume=True)  # nothing left to do, so nothing is added
    _, job = load()
    assert job.llm_calls == 3


def test_cost_estimate_needs_both_prices(monkeypatch):
    base = provider.get()
    monkeypatch.setattr(provider, "get", lambda: dict(base, price_in=None, price_out=2.0))
    assert provider.estimate_cost(1_000_000, 1_000_000) is None
    monkeypatch.setattr(provider, "get", lambda: dict(base, price_in=0.5, price_out=2.0))
    assert provider.estimate_cost(1_000_000, 500_000) == 1.5


# --- memory through the pipeline ------------------------------------------------

def test_a_page_never_sees_what_a_later_page_established(monkeypatch):
    seen = {}

    def vision(ctx, glossary, img):
        index = int(img.decode().rsplit(":p", 1)[1].split(".")[0])
        seen[index] = ctx.get("memory", "")
        memory_payload = {}
        if index == 2:
            memory_payload = {"entities": [{"name": "Lizette", "kind": "person"},
                                           {"name": "Ashen Court", "kind": "org"}],
                              "relations": [{"src": "Lizette", "relation": "betrays",
                                             "dst": "Ashen Court"}]}
        return {"regions": [{"id": "col_0", "order": 0, "japanese_text": "x",
                             "english_text": "y"}],
                "new_terms": {"characters": {"Lizette": "Lizette"}} if index == 2 else {},
                "memory": memory_payload}
    monkeypatch.setattr(llm, "vision_ocr_translate", vision)
    run, load = _harness(monkeypatch)
    run()
    seen.clear()
    run()  # a full re-run: the graph now holds page 2's facts
    assert "Lizette" not in seen[0] and "Lizette" not in seen[1]
    assert "betrays" in seen[2]


def test_a_malformed_memory_payload_does_not_hurt_the_volume(monkeypatch):
    def vision(ctx, glossary, img):
        out = _vision_ok(ctx, glossary, img)
        out["memory"] = {"entities": [{"name": "Lizette", "kind": "person"},
                                      {"name": ["bad"]}],
                         "relations": [{"src": "Lizette", "relation": 3, "dst": None}]}
        return out
    monkeypatch.setattr(llm, "vision_ocr_translate", vision)
    run, load = _harness(monkeypatch)
    run()
    vol, job = load()
    assert vol.status == VolumeStatus.DONE.value, job.error
