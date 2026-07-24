"""
lightning/weather.py — per-zone weather for the Grid Game map layers.

Single free provider: Open-Meteo (no API key, global incl. open ocean, exposes
CAPE). One bulk multi-point request samples an n×n grid across a zone's bbox and
returns precipitation, wind (speed/dir/gusts) and CAPE at each point plus a
summary. Powers the Rain, Wind and Storm-risk (CAPE) layers.

`open_meteo_zone()` is a PURE function (only urllib + stdlib, no Django) so it can
be unit-tested in isolation; `weather_zone()` is the cached DRF view.

Docs: docs/layers/PLAN.md  ·  Coverage gate: boltbet-frontend/test/layers-coverage.mjs
"""

import json
import math
import os
import urllib.parse
import urllib.request
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import LightningStrike

# ── config (env, sane defaults) ──────────────────────────────────────────────
WEATHER_CACHE_SECONDS = int(os.environ.get("WEATHER_CACHE_SECONDS", "900"))  # 15 min
WEATHER_SAMPLE_N = int(os.environ.get("WEATHER_SAMPLE_N", "4"))               # n×n grid
# Force a GLOBAL model so ocean zones never come back empty (best_match can pick a
# land-only hi-res model). gfs_seamless is global and includes CAPE.
OPEN_METEO_MODEL = os.environ.get("OPEN_METEO_MODEL", "gfs_seamless")
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_VARS = "precipitation,rain,wind_speed_10m,wind_direction_10m,wind_gusts_10m"

STORM_TRACK_WINDOW_MIN = int(os.environ.get("STORM_TRACK_WINDOW_MIN", "10"))
STORM_TRACK_CACHE_SECONDS = int(os.environ.get("STORM_TRACK_CACHE_SECONDS", "30"))
STORM_TRACK_PROJECT_MIN = int(os.environ.get("STORM_TRACK_PROJECT_MIN", "5"))
KM_PER_DEG = 111.32
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def _sample_grid(min_lat, max_lat, min_lon, max_lon, n):
    """n×n cell-center sample points across the bbox."""
    pts = []
    for r in range(n):
        lat = min_lat + ((r + 0.5) / n) * (max_lat - min_lat)
        for c in range(n):
            lon = min_lon + ((c + 0.5) / n) * (max_lon - min_lon)
            pts.append((round(lat, 4), round(lon, 4)))
    return pts


def _cape_for_hour(loc, current_time):
    """Nearest hourly CAPE to the 'current' timestamp (Open-Meteo CAPE is hourly)."""
    hourly = loc.get("hourly") or {}
    times = hourly.get("time") or []
    capes = hourly.get("cape") or []
    if not times or not capes:
        return None
    hour_prefix = (current_time or "")[:13]  # "YYYY-MM-DDTHH"
    for i, t in enumerate(times):
        if t.startswith(hour_prefix) and i < len(capes):
            return capes[i]
    # fallback: first non-null value
    for v in capes:
        if v is not None:
            return v
    return None


