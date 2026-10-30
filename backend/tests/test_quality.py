"""Quality checks: glossary consistency per volume, and the regression sample."""
import asyncio
import sys
import tempfile
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import Base  # noqa: E402
from db.models import GlossaryTerm, Page, Region, Series, Volume, VolumeKind  # noqa: E402
from services import llm, pipeline, quality  # noqa: E402


def _volume(monkeypatch, pages):
    """One text volume at order 2 with these (source, translation) pages."""
    path = Path(tempfile.mkdtemp()) / "q.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}", poolclass=NullPool)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(pipeline, "SessionLocal", Session)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            series = Series(title="T", source_language="Japanese")
            s.add(series)
            await s.flush()
            vol = Volume(series_id=series.id, order=2, kind=VolumeKind.TEXT.value)
            s.add(vol)
            await s.flush()
            for i, (jp, en) in enumerate(pages):
                page = Page(volume_id=vol.id, index=i, image_key="")
                s.add(page)
                await s.flush()
                s.add(Region(page_id=page.id, order=0, japanese_text=jp, english_text=en))
            s.add_all([
                GlossaryTerm(series_id=series.id, kind="character", jp="リゼット",
                             en="Lizette Arnault", first_volume=1, first_page=0),
                GlossaryTerm(series_id=series.id, kind="term", jp="灰の教団",
                             en="Order of Ash", first_volume=1, first_page=0),
                GlossaryTerm(series_id=series.id, kind="term", jp="聖剣",
                             en="Holy Sword", first_volume=2, first_page=1)])
            await s.commit()
            return vol.id
    return Session, asyncio.run(go())


def test_glossary_report_finds_drift_and_new_terms(monkeypatch):
    Session, vid = _volume(monkeypatch, [
        ("リゼットは灰の教団に仕えた。", "Lizette served the Order of Ash."),
        ("灰の教団の聖剣。", "The Cult of Ashes and its holy blade."),
    ])

    async def go():
        async with Session() as s:
            return await quality.glossary_report(s, vid)
    report = asyncio.run(go())
    assert report["uses"] == 4 and report["misses"] == 2
    assert {d["jp"] for d in report["drift"]} == {"灰の教団", "聖剣"}
    assert report["drift"][0]["pages"] == [2]
    assert [t["jp"] for t in report["new_terms"]] == ["聖剣"]


def test_regression_sample_scores_and_writes_nothing(monkeypatch):
    Session, vid = _volume(monkeypatch, [("一", "The same words exactly."),
                                         ("二", "Completely different now.")])

    def translate(ctx, glossary, payload):
        same = payload[0]["page_index"] == 0
        text = "The same words exactly." if same else "Nothing alike at all here."
        return {"results": [{"page_index": payload[0]["page_index"],
                             "translations": {"r0": text}}], "new_terms": {}, "memory": {}}
    monkeypatch.setattr(llm, "translate_text", translate)

    async def go():
        async with Session() as s:
            return await quality.regression_sample(s, vid, pages=2)
    report = asyncio.run(go())
    assert report["pages"][0]["similarity"] == 1.0
    assert report["pages"][1]["similarity"] < 0.5
    assert 0.5 <= report["mean_similarity"] < 1.0
