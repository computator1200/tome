"""Series memory as a graph, not a glossary.

The glossary answers "what do we call this word". It cannot answer "who is this",
"how are they related", or "is the reader allowed to know that yet". Those three
questions are what translation of a long series actually needs: a name is easy to
keep consistent, a *relationship* is what makes volume 7 read like volume 7 and
not like a book that forgot volume 2.

Three tables (see db/models.py):

  memory_entities  one row per person / place / org / term, with the volume that
                   first introduces it
  memory_aliases   surface forms that resolve to an entity, normalised for
                   exact matching, each tagged with the volume it appears in
  memory_edges     typed relations, each tagged with the volume that establishes
                   it, so retrieval can hide what the reader has not read

Resolution is deterministic and cheap: normalise, look up an exact alias, and
only fall back to a conservative similarity merge inside the same series and the
same kind. The graph is written from what the translation model already returns
alongside the text, so building it costs no extra LLM calls.

The rule that makes it worth having: `build_context()` never returns an edge
whose `established_volume` is greater than the volume being translated. A volume
12 betrayal cannot leak into a volume 3 prompt, because it is not in the text we
send.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import MemoryAlias, MemoryEdge, MemoryEntity
from logging_config import get_logger

log = get_logger("memory")

# Honorifics that decorate a name without changing who it refers to.
_HONORIFICS = (
    "さん", "さま", "様", "くん", "君", "ちゃん", "先生", "先輩", "氏", "殿",
    "씨", "님", "군", "양", "-san", "-kun", "-chan", "-sama", "-senpai",
)

_KINDS = {"person", "place", "org", "term", "thing"}

# Pronouns and bare honorifics are not entities. Left in, they produce nodes like
# "僕 knows 九条櫻子" that mean nothing and make the graph look foolish.
_STOP = {"僕", "私", "俺", "おれ", "わたし", "あたし", "あなた", "君", "きみ", "彼",
         "彼女", "自分", "我々", "皆", "みんな", "母さん", "父さん", "お母さん",
         "お父さん", "i", "me", "you", "he", "she", "they", "we", "narrator"}

# Similarity threshold for merging two unknown-to-each-other names. Deliberately
# high: Hindsight merges at 0.6 and collapses "Aria" into "Ariane". A translator
# would rather keep two entities than merge a protagonist with her sister.
_MERGE_RATIO = 0.90


def norm(name: str) -> str:
    """The matching key: NFKC, casefolded, honorifics and punctuation stripped."""
    s = unicodedata.normalize("NFKC", name or "").strip().casefold()
    for h in _HONORIFICS:
        if s.endswith(h):
            s = s[: -len(h)].strip()
    s = s.strip(" .,:;!?\"'()[]{}<>「」『』・…")
    s = re.sub(r"[\s\u3000]+", " ", s)
    return s


def script_of(name: str) -> str:
    for ch in name or "":
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            return "ja"
        if 0xAC00 <= o <= 0xD7AF:
            return "ko"
        if 0x4E00 <= o <= 0x9FFF:
            return "zh"
        if "a" <= ch.lower() <= "z":
            return "latn"
        if 0x0400 <= o <= 0x04FF:
            return "cyrl"
        if 0x0600 <= o <= 0x06FF:
            return "arab"
    return "other"


def _kind(value: str | None) -> str:
    k = (value or "").strip().lower()
    aliases = {"character": "person", "people": "person", "location": "place",
               "organisation": "org", "organization": "org", "group": "org",
               "item": "thing", "object": "thing", "concept": "term"}
    k = aliases.get(k, k)
    return k if k in _KINDS else "term"


# A closed vocabulary keeps the graph queryable and stops the model inventing
# verbs ("is vaguely connected to"). Synonyms are folded onto one canonical name.
_RELATION_SYNONYMS = {
    "member": "member_of", "belongs_to": "member_of", "part_of": "member_of",
    "works_at": "works_for", "employed_by": "works_for", "servant_of": "serves",
    "lives_at": "lives_in", "resides_in": "lives_in", "resident_of": "lives_in",
    "born_at": "born_in", "birthplace": "born_in",
    "sister_of": "sibling_of", "brother_of": "sibling_of", "siblings": "sibling_of",
    "mother_of": "parent_of", "father_of": "parent_of", "son_of": "child_of",
    "daughter_of": "child_of", "wife_of": "married_to", "husband_of": "married_to",
    "located_at": "located_in", "situated_in": "located_in", "in": "located_in",
    "has": "owns", "possesses": "owns", "friend": "friend_of", "rival": "rival_of",
}
RELATIONS = {
    "serves", "works_for", "member_of", "sibling_of", "parent_of", "child_of",
    "married_to", "lives_in", "born_in", "located_in", "owns", "knows", "leads",
    "attends", "teaches", "friend_of", "rival_of", "kills", "betrays",
}

# Which kinds each relation is allowed to connect. Anything else is a model
# mistake, not a fact: "member of 鴻上百合子" (a person) is nonsense and is dropped.
_RELATION_TYPES = {
    "member_of": ({"person"}, {"org"}),
    "works_for": ({"person"}, {"org", "person"}),
    "serves": ({"person"}, {"person", "org"}),
    "lives_in": ({"person"}, {"place"}),
    "born_in": ({"person"}, {"place"}),
    "located_in": ({"place", "org"}, {"place"}),
    "sibling_of": ({"person"}, {"person"}),
    "parent_of": ({"person"}, {"person"}),
    "child_of": ({"person"}, {"person"}),
    "married_to": ({"person"}, {"person"}),
    "friend_of": ({"person"}, {"person"}),
    "rival_of": ({"person"}, {"person"}),
    "knows": ({"person"}, {"person"}),
    "leads": ({"person"}, {"org", "person"}),
    "attends": ({"person"}, {"org", "place"}),
    "teaches": ({"person"}, {"org", "place"}),
    "owns": ({"person", "org"}, {"thing", "term", "place"}),
    "kills": ({"person"}, {"person"}),
    "betrays": ({"person"}, {"person", "org"}),
}


def relation_allowed(relation: str, src_kind: str, dst_kind: str) -> bool:
    """Reject a relation whose endpoints are the wrong kind of thing.

    An unknown kind ("term" is what the extractor falls back to) cannot be
    judged, so it passes; a *known* contradiction is a model mistake and is
    dropped. That keeps "member of <a person>" out without throwing away edges
    whose endpoints were never classified.
    """
    allowed = _RELATION_TYPES.get(relation)
    if allowed is None:
        return True
    src, dst = _kind(src_kind), _kind(dst_kind)
    if src == "term" or dst == "term":
        return True
    return src in allowed[0] and dst in allowed[1]


def clean_relation(rel: str) -> str:
    """Relations are stored snake_case so they group: 'serves', 'born_in'."""
    r = unicodedata.normalize("NFKC", rel or "").strip().casefold()
    r = re.sub(r"[^a-z0-9]+", "_", r).strip("_")
    r = _RELATION_SYNONYMS.get(r, r)
    return r if r in RELATIONS else (r[:40] or "related_to")


async def _find_by_alias(session: AsyncSession, series_id: str, n: str) -> MemoryEntity | None:
    stmt = (select(MemoryEntity)
            .join(MemoryAlias, MemoryAlias.entity_id == MemoryEntity.id)
            .where(MemoryEntity.series_id == series_id, MemoryAlias.norm == n)
            .limit(1))
    return (await session.execute(stmt)).scalar_one_or_none()


async def _fuzzy_match(session: AsyncSession, series_id: str, n: str,
                       kind: str, script: str) -> MemoryEntity | None:
    """A conservative merge for a name we have never seen spelled this way."""
    # Two characters is a full Japanese or Korean family name; only guard the
    # Latin-alphabet case, where a two-letter match means nothing.
    if len(n) < 4 and script_of(n) not in {"ja", "ko", "zh"}:
        return None
    stmt = (select(MemoryEntity, MemoryAlias)
            .join(MemoryAlias, MemoryAlias.entity_id == MemoryEntity.id)
            .where(MemoryEntity.series_id == series_id, MemoryEntity.kind == kind))
    rows = (await session.execute(stmt)).all()
    best, best_ratio = None, 0.0
    for entity, alias in rows:
        if alias.script and script and alias.script != script:
            continue
        ratio = difflib.SequenceMatcher(None, n, alias.norm).ratio()
        if ratio > best_ratio:
            best, best_ratio = entity, ratio
    if best is not None and best_ratio >= _MERGE_RATIO:
        return best

    # A Japanese or Korean name is routinely shortened to the family name:
    # 館脇君 -> 館脇 -> 館脇正太郎. Treat a short form that is a prefix of a longer
    # one, in the same kind and script, as the same entity. Kept deliberately
    # tight (>=2 chars, at most 6 chars shorter) so 桜 and 桜井 stay apart.
    # Restricted to CJK on purpose. In Japanese and Korean the family name alone
    # is the same person; in Latin script "Aria" is a prefix of "Ariane" and they
    # are two different characters.
    if script not in {"ja", "ko", "zh"}:
        return None
    for entity, alias in rows:
        if alias.script and alias.script != script:
            continue
        short, long_ = (n, alias.norm) if len(n) <= len(alias.norm) else (alias.norm, n)
        if len(short) < 2 or len(long_) - len(short) > 6:
            continue
        # 館脇 (family name) is a prefix, 櫻子 (given name) is a suffix; both are
        # the same person once the honorific is gone.
        if long_.startswith(short) or long_.endswith(short):
            return entity
    return None


async def resolve_or_create(session: AsyncSession, series_id: str, name: str,
                            kind: str = "term", volume: int = 1) -> MemoryEntity | None:
    """Find the entity this surface form refers to, creating it if it is new."""
    name = (name or "").strip()
    n = norm(name)
    if not n or len(n) > 120 or n in _STOP:
        return None
    kind = _kind(kind)
    script = script_of(name)

    entity = await _find_by_alias(session, series_id, n)
    if entity is None:
        entity = await _fuzzy_match(session, series_id, n, kind, script)
        if entity is not None:
            log.info(f"memory: merged new form {name!r} into {entity.canonical!r}")
    if entity is None:
        entity = MemoryEntity(series_id=series_id, kind=kind, canonical=name,
                              first_volume=max(1, int(volume or 1)))
        session.add(entity)
        await session.flush()

    # Query the aliases rather than reading entity.aliases: a freshly created
    # entity has no relationship loaded yet, and touching it would emit a lazy
    # load that async SQLAlchemy refuses (MissingGreenlet).
    known = set((await session.execute(
        select(MemoryAlias.norm).where(MemoryAlias.entity_id == entity.id))).scalars().all())
    if n not in known:
        session.add(MemoryAlias(entity_id=entity.id, alias=name, norm=n,
                                volume=max(1, int(volume or 1)), script=script))
    if entity.first_volume > (volume or 1):
        entity.first_volume = max(1, int(volume or 1))
    return entity


async def record_extraction(session: AsyncSession, series_id: str, volume: int,
                            payload: dict | None, page_id: str | None = None) -> dict:
    """Apply one page's entity/relation extraction to the graph.

    The model returns this next to the translation, so the graph is built as a
    side effect of work we were doing anyway.
    """
    payload = payload or {}
    added_entities = added_edges = 0
    made: dict[str, MemoryEntity] = {}

    for ent in (payload.get("entities") or [])[:40]:
        if isinstance(ent, str):
            ent = {"name": ent}
        if not isinstance(ent, dict):
            continue
        name = ent.get("name") or ent.get("jp") or ""
        entity = await resolve_or_create(session, series_id, name,
                                          ent.get("kind") or "term", volume)
        if entity is None:
            continue
        made[name] = entity
        made[entity.canonical] = entity
        if entity.id and entity.first_volume == volume:
            added_entities += 1
        for alias in (ent.get("aliases") or [])[:6]:
            if isinstance(alias, str) and alias.strip():
                await resolve_or_create(session, series_id, alias,
                                        entity.kind, volume)

    for rel in (payload.get("relations") or [])[:40]:
        if not isinstance(rel, dict):
            continue
        src_name = rel.get("src") or rel.get("source") or rel.get("from") or ""
        dst_name = rel.get("dst") or rel.get("target") or rel.get("to") or ""
        relation = clean_relation(rel.get("relation") or rel.get("rel") or "")
        if not src_name or not dst_name or not relation:
            continue
        src = made.get(src_name) or await resolve_or_create(
            session, series_id, src_name, "term", volume)
        dst = made.get(dst_name) or await resolve_or_create(
            session, series_id, dst_name, "term", volume)
        if src is None or dst is None or src.id == dst.id:
            continue
        if not relation_allowed(relation, src.kind, dst.kind):
            continue
        exists = (await session.execute(
            select(MemoryEdge.id).where(
                MemoryEdge.series_id == series_id,
                MemoryEdge.src_id == src.id,
                MemoryEdge.dst_id == dst.id,
                MemoryEdge.relation == relation,
            ).limit(1))).scalar_one_or_none()
        if exists:
            # The same relation restated in a later volume must not unlock the
            # knowledge earlier: keep the earliest volume that established it.
            edge = (await session.execute(select(MemoryEdge).where(
                MemoryEdge.id == exists))).scalar_one()
            if edge.established_volume > volume:
                edge.established_volume = volume
            continue
        session.add(MemoryEdge(series_id=series_id, src_id=src.id, dst_id=dst.id,
                               relation=relation, established_volume=max(1, int(volume or 1)),
                               page_id=page_id))
        added_edges += 1

    return {"entities": added_entities, "edges": added_edges}


async def build_context(session: AsyncSession, series_id: str, volume_order: int,
                        max_entities: int = 80, max_edges: int = 60) -> dict:
    """Everything a reader at `volume_order` is allowed to know.

    This is the whole point of the graph: the filter is the reader's position,
    not the series' latest state.
    """
    volume_order = max(1, int(volume_order or 1))
    ent_rows = (await session.execute(
        select(MemoryEntity)
        .where(MemoryEntity.series_id == series_id,
               MemoryEntity.first_volume <= volume_order)
        .order_by(MemoryEntity.first_volume, MemoryEntity.canonical)
        .limit(max_entities))).scalars().all()

    names = {e.id: e.canonical for e in ent_rows}
    edges = []
    if names:
        edge_rows = (await session.execute(
            select(MemoryEdge)
            .where(MemoryEdge.series_id == series_id,
                   MemoryEdge.established_volume <= volume_order)
            .order_by(MemoryEdge.established_volume)
            .limit(max_edges))).scalars().all()
        wanted = {e.src_id for e in edge_rows} | {e.dst_id for e in edge_rows}
        if wanted:
            lookup = {e.id: e.canonical for e in (await session.execute(
                select(MemoryEntity).where(MemoryEntity.id.in_(wanted)))).scalars().all()}
            for edge in edge_rows:
                if edge.src_id in lookup and edge.dst_id in lookup:
                    edges.append({
                        "src": lookup[edge.src_id],
                        "relation": edge.relation,
                        "dst": lookup[edge.dst_id],
                        "volume": edge.established_volume,
                    })

    return {
        "entities": [{"name": e.canonical, "kind": e.kind, "volume": e.first_volume}
                     for e in ent_rows],
        "edges": edges,
        "volume": volume_order,
    }


def render_context(ctx: dict) -> str:
    """The prompt block. Short on purpose: it competes with the page for tokens."""
    if not ctx or not (ctx.get("entities") or ctx.get("edges")):
        return ""
    out = [f"What is known by volume {ctx['volume']} (do not reveal anything beyond it):"]
    if ctx.get("entities"):
        by_kind: dict[str, list[str]] = {}
        for e in ctx["entities"]:
            by_kind.setdefault(e["kind"], []).append(e["name"])
        for kind, names in by_kind.items():
            out.append(f"- {kind}s: " + ", ".join(names[:24]))
    if ctx.get("edges"):
        out.append("Relations:")
        for e in ctx["edges"][:40]:
            out.append(f"- {e['src']} {e['relation'].replace('_', ' ')} {e['dst']} (vol {e['volume']})")
    return "\n".join(out) + "\n\n"


async def stats(session: AsyncSession, series_id: str) -> dict:
    entities = (await session.execute(
        select(func.count(MemoryEntity.id))
        .where(MemoryEntity.series_id == series_id))).scalar() or 0
    edges = (await session.execute(
        select(func.count(MemoryEdge.id))
        .where(MemoryEdge.series_id == series_id))).scalar() or 0
    aliases = (await session.execute(
        select(func.count(MemoryAlias.id))
        .join(MemoryEntity, MemoryEntity.id == MemoryAlias.entity_id)
        .where(MemoryEntity.series_id == series_id))).scalar() or 0
    return {"entities": int(entities), "edges": int(edges), "aliases": int(aliases)}


async def seed_from_glossary(session: AsyncSession, series_id: str) -> int:
    """Give every existing glossary term an entity, so old series are not empty.

    No LLM involved: a glossary row already says what the thing is called in both
    languages. Relations still have to come from text.
    """
    from db.models import GlossaryTerm

    rows = (await session.execute(
        select(GlossaryTerm).where(GlossaryTerm.series_id == series_id))).scalars().all()
    added = 0
    for term in rows:
        before = await _find_by_alias(session, series_id, norm(term.jp))
        await resolve_or_create(session, series_id, term.jp, term.kind or "term", 1)
        if before is None:
            added += 1
        if term.en:
            await resolve_or_create(session, series_id, term.en, term.kind or "term", 1)
    await session.commit()
    return added
