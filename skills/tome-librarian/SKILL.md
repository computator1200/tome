---
name: tome-librarian
description: Drive a self-hosted Tome instance - a series-aware book translation workspace. Lists series and volumes, reads a series' character and relationship memory so a translation stays consistent across volumes, reads the translated pages of a volume, and checks a series' metadata. Use when the user asks about their Tome library, asks what a passage in a translated volume says, asks who a character is in a series they are reading, or asks to inspect or fix a series' title, author, year, description or cover.
license: MIT
compatibility: Requires network access to a running Tome instance (default http://127.0.0.1:8077) and curl or any HTTP client. The instance may sit under a path prefix such as /tome.
metadata:
  author: computator1200
  version: "1.0"
  homepage: https://github.com/computator1200/tome
---

# Tome librarian

Tome is a self-hosted workspace for translating book **series**. The unit of work
is the series, not the page: a character introduced in volume 1 must still carry
the same name in volume 7. Each series therefore holds a memory graph of
characters, places and the relations between them, written as volumes are
translated and read back before the next one.

This skill drives a running instance over HTTP. It is a subset of the surface the
in-app librarian agent uses: reading, searching the library's structure, and
correcting metadata. It does not start or delete work.

## Before you start

Confirm the instance is up and note its version, so you know which endpoints
exist:

```bash
BASE="${TOME_BASE_URL:-http://127.0.0.1:8077}"
curl -s "$BASE/api/health"
curl -s "$BASE/api/version"
```

If the app is served under a subpath, `BASE` includes it (`http://host/tome`),
and that must match the server's `URL_PREFIX`.

If the server sets `TOME_API_KEY`, send `Authorization: Bearer $TOME_API_KEY` on
every request. Never echo the key in output you show the user.

## Orient: which series, which volume

```bash
curl -s "$BASE/api/series" | jq '.[] | {id, title, author, volume_count}'
curl -s "$BASE/api/series/SERIES_ID/volumes" \
  | jq '.[] | {id, label, order, kind, status, page_count}'
```

- `kind` is `scan` (page images, translated by the vision engine) or `text` (an
  EPUB or PDF that already carried text).
- `status` moves `uploaded → processing → done`. A volume still at `uploaded`
  has no translation yet, so do not quote it as if it did.

## Read the memory before answering anything about a character

This is what separates Tome from a file browser. Ask for the memory **scoped to
the volume the reader is on**, so what you say reflects what the translation had
established by that point rather than what a later volume reveals:

```bash
curl -s "$BASE/api/series/SERIES_ID/memory?volume=3" \
  | jq '{stats, truncated, entities: [.entities[] | {name, kind}], edges}'
```

- `entities` are canonical names with their aliases; `edges` carry `src`,
  `relation`, `dst`, `volume`.
- The payload is capped at **80 entities and 60 edges**. Check `truncated`: when
  it is true, `stats` still holds the true totals, so say "the graph knows 312
  entities" instead of implying you read them all.
- A **400** means that volume number does not exist in this series yet. Omit
  `volume` to read the whole-series view.

## Read a volume

```bash
curl -s "$BASE/api/volumes/VOLUME_ID" > /tmp/vol.json
jq -r '.pages[] | "## page \(.index) \(.chapter_title // "")\n" +
       ([.regions[] | .english_text] | join("\n"))' /tmp/vol.json
jq -r '.pages[].regions[] | .japanese_text' /tmp/vol.json   # the source text
```

- The whole volume, pages and regions, arrives in one response. A 295-page scan
  volume is a few megabytes, so write it to a file rather than re-fetching.
- Region fields keep their original names: `japanese_text` holds the **source**
  text whatever the source language is, `english_text` holds the translation.
- `x`, `y`, `width`, `height` are normalised coordinates. They are **0** for pages
  that were never scanned, so do not describe the reader as an overlay.

Progress and exports:

```bash
curl -s "$BASE/api/volumes/VOLUME_ID/progress" | jq
curl -s -o volume.pdf "$BASE/api/volumes/VOLUME_ID/export.pdf"
curl -s -o volume.epub "$BASE/api/volumes/VOLUME_ID/export.epub"
```

An export of an untranslated volume answers **400** with a message saying so;
that is not an error to retry.

## Metadata

```bash
curl -s "$BASE/api/series/SERIES_ID" | jq '{title, author, year, description, poster_url}'
curl -s "$BASE/api/metadata/search?q=TITLE" | jq '.[] | {source, ref, title, author, year}'
```

A record with no cover and no description is unfinished. When the user asks for
one, search first and then link exactly one result - never invent a synopsis:

```bash
curl -s -X POST "$BASE/api/series/SERIES_ID/metadata/link" \
  -H 'content-type: application/json' \
  -d '{"source": "openlibrary", "ref": "/works/OL...W"}'
```

## What this skill must not do

- **Do not start a translation run.** `POST /api/volumes/{id}/process` spends
  model usage and changes the volume's state. The user presses Process in the UI;
  ask before doing anything of the kind, and never retry a failed run unasked.
- **Do not delete anything.** `DELETE /api/series/{id}` and
  `DELETE /api/volumes/{id}` take the translated text with them and cannot be
  undone.
- **Do not create a duplicate series.** List first; if the title already exists,
  ask which record the user means.
- **Do not claim a page is translated** because a row exists. Check
  `page_count` and that regions carry `english_text`.

## Endpoints

Request and response shapes, including the agent-session and ingest endpoints:
[references/api.md](references/api.md).
