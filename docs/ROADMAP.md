# Roadmap

This started as the plan for making Tome translate a *series* rather than a page.
All of that plan is built. This file records what shipped, with the code that
does it, and the choices that were made on purpose; [CHANGELOG.md](../CHANGELOG.md)
has the same work by release.

## Shipped

### Series memory as a graph

- **Extract** with every page, in the same model call as the translation:
  characters, places, organisations and terms, the other names each goes by
  (titles and descriptions included), and typed relations from a closed
  vocabulary (`pipeline._merge_new_terms`, `memory.record_extraction`).
- **Resolve** names onto one entity on evidence only: an exact alias, a close
  spelling, or a short form that fits exactly one person; never two names that
  appear on the same page (`memory.resolve_or_create`).
- **Scope** every name, relation and glossary term to the page where it was
  first seen, so no prompt sees anything from a later page or volume, re-runs
  and reordered volumes included (`memory.build_context`, `memory.glossary_for`,
  `memory.renumber_volumes`).
- **Retrieve** per model call: the names in the text first, with their
  renderings, then the most connected others (`pipeline._prompt_parts`).
- **Correct** it by hand: merge, split and delete from the series page
  (`MemoryPanel.tsx`, `/api/series/{id}/memory/...`).

How all of it works, with diagrams: [GRAPH-MEMORY.md](GRAPH-MEMORY.md).

### The librarian

26 tools across two toolsets. It inspects uploads, samples pages, links metadata,
creates volumes, searches the web, fetches a publisher page, sets a poster, reads
the library and its memory, and repairs it:

- `reorder_volumes` when a collection was sorted by title instead of by content;
- `repair_volume_pages` when a book arrived as a single endless page;
- `convert_pdf_to_scans` when a PDF has no Unicode text layer at all;
- `delete_volume`, which refuses to run until the user has said yes to a
  question naming the volume, because it is the one irreversible act.

The series chat prompt carries the repair playbook, so "the order is wrong" gets
an action rather than an explanation.

### Pages, laid out

- **Vision first.** One model call per page: image in, translated regions with
  their boxes out (`pipeline._run_vision`). The OCR-then-translate path remains
  for scans that need it and for text-layer PDFs.
- **Boxes.** The reader and the PDF set each translation over its source block
  when a page's regions all have boxes, and as one column when they do not.
  Pages translated before boxes existed get them when re-translated.
- **Isolation.** One bad page costs one page on every path, failures are
  counted wherever they happen, and Resume retries only what failed.

### Quality and cost

- **Consistency report** per volume: which glossary terms the translation
  rendered differently, on which pages, and which terms the volume introduced
  (`quality.glossary_report`).
- **Regression sample**: re-translate a fixed sample with the current model and
  prompts, without saving, and score it against what is stored
  (`scripts/regression_sample.py`).
- **Cost and latency per volume**: model calls, tokens and time on every volume,
  with an estimated cost once prices are set (`llm.track_usage`).

### Throughput and storage

- **Several volumes at once**, never two of the same series
  (`worker._dispatch`).
- **Exports** built once per state of the translation, cached on disk and
  streamed from the file (`routers/export._cached`).
- **Local disk or S3** (or MinIO, Garage, R2) for page images
  (`storage_backend/`).

### Generality

Ingest and storage are format-agnostic: magic-byte detection, EPUB, PDF, text,
ZIPs of images, and any-language metadata. Metadata comes from four keyless
catalogues (Open Library, Internet Archive, the German National Library (DNB
SRU) and the Bibliotheque nationale de France (BnF SRU)), plus Google Books when
`GOOGLE_BOOKS_API_KEY` is set. Crossref is not queried: it answers a novel title
with a paper. MangaUpdates and NovelUpdates were removed as sources because they
only cover one community's catalogue.

Source language is detected rather than assumed (`services/language.py`).

## Choices made on purpose

- **A digital book is text, not a facsimile.** A text volume has no page images,
  so its PDF is typeset from the translation, one chapter per page, and an EPUB
  repacks the original book with the text swapped. Scans export as pictures of
  the original with the translation set on them.
- **Local OCR is optional.** `manga-ocr` and torch are a 2 GB install, imported
  only when used. Without them the OCR path uses a vision model to read pages,
  and the Settings page offers local OCR only when it is installed.
- **One graph per series.** Two series that share a name usually mean two
  people, so no fact crosses from one series to another.
- **The model reads; the code decides.** Extraction is the model's reading of
  the page. The code accepts it only within the vocabulary, the kind rules and
  the merge rules, and anything still wrong is fixed in the memory panel.
