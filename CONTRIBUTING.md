# Contributing

How to set up, the conventions, and the rules that have already cost time.

## Set up once

```bash
cp .env.example .env
python -m venv venv && ./venv/bin/pip install -r backend/requirements.txt
cd frontend && npm install && npm run dev     # Vite on :5173, proxies /api to :8077
```

Run the backend from `backend/`:

```bash
cd backend && ../venv/bin/uvicorn main:app --reload --port 8077
```

## Layout

- `backend/routers/` - HTTP only. Parsing, auth, status codes. No business logic.
- `backend/services/` - the work: pipeline, LLM calls, ingest, documents, agent.
- `backend/storage_backend/` - object store abstraction. Never `open()` a page
  file directly from a router.
- `frontend/src/components/` - one component per concern; shadcn primitives live
  in `components/ui/`.

## Rules that have already cost us time

1. **The database is SQLite and the worker is async.** Never block the event
   loop with `time.sleep`, synchronous HTTP or image work; use `asyncio.to_thread`
   or the executor. A blocked loop looks exactly like a hung app.
2. **One backend process per machine when the database is local.** Two uvicorn
   processes against the same SQLite file produce `database is locked` and a
   delete button that silently does nothing. Kill stray processes before
   wondering why writes fail.
3. **Never let one page kill a volume.** The pipeline isolates per page, records
   the failure, and continues. If you add a step, keep that property.
4. **Validate the model, not the prompt.** Anything the model returns is parsed
   defensively and rejected with a clear error, not trusted.
5. **Do not rename storage keys.** `volumes/<id>/pages/0000.jpg` is referenced by
   rows in the database; renaming a key type orphans every existing page.
6. **Subpath deployments need both knobs.** Build the frontend with
   `VITE_BASE=/tome/` and run with `URL_PREFIX=/tome`; every route, file URL and
   router basename is derived from those. Setting one and not the other gives a
   blank page or 404s on every asset.

## Tests

```bash
cd backend && python -m pytest tests -q     # language detection, ingest, API contract
cd frontend && npm run lint && npm run build
```

CI runs both on every push and pull request: the backend on Python 3.11 and
3.12, the frontend with a real production build, plus a guard that fails if a
database, `.env`, `node_modules` or `dist/` ever gets committed.

## Branches and commits

- `main` is always runnable.
- Work on `feat/<short-name>` or `fix/<short-name>`; keep changes small.
- Commit messages: `feat(memory): resolve aliases onto one node`,
  `fix(reader): keep page index when volume changes`.

## Pull requests

- One concern per PR. Say what you changed and how you checked it.
- A PR that changes behaviour should say how to see the change: the page to open,
  the endpoint to call, the screenshot.
- Do not commit `.env`, `backend/data/`, `backend/storage/`, `node_modules/` or
  `frontend/dist/`.

## Where to start

[docs/ROADMAP.md](docs/ROADMAP.md) has the open work.
[docs/ONBOARDING.md](docs/ONBOARDING.md) says where to start reading for each
area of the code, and lists the traps.

## Releasing

1. Bump `VERSION` and `frontend/package.json`, and add a section for the new
   version at the top of [CHANGELOG.md](CHANGELOG.md).
2. Merge to `main` and wait for CI to go green.
3. Tag and push: `git tag v$(cat VERSION) && git push origin v$(cat VERSION)`.

`.github/workflows/release.yml` checks that the tag matches `VERSION`, runs the
tests, builds the frontend, and publishes a GitHub release with that version's
CHANGELOG section as the notes and the built frontend attached.
