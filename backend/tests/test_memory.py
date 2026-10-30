"""Graph memory: the rules that must hold, tested without a server.

These run against a throwaway in-memory SQLite database, so they exercise the
same SQLAlchemy models and queries the app uses.
"""
import asyncio
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import Base  # noqa: E402
from db.models import Series  # noqa: E402
from services import memory  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


async def _fresh():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    session = Session()
    series = Series(title="Test", source_language="Japanese", target_language="English")
    session.add(series)
    await session.commit()
    return engine, session, series


# --- normalisation -----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("リゼット", "リゼット"),
    ("リゼットさん", "リゼット"),
    ("Lizette", "lizette"),
    ("ＬＩＺＥＴＴＥ", "lizette"),
    (" 館脇 正太郎 ", "館脇 正太郎"),
    ("館脇君", "館脇"),
])
def test_norm_strips_honorifics_and_width(raw, expected):
    assert memory.norm(raw) == expected


def test_script_detection():
    assert memory.script_of("リゼット") == "ja"
    assert memory.script_of("館脇") == "zh"      # kanji alone is ambiguous, kept apart
    assert memory.script_of("Lizette") == "latn"


# --- resolution --------------------------------------------------------------

def test_alias_folds_into_the_same_entity():
    async def go():
        engine, session, series = await _fresh()
        a = await memory.resolve_or_create(session, series.id, "リゼット", "person", 1)
        await session.commit()
        b = await memory.resolve_or_create(session, series.id, "リゼットさん", "person", 2)
        await session.commit()
        stats = await memory.stats(session, series.id)
        await engine.dispose()
        return a.id, b.id, stats

    a_id, b_id, stats = _run(go())
    assert a_id == b_id, "an honorific must not create a second person"
    # The honorific is decoration: it resolves to the same norm, so the graph
    # stores one alias, not two spellings of the same name.
    assert stats == {"entities": 1, "edges": 0, "aliases": 1}


def test_surname_shortens_to_the_same_person():
    async def go():
        engine, session, series = await _fresh()
        full = await memory.resolve_or_create(session, series.id, "館脇正太郎", "person", 1)
        await session.commit()
        short = await memory.resolve_or_create(session, series.id, "館脇君", "person", 5)
        await session.commit()
        count = (await memory.stats(session, series.id))["entities"]
        await engine.dispose()
        return full.id, short.id, count

    full_id, short_id, count = _run(go())
    assert full_id == short_id
    assert count == 1


def test_different_people_stay_apart():
    async def go():
        engine, session, series = await _fresh()
        await memory.resolve_or_create(session, series.id, "Aria", "person", 1)
        await memory.resolve_or_create(session, series.id, "Ariane", "person", 2)
        await session.commit()
        count = (await memory.stats(session, series.id))["entities"]
        await engine.dispose()
        return count

    assert _run(go()) == 2, "0.9 similarity must not collapse two characters"


# --- scoping -----------------------------------------------------------------

def test_edges_are_invisible_before_the_volume_that_establishes_them():
    async def go():
        engine, session, series = await _fresh()
        await memory.record_extraction(session, series.id, 2, {
            "entities": [{"name": "Lizette", "kind": "person"},
                         {"name": "Order of Ash", "kind": "org"}],
            "relations": [{"src": "Lizette", "relation": "member_of", "dst": "Order of Ash"}],
        })
        await memory.record_extraction(session, series.id, 12, {
            "entities": [{"name": "Ashen Court", "kind": "org"}],
            "relations": [{"src": "Lizette", "relation": "betrays", "dst": "Ashen Court"}],
        })
        await session.commit()
        early = await memory.build_context(session, series.id, 3)
        late = await memory.build_context(session, series.id, 12)
        await engine.dispose()
        return early, late

    early, late = _run(go())
    early_rels = {(e["src"], e["relation"], e["dst"]) for e in early["edges"]}
    late_rels = {(e["src"], e["relation"], e["dst"]) for e in late["edges"]}
    assert ("Lizette", "member_of", "Order of Ash") in early_rels
    assert ("Lizette", "betrays", "Ashen Court") not in early_rels, "volume 12 leaked into volume 3"
    assert ("Lizette", "betrays", "Ashen Court") in late_rels


def test_render_context_is_short_and_labelled():
    ctx = {"volume": 3,
           "entities": [{"name": "Lizette", "kind": "person", "volume": 1}],
           "edges": [{"src": "Lizette", "relation": "born_in", "dst": "Port Seren", "volume": 1}]}
    text = memory.render_context(ctx)
    assert "volume 3" in text
    assert "Lizette born in Port Seren" in text


def test_relation_names_are_normalised():
    assert memory.clean_relation("Born In") == "born_in"
    assert memory.clean_relation("serves  ") == "serves"
    assert memory.clean_relation("") == "related_to"
