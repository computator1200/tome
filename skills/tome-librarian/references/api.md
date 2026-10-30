# Tome HTTP surface

Every route is mounted under the server's `URL_PREFIX` (`/tome` in the reference
deployment), so `BASE` below means `http://host` plus that prefix.

All request and response bodies are JSON unless the route returns a file.

## Health and identity

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/health` | `{"status": "ok", "capabilities": {"local_ocr": bool, "pdf": bool}}` |
| GET | `/api/version` | `{"version": "0.3.0", "commit": "…", "started_at": "…"}` |

`capabilities.local_ocr` is false when `torch` and the OCR model are not
installed: only the vision engine can translate.

## Series

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/series` | List. Each item: `id`, `title`, `author`, `year`, `volume_count`, `poster_url` |
| POST | `/api/series` | `{"title": "...", "author": "...", "year": 1943}`: creates an empty series |
| GET | `/api/series/{id}` | The record plus `volumes[]`, `page_count`, `genres`, `source_language`, `detected_language` |
| PATCH | `/api/series/{id}` | `title`, `description`, `source_language`, `target_language` |
| DELETE | `/api/series/{id}` | **Destructive.** Removes the series, its volumes and their text |
| POST | `/api/series/{id}/metadata/link` | `{"source": "...", "ref": "..."}`: copies that record's fields and cover onto the series |
| POST | `/api/series/{id}/poster` | Multipart `file`: the image bytes |

## Memory graph

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/series/{id}/memory` | Optional `?volume=N` scopes to what was known by that volume |

```json
{
  "stats": {"entities": 312, "edges": 165, "aliases": 88},
  "truncated": {"entities": true, "edges": true},
  "scoped_to_volume": 3,
  "entities": [{"id": "…", "name": "Ana Ozores", "kind": "character", "aliases": ["la Regenta"]}],
  "edges": [{"src": "Ana Ozores", "relation": "married_to", "dst": "Víctor Quintanar", "volume": 1}]
}
```

- Capped at 80 entities and 60 edges per response; `stats` reports the true totals
  and `truncated` says whether the cap bit.
- `volume=99` on a series with 18 volumes answers **400**, not an empty graph.
- Relations come from a closed vocabulary so the graph stays queryable.

## Volumes

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/series/{id}/volumes` | Ordered list, without pages |
| POST | `/api/series/{id}/volumes` | Query params `label`, `number`, `engine_mode`, `batch_size`; body is the uploaded file |
| POST | `/api/series/{id}/process` | Queues every unfinished volume in reading order; body `{"force": bool}` re-runs finished ones |
| GET | `/api/volumes/{id}` | The volume **with every page and every region** |
| DELETE | `/api/volumes/{id}` | **Destructive** |
| GET | `/api/volumes/{id}/progress` | Server-sent events: `event: progress` then a JSON data frame |
| POST | `/api/volumes/{id}/process` | Enqueues a run. **The user's click, not yours** |
| POST | `/api/volumes/{id}/cancel` | Cancels a queued or running job |
| POST | `/api/volumes/{id}/resume` | Resumes from the last finished page |
| GET | `/api/volumes/{id}/export.pdf` | 400 if the volume has no translation yet |
| GET | `/api/volumes/{id}/export.epub` | Same rule |

Repairs (`reorder_volumes`, `repair_volume_pages`, `convert_pdf_to_scans`,
`delete_volume`) are agent tools rather than routes: they change the library, and
the librarian is the only caller that is expected to explain what it changed.

Volume payload:

```json
{
  "id": "a1976bf1e3e4", "label": "Volume 1", "order": 1,
  "status": "done", "kind": "scan", "engine_mode": "vision",
  "page_count": 295, "summary": "…",
  "job": {"status": "done", "done": 295, "total": 295},
  "pages": [{
    "id": "781c4c071b39", "index": 0, "page_type": "text",
    "chapter_title": "Trafalgar", "width": 1200, "height": 1800,
    "image_url": "/tome/files/pages/…/0.jpg",
    "regions": [{
      "id": "ca6cd613a428", "order": 0,
      "x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0,
      "japanese_text": "…source text…",
      "english_text": "…translation…",
      "confidence": 0.94
    }]
  }]
}
```

Region field names are historical: `japanese_text` is the **source** text for any
language, `english_text` is the translation. Coordinates are normalised and are
`0` on pages that were never scanned.

## Ingest

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/ingest` | Multipart: one or more `files`, plus optional `series_id` and `note`. Answers `{"session_id": "…"}` |

Ingest hands the files to the librarian agent, which inspects them, groups them
into volumes and finishes the metadata. Follow it through the session endpoints
below.

## Agent sessions

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/agent/sessions/{id}` | The transcript: `messages[]` with `role`, `content`, `tool_name` |
| GET | `/api/agent/sessions/{id}/stream` | Server-sent events while the session runs |
| POST | `/api/agent/sessions/{id}/message` | `{"content": "..."}`: reply to the agent, or steer it |
| POST | `/api/agent/sessions/{id}/cancel` | Stop the run |
| GET | `/api/series/{id}/agent/sessions` | Sessions attached to a series |
| GET | `/api/series/{id}/chat` | The series' long-running chat session |
| POST | `/api/series/{id}/chat/new` | Start a fresh one |
| GET | `/api/agent/alerts` | Notifications waiting for the user |

A transcript entry is either prose (`role: "assistant"`) or a tool result
(`role: "tool"`, `tool_name`, `content` holding that tool's output). Tool output is
JSON or plain text depending on the tool; parse defensively.

## Metadata catalogues

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/metadata/search?q=…` | Book catalogues: Open Library, Internet Archive, DNB, BnF, and Google Books when a key is set |
| GET | `/api/novelupdates/search?q=…` | Series records for web and light novels |

A search result carries `source` and `ref`; pass both back to
`/api/series/{id}/metadata/link` unchanged.

## Files

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/files/{key}` | Page images, posters and exports. Keys are storage paths, not user input |

## Settings

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/settings` | Provider configuration; API keys are masked |
| PUT | `/api/settings` | Partial update of `provider.*`, `translation.*`, `ocr.*` |
| POST | `/api/settings/test` | Round-trips the configured model and reports latency |

## Errors

Errors are `{"detail": "..."}` with a 4xx status. Worth handling by name:

- **400**: a volume with no translation was exported, or `volume=` names a volume
  the series does not have.
- **404**: unknown id, or an id from a different series.
- **409**: a job for that volume is already running; cancel it first.
