# Deployment — Lightning Map Game Backend

App name (deployable / image / domains / Vault paths): **`lightning-map-game-backend`**
Python package (importable): **`lightning_map_game_backend`** (Python doesn't allow hyphens).

## Runtime model (current: consolidated)

For now everything runs in **one** service. The live ingest and the maintenance
tasks run as in-process daemon threads inside the Daphne web process, toggled by
`RUN_*_IN_PROCESS` env flags. The web process therefore runs as a **single
instance (`count: 1`)** because the ingest is a singleton writer (only writer of
the rollup counters, only broadcaster of strikes). Running more than one instance
would double-count rollups and duplicate strikes.

This is deliberately reversible — see "Splitting workers out later".

## Roles

| Role | Command | Public | Singleton | How it runs now |
| --- | --- | --- | --- | --- |
| Web / API / WebSocket | `daphne … lightning_map_game_backend.asgi:application` | Yes | Yes (`count: 1`) | The service container |
| Lightning ingest | `python manage.py start_lightning_stream` | No | **Yes** | In-process thread (`RUN_INGEST_IN_PROCESS`) |
| Retention purge | `python manage.py purge_strikes --hours 72` | No | No | In-process thread (`RUN_PURGE_IN_PROCESS`) |
| Per-country trim | `python manage.py trim_country_strikes --keep 1000` | No | No | In-process thread (`RUN_TRIM_IN_PROCESS`) |
| Bet resolver | `python manage.py resolve_strikes_bets --once` | No | No | In-process thread (`RUN_RESOLVER_IN_PROCESS`) |
| Migrations | `python manage.py migrate` | No | n/a | One-off job (see below) |

## Build & run

- Build command: `docker build` using the repo `Dockerfile`.
- Start command: `daphne -b 0.0.0.0 -p ${PORT:-8000} lightning_map_game_backend.asgi:application`
  (the `Dockerfile` CMD already does this).
- Port: the container honors `$PORT`; defaults to **8000**, which matches the
  port declared in the APPLICATIONS `app-definition.yml`.
- Health check path: **`/healthz`** (cheap, no auth, no DB).

## Migrations

Run as a one-off, not inside the web process, against the same image:

```bash
python manage.py migrate
```

(Kept as a separate command rather than a standing app, so we don't add a
second app while keeping the "backend + frontend only" naming.)

## Database

- **PostgreSQL in every environment.** There is no SQLite fallback.
- Local dev uses the docker-compose Postgres via localhost defaults.
- In production (`DJANGO_ENV=production`), `DB_HOST` must be set explicitly or
  the app refuses to start — no silent fallback.
- **Firestore is not the target database for this app.** Use managed Postgres
  (Cloud SQL / shared `shared-pg-17`).

## Base image

The `Dockerfile` keeps the internal `eu.gcr.io/blockchain-internal/...` Python
base images, because this app deploys through Blockchain's internal pipeline
(Consul service discovery + Vault templating), which expects them. Lars's
"public/approved Google-buildable Node/Python image" guidance applies to the
literal Cloud Run + Cloud Build path; if we move to that path, swap the base to
a standard `python:3.13-slim` (or approved equivalent). **Confirm with Alex/SRE
before changing.**

## Required environment variables

See `.env.example` for the full annotated list. Production-critical ones:

- `DJANGO_ENV`, `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`,
  `DJANGO_BEHIND_PROXY`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `CORS_ALLOWED_ORIGINS`
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
- `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`
- `RUN_INGEST_IN_PROCESS`, `RUN_PURGE_IN_PROCESS`, `RUN_TRIM_IN_PROCESS`, `RUN_RESOLVER_IN_PROCESS`
- `OWM_API_KEY`
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `OAUTH_PUBLIC_BASE` (if OAuth used)

Nothing here should be baked into the image; production values come from Vault.

## Expected backend URL shape

- REST API: `https://<api-host>/api/...` (also mounted at `/public/api/...`)
- WebSocket: `wss://<api-host>/ws/lightning/` (also `…/public/ws/lightning/`)
- Health: `https://<api-host>/healthz`

The frontend's `NEXT_PUBLIC_API_URL` / `NEXT_PUBLIC_WS_URL` should point at these.

## Splitting workers out later (no code change)

To move e.g. the ingest into its own service:

1. On the backend, set `RUN_INGEST_IN_PROCESS="false"` (the backend can then
   scale to `count: N`, since Channels fanout is Redis-backed).
2. Add a new APPLICATIONS app (e.g. `lightning-map-game-ingest`) that runs
   `python manage.py start_lightning_stream`, with `count: 1` and **no** service
   port block.
3. Point it at the **same Redis** as the backend (otherwise broadcasts never
   reach the web instances) and the **same Postgres**.
4. Give it a Vault policy for the DB (and Redis) secret paths.

Same recipe for purge / trim / resolver, except those go to a scheduled job
rather than an always-on singleton.

## Notes / sizing

- The reverse-geocoder dataset loaded by the ingest sits in memory for the life
  of the process. Watch for OOM at `memory: 512`; raise the APPLICATIONS memory
  if the ingest thread is killed on startup.
