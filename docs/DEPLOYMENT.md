# Deployment

Tome runs as one uvicorn process behind a reverse proxy. The examples below
serve it at `https://example.com/tome/` as systemd unit `tome.service` on
`127.0.0.1:8078`, with Caddy on the same box.

## Two knobs, both required for a subpath

- `VITE_BASE=/tome/` at build time: Vite emits `/tome/assets/...`.
- `URL_PREFIX=/tome` at runtime: FastAPI mounts routes and builds file URLs
  under the prefix. The proxy must **not** strip it. Setting one without the
  other gives a blank page or 404s on every asset.

## systemd

```ini
WorkingDirectory=/srv/tome/backend
EnvironmentFile=/srv/tome/.env
ExecStart=/srv/tome/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8078 --proxy-headers
Restart=on-failure
MemoryMax=1536M
```

`--proxy-headers` matters: without it the app sees the proxy's address and the
URLs it generates lose the host.

## Caddy

```caddyfile
redir /tome /tome/ 308
handle /tome/* {
	reverse_proxy 127.0.0.1:8078
}
```

The bare `/tome` redirect is load-bearing: `/tome/*` does not match `/tome`, so
without it the request falls through to whatever else the site serves and 404s.
Tome has no login of its own; if the instance is reachable from the internet,
put authentication in front of it at the proxy (for example Caddy's
`forward_auth`).

## One backend process per SQLite database

`DATABASE_URL` is a local SQLite file. Two uvicorn processes against it give
`database is locked`, and the visible symptom is a delete button that silently
does nothing. Look for a stray process before debugging writes.

## Deploying

`.github/workflows/deploy.yml` is manual only (`workflow_dispatch`): a live
instance should not restart because someone pushed to `main`. It builds with
`VITE_BASE=/<target>/`, rsyncs `backend/` and `frontend/dist/` to
`/srv/<target>/` (excluding `venv`, `data`, `storage`, `.env`, `logs`), then
restarts the unit and asserts it is active. Secrets: `DEPLOY_HOST`,
`DEPLOY_USER`, `DEPLOY_SSH_KEY`. Check with `systemctl is-active tome` and
`curl -s -o /dev/null -w '%{http_code}\n' https://example.com/tome/`.
