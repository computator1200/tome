# Graph memory

Tome keeps a graph per series of who and what appears in the books, the names
each of them goes by, how the translation renders them, and how they relate.
Every name and relation records the position (volume and page) where it was
first seen. The graph is written as a side effect of translation and read back
into every prompt, scoped to the page being translated.

The code is `backend/services/memory.py`, the tables are in
`backend/db/models.py`, the write hook is `_merge_new_terms()` and the per-call
read is `_prompt_parts()`, both in `backend/services/pipeline.py`, and the HTTP
views are in `backend/routers/series.py`. The tests are
`backend/tests/test_memory.py` and `test_memory_review.py`, one test for each
behaviour described below. This page describes what the code does today.

On the development library the graph held 5,174 entities, 5,417 aliases and 572
typed relations in October 2026. Separately, all 57,149 regions are indexed in
an SQLite FTS5 table (`region_fts`) for full-text search over the text.

## 1. The problem

A translator that works page by page sees one page and whatever context it is
handed. Take one character in a light-novel series:

- volume 1 introduces her as リゼット・アルノー, and the narration then calls
  her リゼット;
- in volume 2 her friends start calling her リズ, and the text establishes that
  she serves the order 灰の教団;
- from volume 4 she is mostly referred to by her title, 灰の聖女.

![Four source names for one character. The middle column turns them into four people; the right column folds them into one entity with one relation.](images/graph-memory-problem.png)

*Follow each row across. In the middle column every source name becomes its own
English name, so by volume 7 the reader has met four people. In the right column
the four names are aliases of one entity, and the `serves` edge from volume 2
goes with her.*

By volume 7 a page-by-page translator has produced Lisette Arnault, Rizette,
Liz and the Ash Saint. A flat glossary fixes the first two if someone adds the
right row, but it has no way to say that リズ and 灰の聖女 are the same person,
and no way to say that she serves 灰の教団. A volume 7 page that only says
"for that order's sake" is translated without knowing which order.

There is a second failure that a glossary cannot see at all: telling the
reader too much. If the model is given a fact from volume 12 while translating
volume 3, it can leak into the wording. The graph exists to carry what is known,
and only what is known by the page in hand.

## 2. Three tables

![The three graph tables and their joins. Position columns are highlighted.](images/graph-memory-schema.png)

*Look at the three blue columns: where an entity, an alias and an edge were
first seen. Every read is filtered on them. The solid lines are the joins to
`memory_entities.id`; the dashed lines are the per-series keys.*

| Table | One row is | Key columns |
|---|---|---|
| `memory_entities` | a person, place, organisation, term or thing in one series | `series_id`, `kind`, `canonical`, `first_volume`, `first_page`, `rendering` |
| `memory_aliases` | one surface form of an entity | `entity_id`, `alias`, `norm` (indexed), `volume`, `page`, `script` |
| `memory_edges` | one typed relation between two entities | `series_id`, `src_id`, `relation`, `dst_id`, `established_volume`, `established_page`, `page_id` |

Why three and not one:

- **Entities and aliases are separate** because the mapping is one to many.
  Four names point at one row. Lookups go through `memory_aliases.norm`, an
  indexed exact match, so the common case costs one query and no fuzzy work.
- **Edges are separate from entities** because a relation has its own facts: a
  type and the position that states it. Edges hold entity ids, not names, so a
  new alias for a known entity changes no edge.
- **Each table carries a position.** That is what lets the read path return a
  strict subset of the graph for an earlier page.

`rendering` is how the translation renders the entity's name, taken from the
`new_terms` the model returns. The glossary (`glossary_terms`) carries a
position too (`first_volume`, `first_page`), so it is scoped the same way.

All of it lives in the same SQLite file as the rest of Tome. Deleting a series
cascades to its entities and edges, and deleting an entity cascades to its
aliases and edges.

## 3. The write path

![Write path from one model call to the three tables.](images/graph-memory-write-path.png)

*Read the top row first: one model call returns three keys, and only `memory`
feeds the graph. Then read each lane left to right: an entity name stops at the
first step that finds a match, and a relation must pass the kind check before it
is stored.*

### When extraction happens

There is no separate extraction call. The translate prompts in
`backend/services/llm.py` (`translate_text()` and `vision_ocr_translate()`) ask
for a `memory` object in the same JSON response as the translation:

