# Tome docs

Tome is a self-hosted, series-aware translation workspace for book series. It
translates a series volume by volume and keeps the series context (the glossary,
the memory graph and the summaries of earlier volumes) in front of the model, so
a character's name does not drift between volume 1 and volume 7.

These pages are the same in [`docs/`](https://github.com/computator1200/tome/tree/main/docs)
and the [wiki](https://github.com/computator1200/tome/wiki). Edit them in `docs/`;
`scripts/sync_wiki.py` copies them to the wiki.

## Pages

| | |
|---|---|
| [Architecture](ARCHITECTURE.md) | layout, the pipeline stage by stage, the librarian, what ships |
| [Graph memory](GRAPH-MEMORY.md) | how the series memory works, with diagrams of the write and read paths |
| [Models and agent skill](MODELS.md) | Gemma 4, swapping models, the portable librarian skill |
| [Deployment](DEPLOYMENT.md) | systemd, a reverse proxy, the two subpath knobs, S3 storage, the deploy workflow |
| [Roadmap](ROADMAP.md) | what shipped, and the choices made on purpose |
| [Onboarding](ONBOARDING.md) | where to start reading, the dev loop, traps |

Conventions, tests and the rules that have already cost time are in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## What is in the library

Not all of it is Japanese. Alongside the light novels the library holds
public-domain texts translated by the same pipeline with no per-language
special-casing: Voltaire's *Candide* in French, Kafka's *Die Verwandlung* in
German, Pérez Galdós' *Episodios nacionales* in Spanish (five volumes, ordered by
publication and paginated into chapters), and Persian prose: *Kelidar* and
*Shabe Sarab*.
