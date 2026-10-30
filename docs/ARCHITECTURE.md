# Architecture

How the pieces fit, stage by stage. Each table names the file and function that
implement a stage, so you can go straight to the code. The diagrams are
`docs/images/pipeline.svg` and `librarian.svg`, with PNG renders beside them for
GitHub; if you change what a diagram shows, change the diagram in the same
commit.

---

## Overview

```
frontend/          React + TypeScript + Vite + shadcn/ui
backend/           FastAPI + SQLAlchemy (async) + SQLite
  routers/         HTTP surface
  services/        pipeline · llm · memory · quality · ingest · documents · agent · metadata
  storage_backend/ object store: local disk or S3
```

A **series** holds **volumes**; a volume holds **pages**. A page is either an
image (a scan) or a text block (a digital chapter). Translation runs per page,
and every model call carries the series context as of that page: the glossary,
the summaries of earlier volumes, and what the [series memory](GRAPH-MEMORY.md)
knows by then. The `jobs` table is the single source of truth for progress, and
also counts what each volume cost, so a refresh or a restart loses neither.

Storage:

- `backend/storage_backend/` is the only place allowed to touch a page file.
  Keys look like `volumes/<volume_id>/pages/0000.jpg`; database rows point at
  them, so keys are never renamed.
- `STORAGE_BACKEND=local` (the default) keeps objects on disk;
  `STORAGE_BACKEND=s3` keeps them in an S3 bucket, or anything that speaks the
  S3 API (MinIO, Garage, R2), configured with the `S3_*` variables in
  `.env.example`. Either way pages are served through `/files/`, so a bucket
  never has to be public.
- SQLite for the index (`DATABASE_URL`), images by key. The original digital
  source (EPUB) is kept as well, so a translated EPUB can be repacked from it.

Three decisions that everything else leans on:

1. **Pages are objects, not database blobs.** A page image is stored by key; the
   database holds the index. Swapping local disk for S3 is a setting, not a
   migration.
2. **A translated page is regions, not a string.** Every region has its text and
   a box (`x`, `y`, `width`, `height` as percentages of the page). The reader and
   the PDF place each translation over its source block when a page's regions
   all have boxes, and set the page as one column when they do not.
3. **One OpenAI-compatible endpoint.** `services/llm.py` talks to anything that
   speaks Chat Completions, so the app runs against a hosted model or a local
   runner without code changes. See [MODELS.md](MODELS.md).

---

## 1. Translation pipeline

![The translation pipeline: a volume is classified, read by one model call carrying the series context, written, and what it learned feeds the next volume.](images/pipeline.png)

`services/worker.py` runs a dispatcher over an in-process `asyncio.Queue`. It
processes up to `volume_concurrency` volumes at once (Settings, default 1),
never two of the same series, because a volume's prompts build on the volumes
before it. Each volume goes through `pipeline.process_volume()`.

| # | Stage | File · function | What to know |
|---|---|---|---|
| 1 | Ingest | `routers/volumes.py` `create_volume`; `services/ingest.py` `extract_group_to_volume`, `create_text_volume` | Page images go to the object store under `volumes/<id>/pages/0000.ext`; the database holds only the key. |
| 2 | Classify | `services/page_classifier.py` `classify_page` | OpenCV pixel statistics. Runs on every scan page; only `text` and `mixed` pages continue. Text volumes skip it. |
| 3a | vision engine | `pipeline._run_vision` → `llm.vision_ocr_translate` | One call per page: image in, translated regions with boxes out. |
| 3b | ocr_text engine | `pipeline._run_ocr_text` | OCR first, with `llm.ocr_page_vision` (`OCR_BACKEND=vision`, the default) or `services/ocr.py` for `manga_ocr`; then `llm.translate_text` on batches of `batch_size` pages. |
| 3c | text volumes | `pipeline._run_text_volume` | Digital books: blocks are chunked per chapter and translated directly. |
| 4 | Context | `pipeline._prompt_parts` → `memory.build_context`, `memory.glossary_for`; `llm._context_block` | Built for each model call, scoped to its page: title, author, genres, synopsis, earlier volumes' summaries, the memory block, and the glossary, with the names in the text first. |
| 5 | Write | `pipeline._replace_regions`, `pipeline._merge_new_terms` | Regions are replaced per page and committed. Then new glossary terms and the memory extraction are recorded at the page's position, the memory in its own session. |
| 6 | Summarise | `pipeline._summarize_volume` → `llm.summarize` | Becomes `Volume.summary`, the context for later volumes. |
| 7 | Read / export | `components/Reader.tsx`, `PageView.tsx`; `routers/export.py` | The reader places text on its boxes or shows a column. Exports are built once per state of the translation, cached under `_exports/` in the storage directory, and streamed from the file. |