def open_meteo_zone(min_lat, max_lat, min_lon, max_lon, n=WEATHER_SAMPLE_N, model=OPEN_METEO_MODEL, timeout=8):
    """Fetch rain/wind/CAPE over a zone. Returns {sampledAt, model, points, summary}.

    Raises on network/parse failure. Pure (no Django) — safe to unit-test.
    """
    n = max(2, min(6, int(n)))
    pts = _sample_grid(min_lat, max_lat, min_lon, max_lon, n)
    q = urllib.parse.urlencode({
        "latitude": ",".join(str(p[0]) for p in pts),
        "longitude": ",".join(str(p[1]) for p in pts),
        "current": CURRENT_VARS,
        "hourly": "cape",
        "models": model,
        "forecast_days": 1,
        "timezone": "UTC",
    })
    req = urllib.request.Request(f"{OPEN_METEO_URL}?{q}", headers={"User-Agent": "lightning-map-game/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())

    # Multi-location → list; single location → object. Normalize to a list.
    locs = data if isinstance(data, list) else [data]

    points = []
    sampled_at = None
    for loc in locs:
        cur = loc.get("current") or {}
        sampled_at = sampled_at or cur.get("time")
        points.append({
            "lat": loc.get("latitude"),
            "lon": loc.get("longitude"),
            "precip": cur.get("precipitation"),
            "rain": cur.get("rain"),
            "windSpeed": cur.get("wind_speed_10m"),
            "windDir": cur.get("wind_direction_10m"),
            "gust": cur.get("wind_gusts_10m"),
            "cape": _cape_for_hour(loc, cur.get("time")),
        })

    return {
        "sampledAt": sampled_at,
        "model": model,
        "points": points,
        "summary": _summarize(points),
    }


def _summarize(points):
    capes = [p["cape"] for p in points if p["cape"] is not None]
    precips = [p["precip"] for p in points if p["precip"] is not None]
    winds = [p["windSpeed"] for p in points if p["windSpeed"] is not None]
    # circular mean of wind direction, weighted by speed
    sx = sy = 0.0
    for p in points:
        if p["windDir"] is None:
            continue
        w = (p["windSpeed"] or 0) + 0.1
        rad = math.radians(p["windDir"])
        sx += w * math.sin(rad)
        sy += w * math.cos(rad)
    wind_dir = (math.degrees(math.atan2(sx, sy)) + 360) % 360 if (sx or sy) else None
    return {
        "capeMax": max(capes) if capes else None,
        "capeAvg": round(sum(capes) / len(capes)) if capes else None,
        "precipMax": max(precips) if precips else None,
        "windAvg": round(sum(winds) / len(winds), 1) if winds else None,
        "windDir": round(wind_dir) if wind_dir is not None else None,
    }


@api_view(["GET"])
def weather_zone(request):
    """
    Rain / wind / CAPE sampled over a zone bbox (Open-Meteo, cached).
    GET /api/weather/zone/?minLat=&maxLat=&minLon=&maxLon=&n=4
    """
    try:
        min_lat = float(request.GET["minLat"])
        max_lat = float(request.GET["maxLat"])
        min_lon = float(request.GET["minLon"])
        max_lon = float(request.GET["maxLon"])
    except (KeyError, ValueError):
        return Response({"error": "bbox_required"}, status=400)
    if max_lat <= min_lat or max_lon <= min_lon or (max_lat - min_lat) > 30 or (max_lon - min_lon) > 30:
        return Response({"error": "bad_bbox"}, status=400)

    try:
        n = max(2, min(6, int(request.GET.get("n", WEATHER_SAMPLE_N))))
    except ValueError:
        n = WEATHER_SAMPLE_N

    ckey = f"wxzone:{round(min_lat, 1)}:{round(max_lat, 1)}:{round(min_lon, 1)}:{round(max_lon, 1)}:{n}"
    cached = cache.get(ckey)
    if cached:
        return Response(cached)

    try:
        out = open_meteo_zone(min_lat, max_lat, min_lon, max_lon, n=n)
    except Exception:
        return Response({"error": "weather_fetch_failed"}, status=502)

    cache.set(ckey, out, WEATHER_CACHE_SECONDS)
    return Response(out)


# ── storm-cell motion (from our own strike store — always covers a valid zone) ─
def compute_track(points, now_ms, window_ms=None, project_ms=None, min_per_half=3):
    """Storm-cell trajectory from strike-centroid drift.

    `points`: iterable of (lat, lon, t_ms). Splits the trailing window into an
    older and a recent half, takes each half's centroid, and derives heading,
    speed (km/h) and a projected centroid. Returns a dict or None when there
    isn't enough time history yet (a brand-new storm — "forming", not an error).
    """
    window_ms = window_ms or STORM_TRACK_WINDOW_MIN * 60_000
    project_ms = project_ms or STORM_TRACK_PROJECT_MIN * 60_000
    mid = now_ms - window_ms / 2
    older, recent = [], []
    for lat, lon, t in points:
        if t < now_ms - window_ms or t > now_ms:
            continue
        (recent if t >= mid else older).append((lat, lon))
    if len(older) < min_per_half or len(recent) < min_per_half:
        return None

    def centroid(rows):
        return (sum(r[0] for r in rows) / len(rows), sum(r[1] for r in rows) / len(rows))

    o_lat, o_lon = centroid(older)
    r_lat, r_lon = centroid(recent)
    dt_s = (window_ms / 2) / 1000.0  # separation between the two half-midpoints
    cos = max(0.01, math.cos(math.radians(r_lat)))
    d_north = (r_lat - o_lat) * KM_PER_DEG
    d_east = (r_lon - o_lon) * KM_PER_DEG * cos
    dist = math.hypot(d_north, d_east)
    speed_kmh = (dist / dt_s) * 3600 if dt_s > 0 else 0.0
    heading = (math.degrees(math.atan2(d_east, d_north)) + 360) % 360 if dist > 1e-6 else None
    proj_scale = project_ms / (window_ms / 2)
    projected = (r_lat + (r_lat - o_lat) * proj_scale, r_lon + (r_lon - o_lon) * proj_scale)
    return {
        "heading": round(heading) if heading is not None else None,
        "compass": COMPASS[round(heading / 45) % 8] if heading is not None else None,
        "speedKmh": round(speed_kmh, 1),
        "from": {"lat": round(o_lat, 4), "lon": round(o_lon, 4)},
        "to": {"lat": round(r_lat, 4), "lon": round(r_lon, 4)},
        "projected": {"lat": round(projected[0], 4), "lon": round(projected[1], 4)},
        "olderSamples": len(older),
        "recentSamples": len(recent),
    }


@api_view(["GET"])
def storm_track(request):
    """
    Storm-cell trajectory & speed for a zone, from the strike store.
    GET /api/weather/storm-track/?minLat=&maxLat=&minLon=&maxLon=&minutes=10
    """
    try:
        min_lat = float(request.GET["minLat"])
        max_lat = float(request.GET["maxLat"])
        min_lon = float(request.GET["minLon"])
        max_lon = float(request.GET["maxLon"])
    except (KeyError, ValueError):
        return Response({"error": "bbox_required"}, status=400)

    minutes = min(max(int(request.GET.get("minutes", STORM_TRACK_WINDOW_MIN)), 2), 30)
    ckey = f"track:{round(min_lat,1)}:{round(max_lat,1)}:{round(min_lon,1)}:{round(max_lon,1)}:{minutes}"
    cached = cache.get(ckey)
    if cached is not None:
        return Response(cached)

    since = timezone.now() - timedelta(minutes=minutes)
    qs = LightningStrike.objects.filter(
        received_at__gte=since,
        lat__gte=min(min_lat, max_lat), lat__lte=max(min_lat, max_lat),
    )
    if min_lon <= max_lon:
        qs = qs.filter(lon__gte=min_lon, lon__lte=max_lon)
    else:
        qs = qs.filter(Q(lon__gte=min_lon) | Q(lon__lte=max_lon))
    rows = qs.order_by("-received_at").values_list("lat", "lon", "received_at")[:5000]
    now_ms = timezone.now().timestamp() * 1000
    points = [(lat, lon, ra.timestamp() * 1000) for (lat, lon, ra) in rows]

    out = {"track": compute_track(points, now_ms, window_ms=minutes * 60_000), "samples": len(points)}
    cache.set(ckey, out, STORM_TRACK_CACHE_SECONDS)
    return Response(out)
