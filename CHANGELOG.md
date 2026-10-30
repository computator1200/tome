# Changelog

All notable changes are recorded here. Versions follow
[Semantic Versioning](https://semver.org/); the format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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

See [docs/ROADMAP.md](https://github.com/computator1200/tome/blob/main/docs/ROADMAP.md#open)
for the full list.

[Unreleased]: https://github.com/computator1200/tome/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/computator1200/tome/releases/tag/v0.1.0