```json
"memory": {
  "entities": [{"name": "リゼット", "kind": "person", "aliases": ["リズ", "灰の聖女"]}],
  "relations": [{"src": "リゼット", "relation": "serves", "dst": "灰の教団"}]
}
```

An alias is any other way the text refers to the entity: a short name, a
nickname, or a title or description, listed only when the text makes clear it
is the same entity. That is how 灰の聖女 joins リゼット: not by string
similarity, which could never link them, but because a page used both for her.

After each page, the pipeline calls `_merge_new_terms()` with the page's
position. It adds `new_terms` to `glossary_terms` (or moves an existing term's
first sighting earlier), then passes the `memory` object to
`memory.record_extraction()` and the renderings to `record_renderings()`. A
batch of pages is stamped with its last page, so no page in the batch sees what
a later one established. The memory write runs in its own database session: a
malformed payload rolls back only itself, never the translated page, and the
pipeline logs `memory extraction skipped`. The graph costs no extra model calls.

`record_extraction()` reads at most 40 entities, 8 aliases per entity and 40
relations from one response, and ignores anything that is not the right shape.

### Resolving a name

`resolve_or_create()` turns a surface form into an entity, in this order. The
rules follow the character name clustering of Vala et al. ("Mr. Bennet, his
coachman, and the Archbishop walk into a bar", EMNLP 2015): merge only on
evidence, never between names that appear side by side, and drop a short name
that fits more than one character rather than guess.

1. **Normalise.** `norm()` applies NFKC and casefolds; strips quotes and edge
   punctuation; unifies name separators (`＝`, `·`, `･` become `・`); removes
   spaces inside CJK names (九条 櫻子 is 九条櫻子); reads 一 or a dash inside
   katakana as ー; folds variant kanji that name the same person (髙 and 高, 﨑
   and 崎, 齋 and 斎) and the Persian and Arabic forms of ی and ک; and strips
   one trailing honorific (さん, 様, 君, 殿下, 嬢, 씨, -san and others), but
   only if what is left is still a name. 神様 (god) and 神殿 (temple) both end
   in an honorific character, and stripping either would leave 神 and merge
   them. Pronouns and kinship words (僕, 私, 母さん, お兄ちゃん, I, she) are not
   entities.
2. **Exact alias.** Look up `memory_aliases.norm` within the series. This is
   the usual case.
3. **Fuzzy match.** Only within the same series, the same `kind` and the same
   script, and only above a `difflib` ratio of 0.90. The threshold is high on
   purpose: at 0.6, "Aria" and "Ariane" merge, and merging two characters is
   worse than keeping a duplicate. Latin names shorter than four characters
   are never fuzzy-matched.
4. **Short form.** For people only, a name that is a shorter way of writing a
   known one resolves to it, and only if exactly one person fits:
   - with a separator (リゼット・アルノー), the short form must be one whole
     part (リゼット or アルノー), never a prefix of a part;
   - in kanji, a family name (館脇 of 館脇正太郎) or given name (櫻子 of
     九条櫻子), at least two characters and at most four shorter;
   - in hangul, the given name without a one-syllable family name;
   - katakana without a separator never: アリア is not アリアンヌ.

   A longer name never resolves to a shorter one, so 九条総司 arriving after
   九条 does not join it. When a short form fits two people (九条 for 九条櫻子
   and 九条総司), the mention is not recorded at all.
5. **Create.** Otherwise a new entity is created at the current position.

Steps 3 and 4 skip any entity already claimed by a different name on the same
page: two names that appear side by side are two entities, so a sibling edge
between 館脇 and 館脇正太郎 survives instead of being dropped as a self-loop.

Whichever step wins, the surface form is added as an alias if its `norm` is new
for that entity, and its first sighting moves earlier if the current position is
earlier. Aliases the model lists attach to their entity directly. If a listed
alias already belongs to a different entity created on the same page, the two
are merged; if it belongs to an older entity, the conflict is logged and left
for a person to settle (section 5).

Relation endpoints resolve against the names on the same page first, so
`{"src": "リゼット"}` finds the リゼット・アルノー listed beside it instead of
creating a new entity. An endpoint that is not on the page goes through the same
chain; if it is new it is created as a `term`, and its kind is upgraded the
first time the model says what it is.

### The relation vocabulary

Relations come from a closed list of 19 verbs, so the graph stays queryable:
`serves`, `works_for`, `member_of`, `sibling_of`, `parent_of`, `child_of`,
`married_to`, `lives_in`, `born_in`, `located_in`, `owns`, `knows`, `leads`,
`attends`, `teaches`, `friend_of`, `rival_of`, `kills`, `betrays`. Four things
hold it in place:

- both translate prompts list exactly these verbs (a test checks the prompt
  against the code);
- `clean_relation()` converts the verb to snake_case, folds known synonyms onto
  the list (`sister_of` becomes `sibling_of`, `resides_in` becomes `lives_in`),
  and drops anything else;
- `relation_allowed()` checks the kinds at each end against `_RELATION_TYPES`.
  `member_of` must point from a person to an org; `lives_in` from a person to a
  place. A known contradiction is dropped. An end whose kind is still the
  fallback `term` cannot be judged and passes;
- symmetric relations (`sibling_of`, `married_to`, `friend_of`, `rival_of`,
  `knows`) are stored once, whichever way round they were stated.

A relation from an entity to itself is dropped.

### Re-runs

Re-translating a volume does not duplicate the graph. Names resolve to the same
entities through the exact alias lookup, and an edge that already exists is not
added again. If a fact is restated at an earlier position, its position moves
earlier; restating it later never makes it visible earlier than the first page
that established it. A re-run only adds: nothing in the write path deletes.

## 4. The read path

![Read path from a reader at volume N to the API response and the prompt block.](images/graph-memory-read-path.png)

*Start at the timeline: the reader is at volume 3, so the volume 12 relation is
outside the box and never reaches the query result. Then follow the two outputs
of `build_context()`: the API response on the right, and the prompt block at the
bottom.*

`build_context(series_id, N, page, texts)` runs two queries and nothing else:
the aliases first seen at or before (N, page), joined to their entities, and the
edges established at or before it. An entity is visible if one of its names is,
and it is shown only under the names a reader at that point has met: the first
form seen is its name, and later forms are listed as "also". A title the story
reveals in volume 12 is not in a volume 3 prompt, even though it belongs to the
same entity.

There is no model call on the read path. It is SQL against the same SQLite file.

**Why a fact is only visible from where it was stated.** The filter is the
reader's position, not the latest state of the series. An edge stated on page
41 of volume 12 is not returned for any page of volumes 1 to 11, nor for pages 1
to 40 of volume 12, so it is not in the string sent to the model. The model
cannot leak a fact that was never in its prompt. That holds on a re-run too:
re-translating volume 3 does not show page 1 what page 41 established.

**What gets in.** The pipeline calls this once per model call, through
`_prompt_parts()`, with the position of the page (or the first page of a batch)
and, when it has it, the source text about to be translated. Following DelTA
(Wang et al., ICLR 2025), whose proper-noun records put the names that occur in
the current text into the prompt with their first translation:

- names that occur in the text come first, and are never cut;
- the most connected other names fill the remaining room (40 in all), because
  a page often refers to people it does not name;
- relations between the names in the text come first, then those touching
  them (40 in all).

On the vision path the model reads the page and translates it in one call, so
the text is not known in advance; there the block is the 40 most connected
names. `render_context()` writes each name with its rendering and other forms,
and `llm._context_block()` places the block before the glossary:

```
What is known by volume 3, page 12 (do not reveal anything beyond it):
Names (source -> rendering; other forms):
- [person] リゼット -> Lizette; also リズ
- [org] 灰の教団 -> Order of Ash
Relations:
- リゼット serves 灰の教団 (vol 2)
```

**The glossary.** `glossary_for()` applies the same horizon to the flat
glossary: only terms first seen at or before the position are eligible. Terms
that occur in the text come first and are never cut; the rest fill the room up
to 200, characters before terms and recent before old.

**The endpoint.** `GET /api/series/{id}/memory` returns the same view without a
text, for the series page. Without `volume` it is scoped to the series' latest
volume; with `?volume=N` it is scoped as a prompt for volume N would be. A
`volume` larger than the series has is a 400, not a silent "everything":

```json
{
  "stats": {"entities": 412, "edges": 61, "aliases": 430},
  "scoped_to_volume": 3,
  "entities": [{"id": "…", "name": "リゼット", "kind": "person", "volume": 1, "page": 0,
                "aliases": ["リズ"], "rendering": "Lizette"}],
  "edges": [{"id": "…", "src": "リゼット", "relation": "serves", "dst": "灰の教団",
             "volume": 2, "page": 14}],
  "truncated": {"entities": true, "edges": true}
}
```

(Values illustrative.) `stats` counts the whole series, unscoped. `truncated`
is true when the response holds fewer rows than `stats`: either a cap (80
entities, 60 edges) was hit, or the scope hid later rows.

## 5. Keeping it right

**Positions that move.** Every row is stamped with a volume's order, so when the
volumes of a series are reordered (`reorder_volumes`, which the librarian can
call), `renumber_volumes()` moves every alias, edge and glossary term with its
volume. A volume translated first and then moved to position 12 does not spoil
volumes 1 to 11.

**Rows from before positions existed.** `ensure_positions()` runs before each
volume is processed and before the memory view is served. It dates a glossary
term or alias by the first page whose text contains it. A glossary term found
nowhere is hidden until a translation sees it; an alias found nowhere keeps its
volume and moves to the end of it, and so does an edge whose page is unknown.
Unknowns always err on the side of telling the model less. When `norm()`
changes, `upgrade_norms()` re-keys the stored aliases once, at startup.

**Corrections.** Extraction is a model's reading of the page, so the graph can
be wrong. The series page has a memory panel that lists every entity with all
its names, and the API behind it lets a person:

| Fix | Endpoint |
|---|---|
| merge two entries that are one person or thing | `POST /api/series/{id}/memory/merge` |
| split off a name that was merged by mistake | `POST /api/series/{id}/memory/aliases/{alias_id}/split` |
| delete an entity, with its names and relations | `DELETE /api/series/{id}/memory/entities/{entity_id}` |
| delete one relation | `DELETE /api/series/{id}/memory/edges/{edge_id}` |

A merge keeps the earlier position of the two, and duplicate names and edges
collapse. The librarian can read the graph through its `get_memory` tool, so
"who is リズ?" in the series chat is answered from it.

**Series that predate the graph.** `seed_from_glossary()` gives every glossary
term an entity at the term's own position, with its rendering attached, so an
old series is not empty. Its relations come from translation: re-translating a
volume writes its part of the graph page by page.

## 6. Why a graph and not a glossary

A glossary is a list of pairs: this word in the source, that word in English.
It answers one question, "what do we call this?", and it answers it well. Tome
still has one, and it is still in every prompt.

It cannot answer the questions that make a long series hard. It cannot say that
two different words are the same person, because each row stands alone. It
cannot say how two people are connected, because a row has no second end. And
it cannot say when something became true, because a row has no time.

The graph adds exactly those three things: aliases join names to one person,
edges join people to each other, and every row carries the position it came
from. That last part is what lets Tome hand the model everything the reader
already knows and nothing they do not.

A vector memory store was considered and rejected. Recall there is top-k by
similarity, costs around a second and a half per call, and needs a model call on
every write, which across thousands of pages is hours of waiting and a large
token bill. Translation needs the opposite: exact, exhaustive and cheap matching
of every name on the page, and a hard guarantee that a volume 12 relation is not
in a volume 3 prompt. Three tables and a filter give that guarantee; a vector
index gives an approximation of it.

## 7. Design choices

These are deliberate, and each has a reason:

- **One graph per series.** Every lookup is scoped by `series_id`. Two series
  that share a name usually mean two different people, and linking across them
  would let one book's facts reach another's prompts.
- **The model reads; the code decides.** The graph stores what the model read
  on the page, and the code only accepts it in the closed vocabulary, with the
  right kinds at each end, under the merge rules above. Relation direction is
  the model's judgement; when it is wrong, the memory panel fixes it.
- **A description joins a name only on the page's evidence.** "The girl from
  the cathedral" becomes リゼット's alias when a page uses both for her, which is
  the only evidence that is not a guess. A description never paired with a name
  stays its own entry until a page pairs them or a person merges them.
- **Uncertain means hidden.** A short name that fits two people is not
  recorded, a term never seen in the text is not shown, and a fact of unknown
  page is shown only from the end of its volume.
