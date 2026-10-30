# Security

## Reporting a vulnerability

Report it privately through
[GitHub's advisory form](https://github.com/computator1200/tome/security/advisories/new),
not in a public issue. Include the version or commit, what an attacker can do,
and the steps to reproduce.

## Supported versions

Only the latest release gets fixes.

## Running Tome safely

Tome is a self-hosted app with **no login of its own**. Anyone who can reach the
backend can read the library, change settings (including the model endpoint and
key) and delete series. Bind it to `127.0.0.1` or a private network, or put
authentication in front of it at the reverse proxy; see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

The librarian agent reads uploaded files and web pages, which can carry
instructions aimed at the model. Its prompt treats them as data, and no tool can
delete without asking the user first, but treat uploads from people you do not
trust accordingly.