### Numbers

| Value | Where |
|---|---|
| Model calls in flight per volume: `concurrency`, default **4** | `config.OPENAI_CONCURRENCY`; Settings |
| Volumes at once: `volume_concurrency`, default **1** | Settings; `worker._dispatch` |
| `batch_size` default **5**, clamped to **1 to 20** | `db/models.Volume`; `pipeline._run_ocr_text` |
| Text chunks: at most **40** blocks; a chunk closes once it is over **4000** characters | `pipeline._TEXT_CHUNK_BLOCKS`, `_TEXT_CHUNK_CHARS` |
| Memory block: up to **40** names and **40** relations, names in the text never cut | `memory.build_context` |
| Glossary in the prompt: up to **200** terms, terms in the text first | `memory.glossary_for` |
| `_chat` retries: **3** attempts, sleeping 1 s then 2 s; 400/401/403/404 are not retried | `llm._chat` |
| Model timeout default **180 s** | `config.OPENAI_TIMEOUT` |
| Temperature **0.3** (translation), **0.2** (agent) | `llm._create`, `llm.chat_with_tools` |

### Isolation (what one failure takes down)

| Path | Unit | How |
|---|---|---|
| vision | one page | an `LLMError` is caught per page; siblings keep running |
| ocr_text, reading | one page | a page the OCR model or local OCR cannot read is counted as failed |
| ocr_text, translating | one page | a failed batch is retried page by page, so one bad page costs one page |
| text volumes | one chunk | the failure message counts chunks, not pages |
| anything else a task raises | its unit | `pipeline._unexpected` counts what `gather` would otherwise swallow |
| memory | nothing | written in its own session; a bad payload rolls back only itself |
| volume outcome | | `failed > 0` ends the volume as `error` with a count, not `done` |
| resume | | finished pages are skipped (`_is_translated`, `_has_ocr`) |

### Cost

`llm.track_usage()` counts every model call made while a volume is processed:
calls, prompt and completion tokens, and time. `pipeline._save_usage` adds them
to the volume's `Job` (a resume adds, a fresh run starts again), and the API
returns an estimated cost when prices per million tokens are set in Settings
(`provider.estimate_cost`). The series page shows it on each volume.

### Quality

`services/quality.py` has two checks:

- `glossary_report` (`GET /api/volumes/{id}/quality`, the Consistency section of
  each volume): for every glossary term that occurs in a page's source text and
  was known by that page, whether the translation uses the agreed rendering.
  It lists the terms that drifted with their pages, and the terms the volume
  introduced. No model call.
- `regression_sample` (`backend/scripts/regression_sample.py`): re-translates a
  fixed sample of a volume with the current model and prompts, writes nothing,
  and reports how similar the result is to what is stored. It exits non-zero
  below a threshold, so it can gate a model or prompt change.

---

## 2. The librarian

![The librarian: a session loads its history, calls the model, runs the tools it asked for, and repeats.](images/librarian.png)

| Piece | File · function |
|---|---|
| Session rows | `db/models.py` `AgentSession`, `AgentMessage` (ordered by `seq`) |
| Loop | `services/agent.py` `_run`: load history (first 2 and last 60 messages), call `llm.chat_with_tools`, append, run tools, repeat |
| Step caps | ingest **60**, chat **12** (`agent._MAX_ITERS`) |
| Ask / suspend | `ask_user` raises `AskUser`, the session waits as `waiting_user`, and the user's reply resumes it from the stored history (`post_user_message`) |
| Restart | running sessions are marked `error`, never auto-resumed (`recover_orphans`) |
| Tools | `services/agent_tools.py` `TOOLS`, `TOOLSETS`, `run_tool`; results truncated to **8000** characters |
| HTTP | `routers/agent.py`: `POST /api/ingest`, `GET …/stream`, `POST …/message`, `GET /api/series/{id}/chat` |
| Panel | `components/AgentChat.tsx`, `ImportSession.tsx`, `createAgentStream` in `lib/api.ts` |

