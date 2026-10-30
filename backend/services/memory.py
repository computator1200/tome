"""Series memory as a graph, not a glossary.

The glossary answers "what do we call this word". It cannot answer "who is this",
"how are they related", or "is the reader allowed to know that yet". Those three
questions are what translation of a long series actually needs: a name is easy to
keep consistent, a *relationship* is what makes volume 7 read like volume 7 and
not like a book that forgot volume 2.

Three tables (see db/models.py), plus the glossary:

  memory_entities  one row per person / place / org / term, with its English
                   rendering once the translation has settled on one
  memory_aliases   surface forms that resolve to an entity, normalised for exact
                   matching, each stamped with the position it was first seen at
  memory_edges     typed relations, each stamped with the position that
                   establishes it

A position is (volume order, page index). Everything the graph shows a prompt is
filtered on it: a prompt for page p of volume N sees only what was seen before
or on that page. That holds across volumes (a volume 12 betrayal cannot reach a
volume 3 prompt) and inside one (a re-run of volume 3 does not see its own last
chapter on page 1). Facts whose page is unknown are placed at the end of their
volume, and glossary rows never found in the source text are hidden, so the
unknown case always errs on the side of not telling.

Resolution is deterministic and cheap, following the name-clustering rules of
Vala et al. (EMNLP 2015): normalise, look up an exact alias, then a conservative
similarity merge, then a short-form rule that only fires when exactly one person
fits, and never between two different names on the same page. The graph is
written from what the translation model already returns alongside the text, so
building it costs no extra model calls. The prompt block follows DelTA (Wang et
al., ICLR 2025): the names that occur in the text being translated come first,
with their renderings.
"""
from __future__ import annotations

import difflib
import re
import unicodedata

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    AppSetting, GlossaryTerm, MemoryAlias, MemoryEdge, MemoryEntity, Page, Region, Volume,
)
from logging_config import get_logger

log = get_logger("memory")

# --- positions ----------------------------------------------------------------

# A page index meaning "the end of the volume". A fact whose page is not known,
# or that came from a batch of pages, is visible from the end of its volume.
END = 1_000_000
# A volume meaning "never seen in the source text". Hidden from every prompt
# until a translation sees it and lowers it.
HIDDEN = 1_000_000
# Rows written before positions existed. ensure_positions() resolves them.
UNKNOWN = -1


def _visible(vol_col, page_col, volume: int, page: int):
    return or_(vol_col < volume, and_(vol_col == volume, page_col <= page))


