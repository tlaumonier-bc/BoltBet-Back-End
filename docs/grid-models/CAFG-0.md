# Model: CAFG-0 (Country-Activity Fixed-Grid, v0)

> Legacy / baseline zone-selection model for the grid game, captured as a named,
> versioned spec so it stays comparable to the EAGZ line (see EAGZ-1 §0). This
> documents the model **as implemented today**; it predates the EAGZ naming
> scheme, hence version `0`.

## 0. Naming & Versioning

This is model **CAFG-0** — *Country-Activity Fixed-Grid, version 0*. It is the
model in production before EAGZ-1. Same versioning convention as EAGZ:

```
CAFG-<major>.<minor>[-suffix]
CAFG-0        this spec (current production behaviour)
```

Kept as a spec so results logged under CAFG-0 can be A/B compared against EAGZ-1
and later variants.

## 1. Purpose

Decide **which areas are playable** and hand the client a grid to play on. In
CAFG-0 an "area" is an entire **country** (ISO-3166 alpha-2), and the grid is a
**fixed, geometry-free 8×10 index grid** — it is not sized or positioned from
strike data.

Round duration: **60 seconds** of play (`GRID_GAME_SECONDS = 60`) after a
**5-second** prepare countdown (`GRID_PREPARE_SECONDS = 5`).

## 2. High-Level Pipeline

```
CountryStrike stream (per-country retained, populated at ingest from lat/lon)
      │
      ▼
[Selection] Rank active countries by recent strike counts (30s, 5m)   → active-countries list
      │
      ▼
[Gate]      Chosen country must have >= 1 strike in the trailing 30s   → accept / not_playable
      │
      ▼
[Grid]      Fixed 8 cols x 10 rows, abstract cell indices 0..79 (NO geometry)
      │
      ▼
Match runs 60s. Two DECOUPLED scoring paths exist (see §5).
```

There is no observation window, no per-round recompute, no geographic zoning at
the selection stage. Selection happens once, at match creation.

## 3. Area Eligibility (country granularity)

`account/services.py:82` — `active_countries(limit=8)` (backs
`GET /api/game/grid/active-countries/`):

1. Candidate pool: countries with a strike in the **last 5 min**, excluding `""`
   and `"XX"` (unknown), ordered by 5-minute count, capped at
   `max(1, min(limit*3, 50))`.
2. For each candidate, count strikes in the **last 30 s**.
3. Final sort key `(strikes30s, strikes5m)` descending; return top `limit`
   (default 8, capped 20). Each row: `{country, strikes30s, strikes5m}`.

The endpoint advertises `windowSeconds: 30`, `fallbackWindowSeconds: 300`
(`account/views.py:469`).

**Playable gate at match start** (`account/views.py:493`):
`strikes_30s = country_recent_count(country, seconds=30)`; if `<= 0` →
HTTP 400 `{"error": "not_playable"}`. That single count is also stored on the
match as `strikes_30s_at_start` (used only to seed the bot).

All counts query `CountryStrike` by `received_at`.

## 4. Grid Definition (fixed, no sizing)

- `grid_cols = 8`, `grid_rows = 10`, hardcoded at creation
  (`account/views.py:517`). Note the axis convention: **cols=8, rows=10** (the
  client renders it as 10×8).
- Serialized as `"grid": {"cols": 8, "rows": 10}` (`account/views.py:127`).
- Cell is a bare index; click validation is only
  `0 <= cell < grid_cols * grid_rows` (= 80) (`account/views.py:563`).
- **No** cell size, bounding box, center, lat/lon span, or clamping. The server
  never converts a cell index to a coordinate. Geographic placement over the
  country map (if any) is a frontend-only visual.

## 5. Scoring & Settlement (TWO decoupled paths — known issue)

There are two independent scoring systems that do NOT agree with each other:

**A. Client-side / displayed score (geographic, strike-in-cell) — what the
player sees.** In `boltbet-frontend/components/grid-game/GridGameClient.tsx`:
- The play area is a sub-country box (`matchArea.bounds`) tiled into a
  10×8 grid (`AREA_GRID_COLS=10`, `AREA_GRID_ROWS=8`).