**SSE and `${BASE}`.** The stream endpoint polls the database every 0.7 s and
emits one `message` event per new `AgentMessage` with `seq > after`, plus a
`status` event whenever the session changes, and closes once the session stops
running. On the client, axios gets `baseURL: ${BASE}/api`, but `EventSource`
does not go through axios, so its URL is built by hand as
`${BASE}/api/agent/sessions/${id}/stream?after=${seq}`. `BASE` is Vite's
`BASE_URL` (from `VITE_BASE`), and the server mounts every router under
`URL_PREFIX`; both must match the mount point (see
[DEPLOYMENT.md](DEPLOYMENT.md)).

**The ingest session (20 tools)** inspects an upload, samples pages, reads
documents, finds and links metadata, creates the series and its volumes, and
repairs what arrived broken: `reorder_volumes`, `repair_volume_pages`,
`convert_pdf_to_scans`.

**The series chat (17 tools)** answers from the library: `search_text` (FTS5,
falling back to LIKE), `read_page` (up to 6000 characters), the summaries, the
glossary, and `get_memory`, which reads the series graph. It can also make the
repairs and metadata edits a user asks for.

**Guards in code, not just in the prompt.** `delete_volume`, the one
irreversible tool, refuses to run unless the user's latest message answered yes
to an `ask_user` question that named that volume (`agent_tools._user_confirmed`).
`enqueue_processing` is registered but in neither toolset: translation costs
money, so only the user starts it.

---

## 3. Series graph memory

The code is in `services/memory.py`. [GRAPH-MEMORY.md](GRAPH-MEMORY.md) walks
through it with diagrams: entities, the aliases that fold into them, typed edges
that each carry the position that established them, how names are resolved, and
how every prompt is limited to what was known by its page.

| What | Where |
|---|---|
| Tables `memory_entities`, `memory_aliases`, `memory_edges`; positions on `glossary_terms` | `db/models.py` |
| Extraction: the model returns `memory` in the translation JSON | `llm.translate_text`, `llm.vision_ocr_translate`, `llm._MEMORY_RULES` |
| Write, at the page's position, in its own session | `pipeline._merge_new_terms` → `memory.record_extraction`, `memory.record_renderings` |
| Resolution: normalise, exact alias, fuzzy ≥ 0.90, short form if one person fits | `memory.norm`, `memory.resolve_or_create`, `memory._short_form_of` |
| Relations: 19 verbs, kind-checked, symmetric ones stored once | `memory.RELATIONS`, `memory.relation_allowed` |
| Read, per model call | `pipeline._prompt_parts` → `memory.build_context`, `memory.glossary_for`, `memory.render_context` |
| Upkeep: date old rows, renumber on reorder, re-key aliases | `memory.ensure_positions`, `memory.renumber_volumes`, `memory.upgrade_norms` |
| Corrections | `routers/series.py` `/memory/merge`, `/memory/aliases/{id}/split`, `/memory/entities/{id}`, `/memory/edges/{id}`; `components/MemoryPanel.tsx` |
| Tests | `tests/test_memory.py`, `test_memory_review.py`, `test_memory_api.py`, and the memory cases in `test_pipeline.py` |

---

## What ships

| Capability | Where |
|---|---|
| Classify scans, skip artwork | `page_classifier.classify_page` |
| Vision engine, one call per page | `pipeline._run_vision` |
| OCR then translate, batched, retried per page on failure | `pipeline._run_ocr_text` |
| Text volumes, chunked per chapter | `pipeline._run_text_volume` |
| Region boxes from the vision model, in any of the scales models answer in | `llm._box` |
| Reader and PDF place each translation on its box | `PageView.tsx`; `export._render_boxes` |
| Glossary and graph memory, scoped to the page | `memory.glossary_for`, `memory.build_context` |
| Previous-volume summaries as context | `pipeline._previous_summaries` |
| Usage and estimated cost per volume | `llm.track_usage`; `Job`; `provider.estimate_cost` |
| Consistency report and regression sample | `services/quality.py`; `scripts/regression_sample.py` |
| Several volumes at once, one per series | `worker._dispatch` |
| Cached, streamed PDF and EPUB export | `routers/export._cached` |
| Local or S3 object storage | `storage_backend/local.py`, `s3.py` |
| `Process all` for a series (queues unfinished, skips done) | `routers/process.py` `process_series` |
| Repair tools: reorder, re-split, PDF to scans | `services/maintenance.py`; `ingest.render_pdf_group` |
| Glyph-soup detector for PDFs with no Unicode text | `documents._looks_like_glyph_soup` |
| Memory corrections in the UI | `MemoryPanel.tsx` |
