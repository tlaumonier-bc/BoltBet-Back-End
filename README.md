# Lightning Map Game — Backend

Django ASGI backend for **Lightning Map Game**: a realtime lightning-strike map
with an Up/Down betting mini-game. It serves a REST API and a WebSocket stream,
ingests live strikes from Blitzortung, and persists them to PostgreSQL.

## Stack

- Django 6 + Django REST Framework, served by **Daphne** (ASGI)
- **Django Channels** over **Redis** (WebSocket fanout)
- **PostgreSQL** in every environment — there is no SQLite
- Live ingest, retention purge, per-country trim, and the bet resolver run as
  opt-in in-process daemon threads (see `lightning/background.py`)

## Apps

- `lightning` — strike ingest, storage, rollups, strike/weather API, WebSocket consumer
- `account` — players, sessions, OAuth, and the Up/Down betting game

(The legacy zone-game `game` app and the old `bets` app have been removed.)

## Local development

Requires Docker (for Postgres + Redis) and Python 3.13.

```bash
cp .env.example .env
docker compose up -d            # Postgres + Redis
pip install -r requirements.txt
python manage.py migrate

# API + WebSocket:
daphne -b 0.0.0.0 -p 8000 lightning_map_game_backend.asgi:application
# or:
python manage.py runserver
```

To exercise the live ingest locally, set `RUN_INGEST_IN_PROCESS="true"` in `.env`.

## Environment variables

See `.env.example` for the full list. Production values are injected by the
platform (Vault); see `DEPLOYMENT.md`.

## Deployment

See `DEPLOYMENT.md` for the runtime roles, commands, health check, required env
vars, the expected backend URL shape, and how to split the background workers
into their own services later.
