"""Graph memory: one test for each defect a review against the literature found.

Numbered as in that review: S for spoiler safety (a later fact reaching an
earlier prompt), C for correctness. Run against an in-memory SQLite database.
"""
import asyncio
import sys
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import Base  # noqa: E402
from db.models import GlossaryTerm, Page, Region, Series, Volume  # noqa: E402
from services import memory  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


async def _graph(*extractions):
    """A fresh series with these (volume, page, payload) extractions recorded."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    series = Series(title="Test", source_language="Japanese", target_language="English")
    session.add(series)
    await session.commit()
    for volume, page, payload in extractions:
        await memory.record_extraction(session, series.id, volume, payload, page=page)
    await session.commit()
    return engine, session, series


def _names(ctx):
    return {e["name"] for e in ctx["entities"]}


def _rels(ctx):
    return {(e["src"], e["relation"], e["dst"]) for e in ctx["edges"]}


def _person(name, aliases=()):
    return {"name": name, "kind": "person", "aliases": list(aliases)}


# --- C1: aliases ----------------------------------------------------------------

def test_c1_listed_aliases_join_their_entity():
    async def go():
        engine, session, series = await _graph(
            (1, 0, {"entities": [_person("リゼット", ["リズ", "灰の聖女"])]}))
        stats = await memory.stats(session, series.id)
        await engine.dispose()
        return stats
    assert _run(go()) == {"entities": 1, "edges": 0, "aliases": 3}


# --- C2: false merges -------------------------------------------------------------

@pytest.mark.parametrize("first,second", [
    ("九条櫻子", "九条総司"),                    # siblings share a family name
    ("アリア", "アリアンヌ"),                    # katakana given names
    ("リゼット・アルノー", "ジャン・アルノー"),  # Western order, same family name
])
def test_c2_different_people_with_shared_parts_stay_apart(first, second):
    async def go():
        engine, session, series = await _graph(
            (1, 0, {"entities": [_person(first)]}), (2, 0, {"entities": [_person(second)]}))
        n = (await memory.stats(session, series.id))["entities"]
        await engine.dispose()
        return n
    assert _run(go()) == 2


def test_c2_a_family_name_shared_by_two_people_is_not_guessed():
    async def go():
        engine, session, series = await _graph(
            (1, 0, {"entities": [_person("九条櫻子")]}),
            (1, 1, {"entities": [_person("九条総司")]}))
        e = await memory.resolve_or_create(session, series.id, "九条", "person", 2, 0)
        await engine.dispose()
        return e
    assert _run(go()) is None


def test_c2_a_unique_family_name_still_resolves():
    async def go():
        engine, session, series = await _graph((1, 0, {"entities": [_person("館脇正太郎")]}))
        full = await memory.resolve_or_create(session, series.id, "館脇正太郎", "person", 1, 0)
        short = await memory.resolve_or_create(session, series.id, "館脇君", "person", 2, 0)
        await engine.dispose()
        return full.id, short.id
    a, b = _run(go())
    assert a == b


def test_c2_orgs_do_not_merge_by_prefix():
    async def go():
        engine, session, series = await _graph(
            (1, 0, {"entities": [{"name": "騎士団", "kind": "org"}]}),
            (1, 1, {"entities": [{"name": "白銀騎士団", "kind": "org"},
                                 {"name": "黒鉄騎士団", "kind": "org"}]}))
        n = (await memory.stats(session, series.id))["entities"]
        await engine.dispose()
        return n
    assert _run(go()) == 3


def test_c2_two_names_on_one_page_are_two_entities():
    async def go():
        engine, session, series = await _graph(
            (2, 0, {"entities": [_person("館脇"), _person("館脇正太郎")],
                    "relations": [{"src": "館脇", "relation": "sibling_of",
                                   "dst": "館脇正太郎"}]}))
        ctx = await memory.build_context(session, series.id, 2)
        await engine.dispose()
        return ctx
    ctx = _run(go())
    assert len(ctx["entities"]) == 2, "a name beside its own short form is someone else"
    assert ctx["edges"], "and the sibling edge between them survives"


# --- C3: relation endpoints -------------------------------------------------------

def test_c3_a_relation_endpoint_resolves_to_the_person():
    async def go():
        engine, session, series = await _graph((1, 0, {
            "entities": [_person("リゼット・アルノー"), {"name": "灰の教団", "kind": "org"}],
            "relations": [{"src": "リゼット", "relation": "serves", "dst": "灰の教団"}]}))
        ctx = await memory.build_context(session, series.id, 1)
        n = (await memory.stats(session, series.id))["entities"]
        await engine.dispose()
        return ctx, n
    ctx, n = _run(go())
    assert n == 2, "no phantom term entity for the short form"
    assert ("リゼット・アルノー", "serves", "灰の教団") in _rels(ctx)


# --- C4, C5: normalisation --------------------------------------------------------

@pytest.mark.parametrize("a,b", [("神様", "神殿"), ("源氏", "源"), ("宮殿", "宮")])
def test_c4_stripping_an_honorific_never_leaves_a_different_word(a, b):
    assert memory.norm(a) != memory.norm(b)


@pytest.mark.parametrize("word", ["お母さん", "母さん", "僕"])
def test_c4_the_stop_list_takes_effect(word):
    assert memory._is_stop(word)


def test_c4_quoted_names_lose_their_honorific():
    assert memory.norm("「リゼットさん」") == "リゼット"


@pytest.mark.parametrize("a,b", [
    ("九条 櫻子", "九条櫻子"), ("リゼット＝アルノー", "リゼット・アルノー"),
    ("髙橋", "高橋"), ("ｱﾘｱ", "アリア"), ("リゼット一", "リゼットー"),
    ("علي", "علی"),
])
def test_c5_spellings_of_one_name_share_a_key(a, b):
    assert memory.norm(a) == memory.norm(b)


# --- C6: what reaches the prompt --------------------------------------------------

def test_c6_names_in_the_text_come_first_with_their_renderings():
    async def go():
        payloads = [(1, i, {"entities": [_person(f"人物{i:02d}号")]}) for i in range(60)]
        engine, session, series = await _graph(*payloads)
        await memory.record_renderings(session, series.id, {"人物59号": "Person 59"})
        await session.commit()
        ctx = await memory.build_context(session, series.id, 2, texts=["人物59号が来た。"],
                                         max_entities=5)
        await engine.dispose()
        return ctx
    ctx = _run(go())
    assert ctx["entities"][0]["name"] == "人物59号"
    assert "人物59号 -> Person 59" in memory.render_context(ctx)
    assert len(ctx["entities"]) == 5


# --- C7: a bad payload, and re-runs -----------------------------------------------

@pytest.mark.parametrize("payload", [
    {"entities": [{"name": ["not", "a", "string"]}, "Lizette"]},
    {"entities": "nonsense", "relations": [{"src": None, "relation": 3}]},
    ["not", "a", "dict"],
])
def test_c7_malformed_payloads_are_ignored(payload):
    async def go():
        engine, session, series = await _graph((1, 0, payload))
        stats = await memory.stats(session, series.id)
        await engine.dispose()
        return stats
    assert _run(go())["edges"] == 0


def test_c7_rerun_is_idempotent_and_restating_earlier_moves_it_earlier():
    payload = {"entities": [_person("Lizette", ["Liz"]), {"name": "Port Seren", "kind": "place"}],
               "relations": [{"src": "Lizette", "relation": "born_in", "dst": "Port Seren"}]}

    async def go():
        engine, session, series = await _graph((2, 5, payload), (2, 5, payload), (1, 3, payload))
        stats = await memory.stats(session, series.id)
        ctx = await memory.build_context(session, series.id, 1, page=3)
        await engine.dispose()
        return stats, ctx
    stats, ctx = _run(go())
    assert stats == {"entities": 2, "edges": 1, "aliases": 3}
    assert ctx["edges"]


# --- C8: relation noise -----------------------------------------------------------

def test_c8_symmetric_relations_are_stored_once_and_unknown_verbs_dropped():
    async def go():
        engine, session, series = await _graph((1, 0, {
            "entities": [_person("Lizette"), _person("Jean")],
            "relations": [{"src": "Lizette", "relation": "sibling_of", "dst": "Jean"},
                          {"src": "Jean", "relation": "sibling_of", "dst": "Lizette"},
                          {"src": "Jean", "relation": "is vaguely connected to",
                           "dst": "Lizette"}]}))
        n = (await memory.stats(session, series.id))["edges"]
        await engine.dispose()
        return n
    assert _run(go()) == 1


# --- S1 to S5: nothing later reaches an earlier prompt ------------------------------

def test_s1_the_glossary_has_the_same_horizon():
    async def go():
        engine, session, series = await _graph()
        session.add_all([
            GlossaryTerm(series_id=series.id, kind="character", jp="リゼット", en="Lizette",
                         first_volume=1, first_page=0),
            GlossaryTerm(series_id=series.id, kind="term", jp="灰の聖女", en="Ash Saint",
                         first_volume=12, first_page=0)])
        await session.commit()
        early = await memory.glossary_for(session, series.id, 3)
        late = await memory.glossary_for(session, series.id, 12)
        await engine.dispose()
        return early, late
    early, late = _run(go())
    assert [t["jp"] for t in early] == ["リゼット"]
    assert {t["jp"] for t in late} == {"リゼット", "灰の聖女"}


def test_s2_reordering_volumes_moves_their_facts():
    async def go():
        engine, session, series = await _graph((1, 0, {
            "entities": [_person("Lizette"), {"name": "Ashen Court", "kind": "org"}],
            "relations": [{"src": "Lizette", "relation": "betrays", "dst": "Ashen Court"}]}))
        # That volume was uploaded first but is really volume 12.
        await memory.renumber_volumes(session, series.id, {1: 12, 12: 1})
        await session.commit()
        ctx = await memory.build_context(session, series.id, 3)
        await engine.dispose()
        return ctx
    ctx = _run(go())
    assert not ctx["edges"] and not ctx["entities"]


def test_s3_a_later_name_is_not_shown_early():
    async def go():
        engine, session, series = await _graph(
            (12, 0, {"entities": [_person("リゼット", ["灰の聖女"])]}),
            (3, 0, {"entities": [_person("リゼット")]}))
        ctx = await memory.build_context(session, series.id, 3)
        await engine.dispose()
        return ctx
    ctx = _run(go())
    assert _names(ctx) == {"リゼット"}
    assert ctx["entities"][0]["aliases"] == [], "the volume 12 title is not known at volume 3"


def test_s4_undated_rows_are_dated_from_the_text_or_hidden():
    async def go():
        engine, session, series = await _graph()
        for order, text in ((1, "リゼットが来た"), (12, "灰の聖女が現れた")):
            vol = Volume(series_id=series.id, order=order)
            session.add(vol)
            await session.flush()
            page = Page(volume_id=vol.id, index=0, image_key="")
            session.add(page)
            await session.flush()
            session.add(Region(page_id=page.id, order=0, japanese_text=text))
        session.add_all([GlossaryTerm(series_id=series.id, jp="リゼット", en="Lizette"),
                         GlossaryTerm(series_id=series.id, jp="灰の聖女", en="Ash Saint"),
                         GlossaryTerm(series_id=series.id, jp="未登場", en="Unseen")])
        await session.commit()
        await memory.seed_from_glossary(session, series.id)
        gloss = await memory.glossary_for(session, series.id, 3)
        ctx = await memory.build_context(session, series.id, 3)
        await engine.dispose()
        return gloss, ctx
    gloss, ctx = _run(go())
    assert [t["jp"] for t in gloss] == ["リゼット"]
    assert _names(ctx) == {"リゼット"}
    assert ctx["entities"][0]["rendering"] == "Lizette"


def test_s5_a_rerun_does_not_see_its_own_later_pages():
    async def go():
        engine, session, series = await _graph((3, 40, {
            "entities": [_person("Lizette"), {"name": "Ashen Court", "kind": "org"}],
            "relations": [{"src": "Lizette", "relation": "betrays", "dst": "Ashen Court"}]}))
        page1 = await memory.build_context(session, series.id, 3, page=0)
        page41 = await memory.build_context(session, series.id, 3, page=40)
        await engine.dispose()
        return page1, page41
    page1, page41 = _run(go())
    assert not page1["edges"] and page41["edges"]


# --- manual corrections ------------------------------------------------------------

def test_manual_merge_split_and_delete():
    async def go():
        engine, session, series = await _graph(
            (1, 0, {"entities": [_person("Lizette"), {"name": "Port Seren", "kind": "place"}],
                    "relations": [{"src": "Lizette", "relation": "born_in",
                                   "dst": "Port Seren"}]}),
            (2, 0, {"entities": [_person("Liz")]}))
        ents = {e["name"]: e for e in await memory.entity_details(session, series.id)}
        await memory.merge_entities(session, series.id, ents["Lizette"]["id"], ents["Liz"]["id"])
        merged = {e["name"]: e for e in await memory.entity_details(session, series.id)}
        liz = next(a for a in merged["Lizette"]["aliases"] if a["alias"] == "Liz")
        await memory.split_alias(session, series.id, liz["id"])
        after_split = (await memory.stats(session, series.id))["entities"]
        ctx = await memory.build_context(session, series.id, 2)
        await memory.delete_edge(session, series.id, ctx["edges"][0]["id"])
        await memory.delete_entity(session, series.id, merged["Port Seren"]["id"])
        final = await memory.stats(session, series.id)
        await engine.dispose()
        return len(merged), after_split, final
    merged, after_split, final = _run(go())
    assert merged == 2 and after_split == 3
    assert final == {"entities": 2, "edges": 0, "aliases": 2}


def test_the_prompt_offers_exactly_the_vocabulary_the_graph_accepts():
    import re
    from services import llm
    listed = set(re.findall(r"[a-z_]+", llm._MEMORY_RULES.split("relations:")[1]))
    assert listed == memory.RELATIONS


def test_s2_the_reorder_tool_moves_memory_with_the_volumes():
    from services import maintenance

    async def go():
        engine, session, series = await _graph()
        vols = []
        for order, label in ((1, "Twelve"), (2, "One")):
            v = Volume(series_id=series.id, order=order, label=label)
            session.add(v)
            vols.append(v)
        await session.commit()
        # "Twelve" was uploaded first and translated as order 1.
        await memory.record_extraction(session, series.id, 1, {
            "entities": [_person("Lizette"), {"name": "Ashen Court", "kind": "org"}],
            "relations": [{"src": "Lizette", "relation": "betrays", "dst": "Ashen Court"}]}, page=0)
        await session.commit()
        await maintenance.reorder_volumes(session, series.id, ["One", "Twelve"])
        first = await memory.build_context(session, series.id, 1)
        second = await memory.build_context(session, series.id, 2)
        await engine.dispose()
        return first, second
    first, second = _run(go())
    assert not first["edges"] and second["edges"]
