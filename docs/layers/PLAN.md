# Grid Game — Map Layers: backend plan

Wire the four "Soon" layers to live data. **Hard constraint:** playable zones are
global and often over open ocean, so every provider must return data for *any*
lat/lon on Earth. A layer must never render "no data" on a valid zone — if it
does, that's a bug or the wrong provider. This is enforced by an automated
coverage test over the current live zones (≥2, must pass on all).

## Provider decisions

| Layer | Provider | Why (free + global + ocean) | Rejected alternatives |
|---|---|---|---|
| **Storm tracks** (cell trajectory & speed) | **Our own Blitzortung strike store** (backend `CountryStrike`) | A zone exists *because* strikes exist there → motion is always computable. Zero external dependency, guaranteed 100% coverage. | RainViewer radar (no global/ocean), NOAA MRMS (US only). |
| **Rain** (precipitation) | **Open-Meteo** `precipitation`/`rain` | Validated over open ocean; global model (GFS); no key; bulk multi-point. | OWM `precipitation_new` tiles (already proxied) — usable as a raster fallback, but ties us to the OWM key + 60 rpm. |
| **Storm risk** (CAPE) | **Open-Meteo** `cape` (J/kg) | **Only** free source that exposes CAPE, and it's global incl. ocean (validated: 360 J/kg mid-Atlantic). | OWM free (no CAPE), Tomorrow.io/Windy (key + tight limits). |
| **Wind** (speed & direction) | **Open-Meteo** `wind_speed_10m` / `wind_direction_10m` / `wind_gusts_10m` | Vector values → real arrows; global+ocean; no key. | OWM `wind_new` tiles (fallback raster). |

**One weather integration.** Rain + Storm risk + Wind all come from a *single*
Open-Meteo bulk call per zone (all three variable groups, all sample points, one
request). Storm tracks come from data we already own. Open-Meteo needs **no API
key**, so it works in dev and can't 503 like the current OWM proxy.

Validation already run:
- Ocean point (30 N, 40 W): `precipitation`, `rain`, `wind_*`, `cape` all returned.
- Bulk `latitude=a,b,c&longitude=…` returns an array — sample a whole zone in 1 call.
- Force `models=gfs_seamless` so `best_match` can't pick a land-only hi-res model that would leave ocean gaps.

> **Licensing caveat (must confirm before production):** Open-Meteo is free for
> non-commercial use; commercial use needs their paid tier or self-hosting (it's
> open-source). Fine for dev + these tests; flag for the production launch.

## Backend design (Django `lightning/`)

Reuse the existing patterns in `lightning/views.py` (`urllib.request`, `django.core.cache`,
allowlist, rate-limit guard). New module `lightning/weather.py` + views + URLs.

### 1. Weather (rain + risk + wind) — one endpoint
`GET /api/weather/zone/?minLat=&maxLat=&minLon=&maxLon=&n=4`
- Samples an `n×n` grid of points across the bbox (weather is ~uniform at ~40 km,
  so n=4 → 16 points is plenty), one Open-Meteo bulk call:
  `current=precipitation,rain,wind_speed_10m,wind_direction_10m,wind_gusts_10m` +
  `hourly=cape` (nearest hour) + `models=gfs_seamless`.
- Response:
  ```json
  { "sampledAt": "…", "points": [
      { "lat": 12.3, "lon": -40.1, "precip": 0.4, "windSpeed": 18.2,
        "windDir": 250, "gust": 26.0, "cape": 1200 } ],
    "summary": { "capeMax": 1800, "capeAvg": 1100, "precipMax": 2.1,
                 "windAvg": 16.0, "windDir": 250 } }
  ```
- **Cache** per rounded bbox key (`round(minLat,1)…`) for `WEATHER_CACHE_SECONDS`
  (default 900 s / 15 min) — weather changes slowly; keeps us far under limits.
- Coverage guarantee: Open-Meteo returns a value for every point; if any point is
  null the endpoint logs + still returns others (the coverage test flags it).

### 2. Storm tracks — from our strikes
`GET /api/weather/storm-track/?minLat=&maxLat=&minLon=&maxLon=`
- Pull strikes in the bbox over the last ~10 min from `CountryStrike`/`LightningStrike`.
- Split into two windows (e.g. [−10,−5] min vs [−5,0] min), weighted centroid of
  each, velocity = Δcentroid / Δt → **heading (deg)**, **speed (km/h)**, and a
  **projected centroid** a few minutes out. Also return per-window centroid for a
  trail.
- Response: `{ heading, speedKmh, from:{lat,lon}, to:{lat,lon}, projected:{lat,lon}, samples:n }`.
- Cache ~30 s. Always has data for a valid zone.

### Frontend (`lib/api.ts` + `components/grid-game/GridGameClient.tsx`)
- `getZoneWeather(bounds)` and `getStormTrack(bounds)` in `api.ts`.
- Fetch once per active zone (weather every ~2 min via the cache, track every ~10 s),
  keyed on `zoneKey` (same pattern as the strike/cities feeds).
- Flip each entry in `SIDE_LAYERS` to `ready:true` and add a toggle in the layer state.
- Render inside `ZoneMap`, layered under the grid (like the density heatmap):
  - **Wind:** arrows at sample points (length=speed, angle=dir), gentle drift animation.
  - **Rain:** translucent blue intensity blobs/overlay from `precip`.
  - **Storm risk:** color tint + a CAPE gauge/badge (green <1000, amber 1000–2500, red >2500 J/kg).
  - **Storm tracks:** a bold arrow from `from`→`projected` + a faint trail + "moving NE · 35 km/h".

## Coverage test (`boltbet-frontend/test/layers-coverage.mjs`)
The gate the user asked for. Every run:
1. Fetch live global strikes → `detectZones` → current zones (assert ≥2).
2. For **every** zone, hit each layer's data path:
   - weather → Open-Meteo bulk for the zone's sample points;
   - storm-track → strikes-in-bounds drift.
3. Assert **every zone returns valid data for every layer**. Print a matrix
   (zone × layer → OK / MISSING). **Exit non-zero if any cell is MISSING** — that
   means a bug or a provider that doesn't cover that zone (→ fix or swap provider).

Run: `node test/layers-coverage.mjs`.

## Rollout (each milestone ends by running the coverage test on ≥2 zones)
1. **M1 — Backend `/api/weather/zone/`** (Open-Meteo bulk + cache) + coverage test. No UI yet; prove data on all zones incl. ocean.
2. **M2 — Wind layer** (arrows). Simplest visual; validates the end-to-end pipeline.
3. **M3 — Rain layer** (overlay).
4. **M4 — Storm risk layer** (CAPE index + tint).
5. **M5 — Storm tracks** (`/api/weather/storm-track/` + arrow/trail render).

## Config (env, all with sane defaults)
`WEATHER_CACHE_SECONDS=900`, `WEATHER_SAMPLE_N=4`, `OPEN_METEO_MODEL=gfs_seamless`,
`STORM_TRACK_WINDOW_MIN=10`, `STORM_TRACK_CACHE_SECONDS=30`.
