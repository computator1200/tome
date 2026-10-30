# Series memory

Tome translates a book series volume by volume. The hard part is not any one
page; it is that volume 7 must read like the seventh book of a series and not
like a book that has forgotten everything. That needs two different things, and
for a long time this project only had the first:

1. **Lexical memory** - what a name is rendered as. The `glossary_terms` table
   holds 2,000+ `source -> target` pairs per series, is injected into every
   translate prompt, and is extended from the model's `new_terms` after each
   page. This keeps a character called the same thing on page 400 as on page 1.
2. **Semantic memory** - who that name *is*, how it relates to everything else,
   and **when the reader is allowed to know**. A glossary cannot express any of
   that, and getting it wrong is how a translation spoils its own plot.

The second one is `backend/services/memory.py`.

## The graph

Three tables in the same SQLite file (see `backend/db/models.py`):

| Table | Holds | Why |
|---|---|---|
| `memory_entities` | one row per person / place / org / term, `canonical`, `kind`, `first_volume` | the node, and the volume that introduces it |
| `memory_aliases` | `alias`, normalised `norm` (indexed), `volume`, `script` | リゼット, Lizette and リズ are one node, matched cheaply |
| `memory_edges` | `src_id`, `relation`, `dst_id`, `established_volume`, `page_id` | the relation, and **the volume that proves it** |

`established_volume` is the whole design. Retrieval filters on it, so what a
volume 3 prompt sees is a strict subset of what a volume 12 prompt sees.

## How a fact gets in

The model returns the graph alongside the translation, in the same JSON object
it already produces:

```json
"memory": {
  "entities": [{"name": "リゼット", "kind": "person", "aliases": ["リズ"]}],
  "relations": [{"src": "リゼット", "relation": "serves", "dst": "灰の教団"}]
}
```

So building the graph costs **no extra LLM calls**. The pipeline folds it in
from `_merge_new_terms()` after each page or batch, inside a `try` that logs and
rolls back on failure: memory is a side effect of translating, never a reason for
a translation to fail.

## Resolution

Deterministic first, fuzzy only as a fallback:

- **Normalise**: NFKC, casefold, strip honorifics (さん, 様, 君, ちゃん, -san,
  -sama, 씨, 님...), strip punctuation, collapse whitespace.
- **Exact alias** on `norm`. This is the common case and it is free.
- **Fuzzy**, only inside one series and one kind, at a `difflib` ratio of
  **0.90**. Hindsight merges at 0.60; at 0.60 "Aria" becomes "Ariane" and the
  translator has silently merged two characters, which is worse than a duplicate.
- **CJK family/given-name rule**: in Japanese, Korean and Chinese only, a 2+
  character form that is a prefix (`館脇` -> `館脇正太郎`) or a suffix (`櫻子` ->
  `九条櫻子`) of a longer name is the same person. Restricted to CJK on purpose:
  in Latin script a prefix is not evidence of anything.
- Pronouns (`僕`, `私`, `俺`, `I`, `he`...) and bare honorifics are never entities.

Relations use a closed vocabulary of 19 verbs (`serves`, `works_for`,
`member_of`, `sibling_of`, `parent_of`, `child_of`, `married_to`, `lives_in`,
`born_in`, `located_in`, `owns`, `knows`, `leads`, `attends`, `teaches`,
`friend_of`, `rival_of`, `kills`, `betrays`), synonyms are folded onto them, and
each is type-checked: `member_of` pointing at a person is dropped rather than
stored. Unknown kinds pass (the extractor falls back to `term`); contradictions
do not.

## How a fact gets out

`build_context(series_id, volume_order)` returns the entities introduced at or
before `volume_order` and the edges established at or before it.
`render_context()` turns that into the prompt block:

```
What is known by volume 3 (do not reveal anything beyond it):
- persons: 九条櫻子, 館脇正太郎, ばぁやさん
- places: 旭川空港, 北海道
- orgs: 九条家
Relations:
- 館脇正太郎 knows 九条櫻子 (vol 1)
- 九条櫻子 member_of 九条家 (vol 1)
```

The block keeps the longest form of a name and drops variants it already
contains, caps persons/places at 24 and bare glossary terms at 10 (terms are the
noisy part), and is inserted before the glossary in
`services/llm._context_block()`.

`GET /api/series/{id}/memory?volume=N` returns the same view the prompt gets,
which is what the series page shows and what makes the rule checkable rather
than merely claimed.

## Backfill

Series translated before the graph existed are not left empty:

- every `glossary_terms` row becomes an entity (2,067 terms across the library),
  no LLM involved;
- each volume with translated text is read once by the model, which returns its
  entities and relations, stamped with that volume's order.

Current library: 5,174 entities and 572 typed relations across eight
series. Sakurako (18 volumes) is the useful demonstration: a volume 1 prompt
sees 22 entities and 16 relations; the same series at volume 18 sees 80 and 60.

## Limits, stated plainly

- Extraction quality is the model's, not the schema's. Relation direction is
  occasionally wrong ("磯崎 serves 館脇正太郎" where the reverse may be true).
  The type check removes nonsense, not subtlety.
- Backfilled relations come from a sample of each volume, not the whole book.
- Series translated before today get their graph from the backfill pass; only
  runs after 2026-10-24 write it live.
- There is no merge UI: a wrong merge has to be fixed in the database.
- 13 tests cover normalisation, resolution, type rules and scoping
  (`backend/tests/test_memory.py`).

## Why not Hindsight per series

Evaluated and rejected before building this. Hindsight is a good system, but it
is the wrong shape for translation memory: recall costs ~1.5 s per call and needs
an LLM on write, which at 6,000 pages is hours of dead time and a large token
bill. More importantly its semantic retrieval is top-k by similarity, and
translation needs the opposite:
exact, exhaustive, cheap literal matching of every name on the page, with a hard
guarantee that a relation from volume 12 is not in a volume 3 prompt. Three
tables and a filter give that guarantee; a vector index gives an approximation
of it.