- `cellForStrike()` (line 544) projects each strike (Mercator + aspect) into a
  cell.
- **Player**: tapping a cell locks it for `CELL_LOCK_MS = 3000 ms`; every strike
  landing in that cell during the lock increments local `playerScore`
  (effect at line 1539).
- **Bot**: picks a *random* cell every 3 s; strikes landing in it increment
  local `botScore` (effect at line 1503).
- The "Match finished / You WON / score" overlay uses these LOCAL values.

**B. Backend / authoritative score (NON-geographic) — what actually sets Elo.**
- `player_score += 1` per `/click/` call (`account/views.py:567`) — BUT the
  frontend never calls `clickGridMatchCell` (it's dead code), so backend
  `player_score` stays 0.
- Bot (`account/services.py:125`, `bot_score_for`), a formula, reads no strikes:
  ```
  elapsed   = min(GRID_GAME_SECONDS, max(0, now - started_at))
  base_rate = clamp(strikes_30s_at_start / 45, 0.4, 4.0)
  skill     = clamp(bot_elo / 1200, 0.72, 1.28)
  score     = int(elapsed * base_rate * skill)
  ```
- **Settle** (`account/services.py:135`): `result` = 1/0.5/0 from backend
  `player_score` (≈0) vs backend `bot_score`; Elo K=32,
  `delta = round(32 * (result - expected))`.

**Consequence**: the on-screen win/score is strike-in-cell (correct intent), but
the Elo gained/lost is computed from an unrelated path (click-count 0 vs bot
formula), so the two can contradict. Making path A authoritative on the server
is a prerequisite for any fairness-based zoning model (e.g. EAGZ-1).

## 6. Cadence

- Country selection & grid assignment: **once, at match creation.** Nothing is
  recomputed during or across rounds. No streaming pipeline.

## 7. Parameters (CAFG-0 baseline)

| Parameter | Description | Value |
|---|---|---|
| `GRID_PREPARE_SECONDS` | Countdown before play | 5 s |
| `GRID_GAME_SECONDS` | Round duration | 60 s |
| Selection candidate window | Pool of active countries | 5 min |
| Playable / ranking window | Recent-activity gate + primary sort | 30 s |
| `min strikes to be playable` | Eligibility floor | 1 strike (in 30 s) |
| `grid_cols × grid_rows` | Fixed grid | 8 × 10 (indices 0..79) |
| `GRID_ELO_K` | Elo K-factor | 32 |
| `active-countries limit` | Countries returned | 8 (cap 20) |

## 8. Data Model Used

- `CountryStrike` (`lightning/models.py:62`) — retained rolling window (~newest
  10 000 per country, NOT purged), fields `country, lat, lon, timestamp,
  received_at, quality`; index `(country, -received_at)`. **This is the only
  strike table the grid game reads.**
- `GridMatch` (`account/models.py:103`) — stores `country`, `grid_cols`,
  `grid_rows`, scores, Elo before/after, `strikes_30s_at_start`, timing. **No
  geometry fields.**
- `GridPlayerStats` (`account/models.py:89`) — `grid_elo`, games, wins.

## 9. Known Limitations (motivation for EAGZ-1)

- Area granularity is a whole country; no sub-country / storm-cell targeting.
- The grid has no relationship to where strikes actually are — cell odds are not
  calibrated to anything.
- Outcome is a click-race vs a simulated bot; strikes never determine the
  result, so no cell-fairness property is (or can be) enforced.
- One area = one country means big countries with a single active storm still
  expose the whole country as "playable."

## 10. Reference (files)

- `account/services.py` — selection, windows, bot, Elo, settlement
  (lines 22-28, 53-114, 117-181).
- `account/views.py` — grid endpoints + payload (lines 117-154, 467-570).
- `account/models.py` — `GridMatch`, `GridPlayerStats` (lines 89-134).
- `lightning/models.py` — strike tables.
- `boltbet_backend/urls.py` — routes (lines 81-84).
