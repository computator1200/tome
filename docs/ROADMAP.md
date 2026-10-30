# Roadmap

This started as the plan for making Tome translate a *series* rather than a page.
Most of that plan is now built, so this file records what shipped and what is
still open. The shipped items cite the code that proves them; the open ones are
work, not aspirations.

## Shipped

### Series memory as a graph

- **Extract** per volume: characters, places, recurring terms, honorifics, and the
  source-language form of each (`pipeline.py`, `_merge_new_terms`).
- **Resolve** aliases onto one node, with a short-form guard so "Al" does not
  swallow "Alcazar" (`services/memory.py`).
- **Store** typed relations from a closed vocabulary, each stamped with the volume
  that established it (`memory_edges`).
- **Retrieve** at translation time: a prompt for volume N carries only what was
  established up to volume N (`memory.py:343-344`, `pipeline.py:222-225`).
- **Write back** so volume N+1 starts from what N learned.

### The librarian

26 tools across two toolsets. It inspects uploads, samples pages, links metadata,
creates volumes, searches the web, fetches a publisher page, sets a poster, reads
the library, and repairs it:

- `reorder_volumes` when a collection was sorted by title instead of by content;
- `repair_volume_pages` when a book arrived as a single endless page;
- `convert_pdf_to_scans` when a PDF has no Unicode text layer at all;
- `delete_volume`, gated behind `ask_user` because it is the one irreversible act.

The series chat prompt carries the repair playbook, so "the order is wrong" gets
an action rather than an explanation.

### Vision engine as the primary path

One model call per page: image in, translated regions out (`pipeline.py:324`).
The OCR-then-translate path still exists for text-layer PDFs, which is what makes
the digital ingest route cheap.

### Generality

Ingest and storage are format-agnostic: magic-byte detection, EPUB, PDF, text,
ZIPs of images, and any-language metadata. Metadata comes from four keyless
catalogues (Open Library, Internet Archive, the German National Library (DNB
SRU) and the Bibliotheque nationale de France (BnF SRU)), plus Google Books when
`GOOGLE_BOOKS_API_KEY` is set. Crossref is not queried: it answers a novel title
with a paper. MangaUpdates and NovelUpdates were removed as sources because they
only cover one community's catalogue.

Source language is detected rather than assumed (`services/language.py`).

## Open

- **Quality gates.** A glossary diff between consecutive volumes to catch drift;
  a fixed sample re-translated and diffed as a regression test; cost and latency
  per volume, because a series is hundreds of pages.
- **Per-page memory retrieval.** The memory block is currently per volume; the
  page being translated does not narrow it further. It is capped at 80 entities
  and 60 edges, oldest first, and is only as good as the model's extraction:
  relation direction is sometimes wrong, and edges do not yet record the page
  that established them. See [GRAPH-MEMORY.md](GRAPH-MEMORY.md#6-what-it-does-not-do-yet).
- **Bounding boxes.** `regions` has `x`, `y`, `width`, `height` as percentages of
  the page and the writer stores whatever the model returns, but every one of the
  57,149 regions in the current database is `0,0,0,0`. The reader and the PDF
  therefore place text in reading order, not at a position. Making it spatial
  means changing the vision prompt and re-running the affected volumes.
- **Text volumes export as reflowed text, not facsimiles.** PDF export renders
  scan pages from their stored images; a text volume has no images, so its PDF is
  typeset from the translated text, one page per chapter. In the reader, chapters
  are a text column, not a laid-out page: fine for a novel, not a facsimile.
- **Isolation on the local-OCR path.** Resume is real, and on the vision path one
  failed page fails alone. On the local-OCR path, if one response in a batch of
  five fails, the other four pages fail with it. See
  [ARCHITECTURE.md](ARCHITECTURE.md#isolation-what-one-failure-takes-down).
- **One volume at a time.** No batching across volumes or series.
- **Streamed export.** PDF and EPUB are built in memory before the first byte is
  sent, which is slow for a 300-page scan.
- **Local OCR.** `manga-ocr` plus torch is not installed on the deployment, so the
  OCR path is hidden behind `capabilities.local_ocr: false`.
