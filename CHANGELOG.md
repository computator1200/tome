# Changelog

All notable changes are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.2.0] - 2026-10-30

### Added

- **Page-scoped memory.** Every name, relation and glossary term records the
  page where it was first seen, and each model call gets a memory block and
  glossary built for its own page: names in the text first, with their English
  renderings and other forms. Nothing from a later page or volume reaches a
  prompt, on re-runs and after reordering volumes too.
- **Memory corrections.** Merge, split and delete entries from the series page,
  and a `get_memory` tool so the series chat can answer from the graph.
- **Bounding boxes.** The vision model returns a box for each block; the reader
  and the PDF set each translation over the block it replaces.
- **Consistency report** per volume, and a regression-sample script that scores
  a re-translation of fixed pages against what is stored.
- **Cost per volume**: model calls, tokens and time, with an estimated cost once
  prices are set in Settings.
- **Several volumes at once**, never two of the same series.
- **S3 storage** (`STORAGE_BACKEND=s3`), for AWS and S3-compatible servers.
- Exports are cached on disk and streamed from the file.

### Changed

- Name resolution follows the evidence: aliases the model lists attach to their
  entity; a short name resolves only when exactly one person fits, and never
  across two names on the same page. Siblings who share a family name, or
  katakana names that share a prefix, are no longer merged.
- Normalisation no longer turns words into other words when stripping
  honorifics (神殿 is not 神), and unifies spaces, separators and variant kanji
  in names.
- Both translate prompts list all 19 relation verbs; anything else is dropped.
- `delete_volume` checks for the user's yes in code, not only in the prompt.

### Fixed

- A page the OCR model could not read is counted as failed, so the volume no
  longer finishes as done with pages missing.
- One local OCR failure no longer fails the whole volume.
- Exceptions other than model errors are counted instead of swallowed.
- A malformed memory payload can no longer break the rest of a volume.
- Text-volume failures are reported as chunks, which is what they count.
- The app serves its JavaScript with the right type on Windows.

### Upgrading

Start the new version once and it migrates the database in place. Memory and
glossary rows written by 0.1.0 have no page; the first run of each series dates
them from the text, and hides glossary terms the text never contains until a
translation sees them. Re-translate a scan volume to give its pages boxes.

## [0.1.0] - 2026-10-30

First public release.

### Added

- Library of series, volumes and pages, with reading progress and per-series
  settings.
- Ingest of scan archives (zip, rar, 7z, folders of images) and digital books
  (EPUB, PDF, TXT, HTML, AZW3), with the format detected from magic bytes and
  legacy-encoded filenames repaired.
- Two translation engines: a vision path that reads the page image and returns
  translated regions in one call, and an OCR-then-translate path.
- Series graph memory: entities, aliases and typed relations extracted during
  translation, with each volume's prompt scoped to what was established by that
  volume.
- A librarian agent with 26 tools that organises uploads, links metadata from
  keyless book catalogues, and repairs volume order, page splits and PDFs with no
  text layer.
- `Process all` for a whole series, resumable volumes, and PDF and EPUB export.
- A portable Agent Skill for driving a Tome instance from other agents.
- CI on every push and pull request, and a tag-driven release workflow.

### Known limitations

- Memory is built once per volume run, not per page.
- Region bounding boxes are not filled in, so the reader is a text panel rather
  than an overlay.
- On the OCR path, one failed response fails the rest of its batch.

See [docs/ROADMAP.md](https://github.com/computator1200/tome/blob/v0.1.0/docs/ROADMAP.md#open)
for the full list.

[Unreleased]: https://github.com/computator1200/tome/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/computator1200/tome/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/computator1200/tome/releases/tag/v0.1.0