def _earlier(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a < b


# --- normalisation ------------------------------------------------------------

# Honorifics that decorate a name without changing who it refers to. Longest
# first, so 殿下 is stripped as a whole and not as 殿.
_HONORIFICS = tuple(sorted((
    "さん", "さま", "様", "くん", "君", "ちゃん", "先生", "先輩", "氏", "殿", "殿下",
    "嬢", "卿", "姫", "씨", "님", "군", "양",
    "-san", "-kun", "-chan", "-sama", "-senpai", "-dono",
), key=len, reverse=True))

_KINDS = {"person", "place", "org", "term", "thing"}

# Pronouns, kinship words and bare honorifics are not entities. Left in, they
# produce nodes like "僕 knows 九条櫻子" that mean nothing.
_STOP = {"僕", "私", "俺", "おれ", "わたし", "あたし", "あなた", "君", "きみ", "彼",
         "彼女", "自分", "我々", "皆", "みんな", "母さん", "父さん", "お母さん",
         "お父さん", "母", "父", "兄", "姉", "弟", "妹", "お兄ちゃん", "お姉ちゃん",
         "i", "me", "you", "he", "she", "they", "we", "narrator"}

# Similarity threshold for merging two names we have never seen linked. Deliberately
# high: at 0.6 "Aria" and "Ariane" merge, and a translator would rather keep two
# entities than merge a protagonist with her sister.
_MERGE_RATIO = 0.90

_EDGE_PUNCT = " .,:;!?\"'()[]{}<>「」『』【】《》〈〉“”‘’…・"
_SEPARATORS = re.compile(r"[･·•‧∙＝=゠]")
_KANA = r"゠-ヿ"
_CJK = r"぀-ヿ一-鿿가-힯"
# 一 (the kanji for one), a hyphen or a dash read inside katakana is almost
# always an OCR or input slip for the long-vowel mark ー.
_KATAKANA_DASH = re.compile(rf"(?<=[{_KANA}])[一\-‐‑–—―─－](?=[{_KANA}]|$)")
_CJK_SPACE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")
# Old and variant kanji that NFKC leaves alone but that name the same person.
_VARIANTS = str.maketrans({
    "髙": "高", "﨑": "崎", "邊": "辺", "邉": "辺", "齋": "斎", "齊": "斉", "濱": "浜",
    "濵": "浜", "嶋": "島", "澤": "沢", "國": "国", "櫻": "桜", "廣": "広", "眞": "真",
    "德": "徳", "惠": "恵", "龍": "竜",
    # Persian and Arabic letter pairs that are the same letter in names.
    "ي": "ی", "ك": "ک", "‌": "", "ـ": "",
})


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3040 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF or 0xAC00 <= o <= 0xD7AF


def norm(name: str, honorifics: bool = True) -> str:
    """The matching key: NFKC, casefolded, punctuation, separators and variant
    forms unified, and one trailing honorific stripped.

    An honorific is only stripped when what is left is still a name: 神様 (god)
    and 神殿 (temple) both end in an honorific character, and stripping either
    would leave 神 and merge the two.
    """
    s = unicodedata.normalize("NFKC", name or "").strip().casefold()
    s = s.strip(_EDGE_PUNCT)
    s = _SEPARATORS.sub("・", s)
    s = _KATAKANA_DASH.sub("ー", s)
    s = _CJK_SPACE.sub("", s)
    s = re.sub(r"[\s　]+", " ", s).translate(_VARIANTS)
    if honorifics:
        for h in _HONORIFICS:
            if s.endswith(h):
                rest = s[: -len(h)].strip()
                cjk = any(_is_cjk(c) for c in rest)
                if len(rest) >= (2 if cjk else 1):
                    s = rest
                break
    return s.strip(_EDGE_PUNCT)


def _is_stop(name: str) -> bool:
    return norm(name) in _STOP or norm(name, honorifics=False) in _STOP


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


# --- relations ----------------------------------------------------------------

# A closed vocabulary keeps the graph queryable and stops the model inventing
# verbs ("is vaguely connected to"). Synonyms are folded onto one canonical name;
# anything else is dropped.
_RELATION_SYNONYMS = {
    "member": "member_of", "belongs_to": "member_of", "part_of": "member_of",
    "works_at": "works_for", "employed_by": "works_for", "servant_of": "serves",
    "lives_at": "lives_in", "resides_in": "lives_in", "resident_of": "lives_in",
    "born_at": "born_in", "birthplace": "born_in",
    "sister_of": "sibling_of", "brother_of": "sibling_of", "siblings": "sibling_of",
    "mother_of": "parent_of", "father_of": "parent_of", "son_of": "child_of",
    "daughter_of": "child_of", "wife_of": "married_to", "husband_of": "married_to",
    "spouse_of": "married_to", "located_at": "located_in", "situated_in": "located_in",
    "in": "located_in", "has": "owns", "possesses": "owns", "friend": "friend_of",
    "friends_with": "friend_of", "rival": "rival_of", "killed": "kills",
    "murders": "kills", "betrayed": "betrays", "leader_of": "leads",
    "student_at": "attends", "teacher_at": "teaches",
}
RELATIONS = {
    "serves", "works_for", "member_of", "sibling_of", "parent_of", "child_of",
    "married_to", "lives_in", "born_in", "located_in", "owns", "knows", "leads",
    "attends", "teaches", "friend_of", "rival_of", "kills", "betrays",
}
# The same fact whichever way round it is stated; stored once.
SYMMETRIC = {"sibling_of", "married_to", "friend_of", "rival_of", "knows"}

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
    """Reject a relation outside the vocabulary or between the wrong kinds.

    An endpoint whose kind is still the fallback `term` cannot be judged, so it
    passes; a *known* contradiction is a model mistake and is dropped.
    """
    allowed = _RELATION_TYPES.get(relation)
    if allowed is None:
        return False
    src, dst = _kind(src_kind), _kind(dst_kind)
    if src == "term" or dst == "term":
        return True
    return src in allowed[0] and dst in allowed[1]


def clean_relation(rel: str) -> str | None:
    """The canonical snake_case verb, or None when it is not in the vocabulary."""
    if not isinstance(rel, str):
        return None
    r = unicodedata.normalize("NFKC", rel).strip().casefold()
    r = re.sub(r"[^a-z0-9]+", "_", r).strip("_")
    r = _RELATION_SYNONYMS.get(r, r)
    return r if r in RELATIONS else None


# --- resolution ---------------------------------------------------------------

_AMBIGUOUS = object()


def _short_form_of(short: str, long_: str) -> bool:
    """Is `short` a way of writing the longer name `long_` on its own?

    - Names with a separator (リゼット・アルノー, Western order in katakana): the
      short form must be one whole part, never a prefix of a part.
    - Kanji names: a family name (館脇 of 館脇正太郎) or given name (櫻子 of 九条櫻子),
      at least two characters and at most four shorter.
    - Hangul: the given name, which drops a one-syllable family name.
    - Katakana without a separator: never. アリア is not アリアンヌ.
    """
    if len(short) < 2 or len(short) >= len(long_):
        return False
    if "・" in long_:
        return short in [p for p in long_.split("・") if p]
    if any(0xAC00 <= ord(c) <= 0xD7AF for c in long_):
        return long_.endswith(short) and len(long_) - len(short) == 1
    if any(0x4E00 <= ord(c) <= 0x9FFF for c in long_) and any(0x4E00 <= ord(c) <= 0x9FFF for c in short):
        return (long_.startswith(short) or long_.endswith(short)) and len(long_) - len(short) <= 4
    return False


async def _find_by_alias(session: AsyncSession, series_id: str, n: str) -> MemoryEntity | None:
    stmt = (select(MemoryEntity)
            .join(MemoryAlias, MemoryAlias.entity_id == MemoryEntity.id)
            .where(MemoryEntity.series_id == series_id, MemoryAlias.norm == n)
            .order_by(MemoryEntity.first_volume, MemoryEntity.id)
            .limit(1))
    return (await session.execute(stmt)).scalars().first()


async def _candidates(session: AsyncSession, series_id: str, kind: str | None):
    stmt = (select(MemoryEntity, MemoryAlias)
            .join(MemoryAlias, MemoryAlias.entity_id == MemoryEntity.id)
            .where(MemoryEntity.series_id == series_id)
            .order_by(MemoryEntity.id, MemoryAlias.id))
    if kind is not None:
        stmt = stmt.where(MemoryEntity.kind == kind)
    return (await session.execute(stmt)).all()


async def _fuzzy_match(session: AsyncSession, series_id: str, n: str, kind: str | None,
                       script: str, blocked: set[str]):
    """A conservative merge for a name we have never seen spelled this way.

    Returns an entity, None, or _AMBIGUOUS when a short form fits more than one
    person (a bare family name shared by siblings): such a mention is not
    recorded at all rather than recorded against the wrong person.
    """
    if len(n) < 4 and script not in {"ja", "ko", "zh"}:
        return None
    rows = await _candidates(session, series_id, kind)
    best, best_ratio = None, 0.0
    for entity, alias in rows:
        if entity.id in blocked or (alias.script and script and alias.script != script):
            continue
        ratio = difflib.SequenceMatcher(None, n, alias.norm).ratio()
        if ratio > best_ratio:
            best, best_ratio = entity, ratio
    if best is not None and best_ratio >= _MERGE_RATIO:
        return best

    if script not in {"ja", "ko", "zh"}:
        return None
    people = {entity.id: entity for entity, alias in rows
              if entity.kind == "person" and entity.id not in blocked
              and _short_form_of(n, alias.norm)}
    if len(people) == 1:
        return next(iter(people.values()))
    if len(people) > 1:
        return _AMBIGUOUS
    return None


async def _add_alias(session: AsyncSession, entity: MemoryEntity, name: str, n: str,
                     pos: tuple[int, int]) -> None:
    """Attach a surface form to an entity, or move its first sighting earlier."""
    row = (await session.execute(select(MemoryAlias).where(
        MemoryAlias.entity_id == entity.id, MemoryAlias.norm == n))).scalars().first()
    if row is None:
        session.add(MemoryAlias(entity_id=entity.id, alias=name, norm=n, volume=pos[0],
                                page=pos[1], script=script_of(name)))
    elif _earlier(pos, (row.volume, row.page)):
        row.volume, row.page = pos
    if _earlier(pos, (entity.first_volume, entity.first_page)):
        entity.first_volume, entity.first_page = pos


async def resolve_or_create(session: AsyncSession, series_id: str, name: str,
                            kind: str = "term", volume: int = 1, page: int = END,
                            blocked: set[str] | None = None,
                            any_kind: bool = False) -> MemoryEntity | None:
    """Find the entity this surface form refers to, creating it if it is new.

    `blocked` holds entities already claimed by a different name on the same
    page: two names that appear side by side are two entities (Vala et al.), so
    only an exact alias can resolve to one of them. `any_kind` is for relation
    endpoints, whose kind the model does not state.
    """
    name = (name or "").strip()
    n = norm(name)
    if not n or len(n) > 120 or _is_stop(name):
        return None
    kind = _kind(kind)
    pos = (max(1, int(volume or 1)), int(page))
    entity = await _find_by_alias(session, series_id, n)
    if entity is None:
        found = await _fuzzy_match(session, series_id, n, None if any_kind else kind,
                                   script_of(name), blocked or set())
        if found is _AMBIGUOUS:
            log.info(f"memory: {name!r} fits more than one person; not recorded")
            return None
        entity = found
        if entity is not None:
            log.info(f"memory: merged new form {name!r} into {entity.canonical!r}")
    if entity is None:
        entity = MemoryEntity(series_id=series_id, kind=kind, canonical=name,
                              first_volume=pos[0], first_page=pos[1], rendering="")
        session.add(entity)
        await session.flush()
    elif entity.kind == "term" and kind != "term":
        # A relation endpoint is created as a term; the first time the model says
        # what it is, believe it.
        entity.kind = kind
    await _add_alias(session, entity, name, n, pos)
    return entity


async def _merge_into(session: AsyncSession, keep: MemoryEntity, drop: MemoryEntity) -> None:
    """Fold `drop` into `keep`: its aliases and edges move, duplicates collapse."""
    await session.execute(update(MemoryAlias).where(MemoryAlias.entity_id == drop.id)
                          .values(entity_id=keep.id))
    await session.execute(update(MemoryEdge).where(MemoryEdge.src_id == drop.id)
                          .values(src_id=keep.id))
    await session.execute(update(MemoryEdge).where(MemoryEdge.dst_id == drop.id)
                          .values(dst_id=keep.id))
    if _earlier((drop.first_volume, drop.first_page), (keep.first_volume, keep.first_page)):
        keep.first_volume, keep.first_page = drop.first_volume, drop.first_page
    if keep.kind == "term" and drop.kind != "term":
        keep.kind = drop.kind
    keep.rendering = keep.rendering or drop.rendering
    await session.flush()
    await session.execute(delete(MemoryEntity).where(MemoryEntity.id == drop.id))
    # Duplicate aliases and edges, and edges that now point at themselves.
    seen: dict[str, MemoryAlias] = {}
    for a in (await session.execute(select(MemoryAlias).where(
            MemoryAlias.entity_id == keep.id).order_by(MemoryAlias.volume, MemoryAlias.page))).scalars():
        if a.norm in seen:
            await session.delete(a)
        else:
            seen[a.norm] = a
    await _dedupe_edges(session, keep.series_id)


async def _dedupe_edges(session: AsyncSession, series_id: str) -> None:
    edges = (await session.execute(select(MemoryEdge).where(MemoryEdge.series_id == series_id)
             .order_by(MemoryEdge.established_volume, MemoryEdge.established_page))).scalars().all()
    seen = set()
    for e in edges:
        if e.relation in SYMMETRIC and e.src_id > e.dst_id:
            e.src_id, e.dst_id = e.dst_id, e.src_id
        key = (e.src_id, e.relation, e.dst_id)
        if e.src_id == e.dst_id or key in seen:
            await session.delete(e)
        else:
            seen.add(key)
    await session.flush()


async def record_extraction(session: AsyncSession, series_id: str, volume: int,
                            payload: dict | None, page_id: str | None = None,
                            page: int = END) -> dict:
    """Apply one page's (or one batch's) entity and relation extraction.

    The model returns this next to the translation, so the graph is built as a
    side effect of work we were doing anyway. `page` is the position the facts
    are stamped with; a batch passes its last page.
    """
    payload = payload if isinstance(payload, dict) else {}
    pos = (max(1, int(volume or 1)), int(page))
    added_entities = added_edges = 0
    local: dict[str, MemoryEntity] = {}  # norm -> entity, for this page's names
    claimed: set[str] = set()
    created: set[str] = set()

    async def known_ids() -> set[str]:
        return set((await session.execute(select(MemoryEntity.id).where(
            MemoryEntity.series_id == series_id))).scalars().all())

    before = await known_ids()
    for ent in (payload.get("entities") or [])[:40]:
        if isinstance(ent, str):
            ent = {"name": ent}
        if not isinstance(ent, dict):
            continue
        name = ent.get("name") or ent.get("jp") or ""
        if not isinstance(name, str):
            continue
        n = norm(name)
        if n in local:
            entity = local[n]
        else:
            entity = await resolve_or_create(session, series_id, name, ent.get("kind") or "term",
                                             pos[0], pos[1], blocked=claimed)
        if entity is None:
            continue
        local[n] = entity
        claimed.add(entity.id)
        if entity.id not in before:
            created.add(entity.id)

        # The model listing a form as an alias on this page is the evidence a
        # merge needs, so it attaches directly, without a similarity guess.
        aliases = ent.get("aliases") if isinstance(ent.get("aliases"), list) else []
        for alias in aliases[:8]:
            if not isinstance(alias, str) or not alias.strip() or _is_stop(alias):
                continue
            an = norm(alias)
            if not an or len(an) > 120:
                continue
            other = await _find_by_alias(session, series_id, an)
            if other is None or other.id == entity.id:
                await _add_alias(session, entity, alias.strip(), an, pos)
            elif other.id in created:
                await _merge_into(session, entity, other)
                created.discard(other.id)
                claimed.discard(other.id)
                local = {k: (entity if v.id == other.id else v) for k, v in local.items()}
            else:
                log.info(f"memory: {alias!r} is listed for {entity.canonical!r} but belongs "
                         f"to {other.canonical!r}; left for a manual merge")
                continue
            local[an] = entity
    added_entities = len(created)

    for rel in (payload.get("relations") or [])[:40]:
        if not isinstance(rel, dict):
            continue
        src_name = rel.get("src") or rel.get("source") or rel.get("from") or ""
        dst_name = rel.get("dst") or rel.get("target") or rel.get("to") or ""
        relation = clean_relation(rel.get("relation") or rel.get("rel") or "")
        if not relation or not isinstance(src_name, str) or not isinstance(dst_name, str):
            continue
        ends = []
        for end in (src_name, dst_name):
            e = local.get(norm(end))
            if e is None:
                e = await resolve_or_create(session, series_id, end, "term", pos[0], pos[1],
                                            any_kind=True)
            ends.append(e)
        src, dst = ends
        if src is None or dst is None or src.id == dst.id:
            continue
        if not relation_allowed(relation, src.kind, dst.kind):
            continue
        if relation in SYMMETRIC and src.id > dst.id:
            src, dst = dst, src
        edge = (await session.execute(select(MemoryEdge).where(
            MemoryEdge.series_id == series_id, MemoryEdge.src_id == src.id,
            MemoryEdge.dst_id == dst.id, MemoryEdge.relation == relation).limit(1))).scalars().first()
        if edge is not None:
            # Restating a fact later must not unlock it earlier: keep the earliest.
            if _earlier(pos, (edge.established_volume, edge.established_page)):
                edge.established_volume, edge.established_page = pos
                edge.page_id = page_id
            continue
        session.add(MemoryEdge(series_id=series_id, src_id=src.id, dst_id=dst.id,
                               relation=relation, established_volume=pos[0],
                               established_page=pos[1], page_id=page_id))
        added_edges += 1
    await session.flush()
    return {"entities": added_entities, "edges": added_edges}


async def record_renderings(session: AsyncSession, series_id: str, mapping: dict) -> int:
    """Remember how the translation renders a name, from the model's new_terms."""
    set_ = 0
    for src, target in (mapping or {}).items():
        if not isinstance(src, str) or not isinstance(target, str) or not target.strip():
            continue
        entity = await _find_by_alias(session, series_id, norm(src))
        if entity is not None and not entity.rendering:
            entity.rendering = target.strip()[:120]
            set_ += 1
    return set_


# --- reading ------------------------------------------------------------------

def _mentions(alias: str, hay: str, hay_folded: str) -> bool:
    alias = alias.strip()
    if len(alias) < 2:
        return False
    if any(_is_cjk(c) for c in alias):
        return alias in hay
    return re.search(rf"(?<!\w){re.escape(alias.casefold())}(?!\w)", hay_folded) is not None


async def build_context(session: AsyncSession, series_id: str, volume_order: int,
                        page: int = END, texts: list[str] | None = None,
                        max_entities: int = 40, max_edges: int = 40) -> dict:
    """What a reader at page `page` of volume `volume_order` is allowed to know.

    With `texts` (the source text about to be translated), the names that occur
    in it come first, then the relations that touch them. Without it (a vision
    call, where the text is not known until the model reads the page), the most
    connected names come first. Either way, every alias, entity and edge is
    filtered on its position, and a name is shown only in forms seen by then.
    """
    volume_order = max(1, int(volume_order or 1))
    rows = (await session.execute(
        select(MemoryAlias, MemoryEntity)
        .join(MemoryEntity, MemoryEntity.id == MemoryAlias.entity_id)
        .where(MemoryEntity.series_id == series_id,
               _visible(MemoryAlias.volume, MemoryAlias.page, volume_order, page))
        .order_by(MemoryAlias.volume, MemoryAlias.page, MemoryAlias.id))).all()
    entities: dict[str, dict] = {}
    for alias, entity in rows:
        e = entities.setdefault(entity.id, {
            "id": entity.id, "kind": entity.kind, "rendering": entity.rendering or "",
            "volume": alias.volume, "page": alias.page, "canonical": entity.canonical,
            "seen": []})
        e["seen"].append((alias.volume, alias.page, alias.alias))
    for e in entities.values():
        # The first form seen is the name a reader at this point knows it by. Forms
        # first seen on the same page tie-break on the name the entity was created
        # with, then the longer form, so the choice never depends on row ids.
        e["seen"].sort(key=lambda s: (s[0], s[1], s[2] != e["canonical"], -len(s[2]), s[2]))
        e["forms"] = [s[2] for s in e["seen"]]
        e["name"] = e["forms"][0]
        e["aliases"] = [f for f in dict.fromkeys(e["forms"][1:]) if f != e["name"]]

    edge_rows = (await session.execute(
        select(MemoryEdge).where(
            MemoryEdge.series_id == series_id,
            _visible(MemoryEdge.established_volume, MemoryEdge.established_page,
                     volume_order, page))
        .order_by(MemoryEdge.established_volume.desc(), MemoryEdge.established_page.desc(),
                  MemoryEdge.id))).scalars().all()
    edge_rows = [e for e in edge_rows if e.src_id in entities and e.dst_id in entities]
    degree: dict[str, int] = {}
    for e in edge_rows:
        degree[e.src_id] = degree.get(e.src_id, 0) + 1
        degree[e.dst_id] = degree.get(e.dst_id, 0) + 1

    by_weight = sorted(entities, key=lambda i: (-degree.get(i, 0), -entities[i]["volume"],
                                                -entities[i]["page"], i))
    mentioned: list[str] = []
    if texts:
        hay = "\n".join(t for t in texts if t)
        folded = hay.casefold()
        mentioned = [eid for eid, e in entities.items()
                     if any(_mentions(f, hay, folded) for f in e["forms"])]
    # Names in the text first, never cut; the most connected others fill the
    # room, because a page often refers to people it does not name.
    focus = set(mentioned)
    order = (mentioned + [i for i in by_weight if i not in focus])[:max(max_entities, len(mentioned))]
    chosen_set = set(order)
    edges = [e for e in edge_rows if e.src_id in focus and e.dst_id in focus]
    edges += [e for e in edge_rows if (e.src_id in focus) != (e.dst_id in focus)
              and e.src_id in chosen_set and e.dst_id in chosen_set]
    edges += [e for e in edge_rows if e.src_id not in focus and e.dst_id not in focus
              and e.src_id in chosen_set and e.dst_id in chosen_set]
    edges = edges[:max_edges]
    chosen = list(dict.fromkeys(order + [i for e in edges for i in (e.src_id, e.dst_id)]))

    def out_entity(i):
        e = entities[i]
        return {"id": e["id"], "name": e["name"], "kind": e["kind"], "volume": e["volume"],
                "page": e["page"], "aliases": e["aliases"], "rendering": e["rendering"]}

    return {
        "entities": [out_entity(i) for i in chosen],
        "edges": [{"id": e.id, "src": entities[e.src_id]["name"], "relation": e.relation,
                   "dst": entities[e.dst_id]["name"], "volume": e.established_volume,
                   "page": e.established_page} for e in edges],
        "volume": volume_order,
        "page": page,
        "visible_entities": len(entities),
        "visible_edges": len(edge_rows),
    }


def render_context(ctx: dict) -> str:
    """The prompt block. Short on purpose: it competes with the page for tokens."""
    if not ctx or not (ctx.get("entities") or ctx.get("edges")):
        return ""
    where = f"volume {ctx['volume']}"
    if ctx.get("page") is not None and ctx["page"] < END:
        where += f", page {ctx['page'] + 1}"
    out = [f"What is known by {where} (do not reveal anything beyond it):"]
    if ctx.get("entities"):
        out.append("Names (source -> rendering; other forms):")
        for e in ctx["entities"]:
            line = f"- [{e['kind']}] {e['name']}"
            if e.get("rendering"):
                line += f" -> {e['rendering']}"
            if e.get("aliases"):
                line += "; also " + ", ".join(e["aliases"][:6])
            out.append(line)
    if ctx.get("edges"):
        out.append("Relations:")
        for e in ctx["edges"]:
            out.append(f"- {e['src']} {e['relation'].replace('_', ' ')} {e['dst']} (vol {e['volume']})")
    return "\n".join(out) + "\n\n"


async def glossary_for(session: AsyncSession, series_id: str, volume_order: int,
                       page: int = END, texts: list[str] | None = None,
                       limit: int = 200) -> list[dict]:
    """The glossary a prompt at this position may see, most useful first.

    Only terms first seen at or before the position are eligible, so the flat
    glossary has the same spoiler horizon as the graph. Terms that occur in the
    text come first and are never cut; the rest fill the remaining room,
    characters before terms and recent before old.
    """
    rows = (await session.execute(
        select(GlossaryTerm).where(
            GlossaryTerm.series_id == series_id,
            GlossaryTerm.first_volume != UNKNOWN,
            _visible(GlossaryTerm.first_volume, GlossaryTerm.first_page,
                     max(1, int(volume_order or 1)), page))
        .order_by((GlossaryTerm.kind == "character").desc(),
                  GlossaryTerm.first_volume.desc(), GlossaryTerm.jp))).scalars().all()
    hay = "\n".join(t for t in (texts or []) if t)
    hits = [t for t in rows if hay and t.jp and t.jp in hay]
    hit_ids = {t.id for t in hits}
    rest = [t for t in rows if t.id not in hit_ids]
    chosen = hits + rest[: max(0, limit - len(hits))]
    return [{"jp": t.jp, "en": t.en, "kind": t.kind} for t in chosen]


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


# --- upkeep -------------------------------------------------------------------

async def _page_texts(session: AsyncSession, series_id: str) -> list[tuple[int, int, str]]:
    """(volume order, page index, source and translated text) for every page, in
    reading order. What ensure_positions searches."""
    rows = (await session.execute(
        select(Volume.order, Page.index, Region.japanese_text, Region.english_text)
        .join(Page, Page.volume_id == Volume.id)
        .join(Region, Region.page_id == Page.id)
        .where(Volume.series_id == series_id)
        .order_by(Volume.order, Page.index, Region.order))).all()
    pages: dict[tuple[int, int], list[str]] = {}
    for order, index, jp, en in rows:
        pages.setdefault((order, index), []).append(f"{jp}\n{en}")
    return [(v, p, "\n".join(parts)) for (v, p), parts in pages.items()]


def _first_seen(needle: str, pages: list[tuple[int, int, str]]) -> tuple[int, int] | None:
    needle = needle.strip()
    if len(needle) < 2:
        return None
    cjk = any(_is_cjk(c) for c in needle)
    folded = needle.casefold()
    for v, p, text in pages:
        if (needle in text) if cjk else (folded in text.casefold()):
            return (max(1, v), p)
    return None


async def ensure_positions(session: AsyncSession, series_id: str) -> int:
    """Give every row written before positions existed a position. Returns how
    many rows were updated.

    Glossary terms and aliases are dated by the first page whose text contains
    them; a glossary term found nowhere is hidden until a translation sees it,
    and an alias found nowhere keeps its volume and moves to the end of it. Edges
    of unknown page move to the end of their volume. Cheap when nothing is
    unknown, which after the first run is always.
    """
    terms = (await session.execute(select(GlossaryTerm).where(
        GlossaryTerm.series_id == series_id,
        or_(GlossaryTerm.first_volume == UNKNOWN, GlossaryTerm.first_page == UNKNOWN)))).scalars().all()
    aliases = (await session.execute(
        select(MemoryAlias).join(MemoryEntity, MemoryEntity.id == MemoryAlias.entity_id)
        .where(MemoryEntity.series_id == series_id, MemoryAlias.page == UNKNOWN))).scalars().all()
    edges = (await session.execute(select(MemoryEdge).where(
        MemoryEdge.series_id == series_id, MemoryEdge.established_page == UNKNOWN))).scalars().all()
    if not (terms or aliases or edges):
        return 0
    pages = await _page_texts(session, series_id)
    for t in terms:
        t.first_volume, t.first_page = _first_seen(t.jp, pages) or (HIDDEN, 0)
    for a in aliases:
        a.volume, a.page = _first_seen(a.alias, pages) or (max(1, a.volume or 1), END)
    for e in edges:
        e.established_page = END
    await session.flush()
    # An entity is first seen where its earliest alias was.
    for ent in (await session.execute(select(MemoryEntity).where(
            MemoryEntity.series_id == series_id))).scalars().all():
        first = (await session.execute(select(MemoryAlias.volume, MemoryAlias.page).where(
            MemoryAlias.entity_id == ent.id).order_by(MemoryAlias.volume, MemoryAlias.page)
            .limit(1))).first()
        if first is not None:
            ent.first_volume, ent.first_page = first
        elif ent.first_page == UNKNOWN:
            ent.first_page = END
    await session.flush()
    log.info(f"memory: dated {len(terms)} glossary terms, {len(aliases)} aliases and "
             f"{len(edges)} edges in series {series_id}")
    return len(terms) + len(aliases) + len(edges)


async def renumber_volumes(session: AsyncSession, series_id: str, mapping: dict[int, int]) -> None:
    """Volume orders changed (a reorder): move every position with them.

    Memory and glossary rows are stamped with the order a volume had when they
    were written. Without this, a volume translated first and then moved to
    position 12 would keep its facts at position 1 and spoil every volume.
    """
    mapping = {int(k): int(v) for k, v in mapping.items() if int(k) != int(v)}
    if not mapping:
        return

    def remap(col):
        return case(*((col == old, new) for old, new in mapping.items()), else_=col)

    entity_ids = select(MemoryEntity.id).where(MemoryEntity.series_id == series_id)
    await session.execute(update(MemoryEntity).where(MemoryEntity.series_id == series_id)
                          .values(first_volume=remap(MemoryEntity.first_volume)))
    await session.execute(update(MemoryAlias).where(MemoryAlias.entity_id.in_(entity_ids))
                          .values(volume=remap(MemoryAlias.volume)))
    await session.execute(update(MemoryEdge).where(MemoryEdge.series_id == series_id)
                          .values(established_volume=remap(MemoryEdge.established_volume)))
    await session.execute(update(GlossaryTerm).where(GlossaryTerm.series_id == series_id)
                          .values(first_volume=remap(GlossaryTerm.first_volume)))
    await session.flush()


_NORM_VERSION = "2"


async def upgrade_norms(session: AsyncSession) -> int:
    """Recompute stored alias keys after norm() changes. Runs once per change."""
    row = (await session.execute(select(AppSetting).where(
        AppSetting.key == "memory.norm_version"))).scalar_one_or_none()
    if row is not None and row.value == _NORM_VERSION:
        return 0
    changed = 0
    for a in (await session.execute(select(MemoryAlias))).scalars().all():
        n = norm(a.alias)
        if n and n != a.norm:
            a.norm = n
            changed += 1
    if row is None:
        session.add(AppSetting(key="memory.norm_version", value=_NORM_VERSION))
    else:
        row.value = _NORM_VERSION
    await session.commit()
    if changed:
        log.info(f"memory: re-normalised {changed} aliases")
    return changed


# --- manual corrections ---------------------------------------------------------

async def _entity(session: AsyncSession, series_id: str, entity_id: str) -> MemoryEntity:
    e = (await session.execute(select(MemoryEntity).where(
        MemoryEntity.id == entity_id, MemoryEntity.series_id == series_id))).scalar_one_or_none()
    if e is None:
        raise LookupError(f"no entity {entity_id} in this series")
    return e


async def merge_entities(session: AsyncSession, series_id: str, keep_id: str, drop_id: str) -> None:
    """Two entities are one: fold the second into the first."""
    if keep_id == drop_id:
        raise ValueError("an entity cannot be merged into itself")
    await _merge_into(session, await _entity(session, series_id, keep_id),
                      await _entity(session, series_id, drop_id))
    await session.commit()


async def split_alias(session: AsyncSession, series_id: str, alias_id: str) -> str:
    """One form was wrongly merged: make it an entity of its own. Returns its id.

    Its edges stay where they are; a wrong edge can be deleted on its own.
    """
    alias = (await session.execute(
        select(MemoryAlias).join(MemoryEntity, MemoryEntity.id == MemoryAlias.entity_id)
        .where(MemoryAlias.id == alias_id, MemoryEntity.series_id == series_id))).scalar_one_or_none()
    if alias is None:
        raise LookupError(f"no alias {alias_id} in this series")
    old = await _entity(session, series_id, alias.entity_id)
    others = (await session.execute(select(func.count(MemoryAlias.id)).where(
        MemoryAlias.entity_id == old.id))).scalar() or 0
    if others <= 1:
        raise ValueError("this is the entity's only name; there is nothing to split from")
    new = MemoryEntity(series_id=series_id, kind=old.kind, canonical=alias.alias,
                       first_volume=alias.volume, first_page=alias.page, rendering="")
    session.add(new)
    await session.flush()
    alias.entity_id = new.id
    await session.commit()
    return new.id


async def delete_entity(session: AsyncSession, series_id: str, entity_id: str) -> None:
    e = await _entity(session, series_id, entity_id)
    await session.execute(delete(MemoryEdge).where(
        or_(MemoryEdge.src_id == e.id, MemoryEdge.dst_id == e.id)))
    await session.execute(delete(MemoryAlias).where(MemoryAlias.entity_id == e.id))
    await session.execute(delete(MemoryEntity).where(MemoryEntity.id == e.id))
    await session.commit()


async def delete_edge(session: AsyncSession, series_id: str, edge_id: str) -> None:
    result = await session.execute(delete(MemoryEdge).where(
        MemoryEdge.id == edge_id, MemoryEdge.series_id == series_id))
    if not result.rowcount:
        raise LookupError(f"no relation {edge_id} in this series")
    await session.commit()


async def entity_details(session: AsyncSession, series_id: str) -> list[dict]:
    """Every entity with all its aliases, for the correction UI."""
    ents = (await session.execute(select(MemoryEntity).where(
        MemoryEntity.series_id == series_id).order_by(MemoryEntity.first_volume,
                                                      MemoryEntity.first_page))).scalars().all()
    aliases = (await session.execute(
        select(MemoryAlias).join(MemoryEntity, MemoryEntity.id == MemoryAlias.entity_id)
        .where(MemoryEntity.series_id == series_id)
        .order_by(MemoryAlias.volume, MemoryAlias.page))).scalars().all()
    by_entity: dict[str, list[dict]] = {}
    for a in aliases:
        by_entity.setdefault(a.entity_id, []).append(
            {"id": a.id, "alias": a.alias, "volume": a.volume, "page": a.page})
    return [{"id": e.id, "kind": e.kind, "rendering": e.rendering or "",
             "name": (by_entity.get(e.id) or [{"alias": e.canonical}])[0]["alias"],
             "volume": e.first_volume, "aliases": by_entity.get(e.id, [])} for e in ents]


async def seed_from_glossary(session: AsyncSession, series_id: str) -> int:
    """Give every glossary term an entity, so a series translated before the
    graph existed is not empty. No model call: a glossary row already says what
    the thing is called in both languages, so the rendering is attached to the
    same entity, and each term keeps the position it was first seen at.
    """
    await ensure_positions(session, series_id)
    rows = (await session.execute(select(GlossaryTerm).where(
        GlossaryTerm.series_id == series_id, GlossaryTerm.first_volume < HIDDEN))).scalars().all()
    added = 0
    for term in rows:
        before = await _find_by_alias(session, series_id, norm(term.jp))
        entity = await resolve_or_create(session, series_id, term.jp,
                                         "person" if term.kind == "character" else "term",
                                         term.first_volume, term.first_page)
        if entity is None:
            continue
        if before is None:
            added += 1
        if term.en and not entity.rendering:
            entity.rendering = term.en
    await session.commit()
    return added
