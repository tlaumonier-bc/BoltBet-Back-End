"""
lightning/total_lightning.py — TOTAL lightning (intracloud + cloud-to-ground).

Intracloud (IC) flashes precede cloud-to-ground (CG) strikes by minutes, so a
total-lightning field is a LEADING indicator that complements our in-house
strike-density heatmap (CG-only, and it can only lag reality).

Provider: EUMETSAT MTG Lightning Imager (MTG-LI) Level-2, product
"LI Lightning Flashes" (LI-2-LFL, collection EO:EUM:DAT:0691) — optical total-
lightning flashes with per-flash lat/lon/time, covering the 0° disc
(Europe / Africa / Middle East). Zones outside that disc simply return no flashes.

Two interchangeable providers behind `TotalLightningProvider`:
  • EumetsatMtgLiProvider — the real thing. OAuth (client_credentials) → find the
    latest LFL product(s) → download the zip → parse the netCDF (h5py) → aggregate
    flashes onto the requested zone grid. The recent global flash list is cached so
    the provider only downloads once per TL_REFRESH_SECONDS regardless of traffic.
  • MockProvider — synthetic field derived from our own recent CG strikes, for
    local dev / when no credentials are configured.

Selected by env TOTAL_LIGHTNING_SOURCE ("eumetsat" | "mock"); "eumetsat" needs
EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET (never hardcode — set as env /
secrets) and falls back to the mock if they're missing.

`aggregate_flash_field()` and `parse_lfl_netcdf()` are PURE (stdlib/numpy/h5py, no
Django) so they're unit-testable; `total_lightning()` is the cached DRF view.
"""

import base64
import io
import math
import os
import urllib.parse
import urllib.request
import zipfile
from datetime import timedelta
from typing import Protocol

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import LightningStrike

# ── config (env, sane defaults) ──────────────────────────────────────────────
TOTAL_LIGHTNING_SOURCE = os.environ.get("TOTAL_LIGHTNING_SOURCE", "mock")
TL_CACHE_SECONDS = int(os.environ.get("TOTAL_LIGHTNING_CACHE_SECONDS", "45"))
TL_SAMPLE_N = int(os.environ.get("TOTAL_LIGHTNING_SAMPLE_N", "6"))   # n×n field
TL_OBS_MIN = int(os.environ.get("TOTAL_LIGHTNING_OBS_MIN", "8"))     # lookback window
TL_TREND_MIN = int(os.environ.get("TOTAL_LIGHTNING_TREND_MIN", "3"))  # recent-vs-prev split
TL_IC_CG_RATIO = float(os.environ.get("TOTAL_LIGHTNING_IC_CG_RATIO", "2.5"))  # mock only

# EUMETSAT (real provider). Credentials via env/secrets only.
EUMETSAT_CONSUMER_KEY = os.environ.get("EUMETSAT_CONSUMER_KEY", "")
EUMETSAT_CONSUMER_SECRET = os.environ.get("EUMETSAT_CONSUMER_SECRET", "")
EUMETSAT_TOKEN_URL = os.environ.get("EUMETSAT_TOKEN_URL", "https://api.eumetsat.int/token")
EUMETSAT_SEARCH_URL = os.environ.get("EUMETSAT_SEARCH_URL", "https://api.eumetsat.int/data/search-products/1.0.0/os")
EUMETSAT_DOWNLOAD_BASE = os.environ.get("EUMETSAT_DOWNLOAD_BASE", "https://api.eumetsat.int/data/download/1.0.0/collections")
LFL_COLLECTION = os.environ.get("EUMETSAT_LFL_COLLECTION", "EO:EUM:DAT:0691")
TL_REFRESH_SECONDS = int(os.environ.get("TOTAL_LIGHTNING_REFRESH_SECONDS", "180"))  # global flash cache
TL_PRODUCTS = int(os.environ.get("TOTAL_LIGHTNING_PRODUCTS", "2"))  # latest N LFL files to merge

KM_PER_DEG = 111.32
_Y2K_UNIX = 946_684_800.0  # unix seconds at 2000-01-01T00:00:00Z (flash_time epoch)


class TotalLightningProvider(Protocol):
    def flashes(self, min_lat, max_lat, min_lon, max_lon, n) -> dict:  # pragma: no cover
        ...


def _cos_lat(lat):
    return max(0.01, math.cos(math.radians(lat)))


# ── shared grid aggregation ──────────────────────────────────────────────────
def aggregate_flash_field(points, min_lat, max_lat, min_lon, max_lon, n, now_ms, ratio, source):
    """PURE: bin flash `points` (lat, lon, t_ms) onto an n×n grid over the bbox.

    Per cell centre we sum flashes within ~one cell radius (flashes fan out), scale
    by `ratio` (1.0 for real LFL flashes; >1 for the mock's synthetic IC:CG), and
    compute a `trend` = sign(recent-window count − previous-window count) → the
    intensifying / decaying hint.
    """
    recent_since = now_ms - TL_TREND_MIN * 60_000
    prev_since = now_ms - 2 * TL_TREND_MIN * 60_000
    span_lat = max_lat - min_lat
    span_lon = max_lon - min_lon
    radius_km = max(span_lat, span_lon) * KM_PER_DEG / n

    pts = []
    flash_total = 0.0
    flash_max = 0.0
    up = down = 0
    for r in range(n):
        clat = min_lat + ((r + 0.5) / n) * span_lat
        c = _cos_lat(clat)
        for col in range(n):
            clon = min_lon + ((col + 0.5) / n) * span_lon
            near = recent = prev = 0
            for (plat, plon, t) in points:
                dlat = (plat - clat) * KM_PER_DEG
                dlon = (plon - clon) * KM_PER_DEG * c
                if dlat * dlat + dlon * dlon > radius_km * radius_km:
                    continue
                near += 1
                if t >= recent_since:
                    recent += 1
                elif t >= prev_since:
                    prev += 1
            flashes = round(near * ratio)
            trend = 1 if recent > prev else -1 if recent < prev else 0
            if trend > 0:
                up += 1
            elif trend < 0:
                down += 1
            flash_total += flashes
            flash_max = max(flash_max, flashes)
            pts.append({"lat": round(clat, 4), "lon": round(clon, 4), "flashes": flashes, "trend": trend})

    overall = "intensifying" if up > down else "decaying" if down > up else "steady"
    return {
        "available": True,
        "source": source,
        "sampledAt": timezone.now().isoformat(),
        "points": pts,
        "summary": {"flashMax": flash_max, "flashTotal": round(flash_total), "trend": overall},
    }


# ── mock provider (strike-derived) ───────────────────────────────────────────
class MockProvider:
    def flashes(self, min_lat, max_lat, min_lon, max_lon, n):
        since = timezone.now() - timedelta(minutes=TL_OBS_MIN)
        qs = LightningStrike.objects.filter(
            received_at__gte=since,
            lat__gte=min(min_lat, max_lat), lat__lte=max(min_lat, max_lat),
        )
        if min_lon <= max_lon:
            qs = qs.filter(lon__gte=min_lon, lon__lte=max_lon)
        else:
            qs = qs.filter(Q(lon__gte=min_lon) | Q(lon__lte=max_lon))
        rows = qs.order_by("-received_at").values_list("lat", "lon", "received_at")[:8000]
        pts = [(la, lo, ra.timestamp() * 1000.0) for (la, lo, ra) in rows]
        now_ms = timezone.now().timestamp() * 1000.0
        return aggregate_flash_field(pts, min_lat, max_lat, min_lon, max_lon, n, now_ms, TL_IC_CG_RATIO, "mtg-li-mock")


# ── real EUMETSAT MTG-LI provider ────────────────────────────────────────────
def _scalar_attr(dset, name, default=None):
    import numpy as np
    v = dset.attrs.get(name, default)
    if v is None:
        return default
    return float(np.asarray(v).ravel()[0])


def parse_lfl_netcdf(nc_bytes):
    """PURE: parse an MTG LI-2-LFL netCDF (HDF5) → list of (lat, lon, t_ms).

    latitude/longitude are scaled int16 (degrees); flash_time is seconds since
    2000-01-01 UTC. Fill values are dropped.
    """
    import h5py
    import numpy as np

    with h5py.File(io.BytesIO(nc_bytes), "r") as f:
        lat_d, lon_d, t_d = f["latitude"], f["longitude"], f["flash_time"]
        lat_raw = lat_d[()].astype("f8")
        lon_raw = lon_d[()].astype("f8")
        t = t_d[()].astype("f8")

        lat_fill = _scalar_attr(lat_d, "_FillValue")
        lon_fill = _scalar_attr(lon_d, "_FillValue")
        lat = lat_raw * _scalar_attr(lat_d, "scale_factor", 1.0) + _scalar_attr(lat_d, "add_offset", 0.0)
        lon = lon_raw * _scalar_attr(lon_d, "scale_factor", 1.0) + _scalar_attr(lon_d, "add_offset", 0.0)
        t_ms = (t + _Y2K_UNIX) * 1000.0

        good = np.ones(lat.shape, dtype=bool)
        if lat_fill is not None:
            good &= lat_raw != lat_fill
        if lon_fill is not None:
            good &= lon_raw != lon_fill
        good &= np.isfinite(lat) & np.isfinite(lon) & np.isfinite(t_ms)
        good &= (lat >= -90) & (lat <= 90) & (lon >= -180) & (lon <= 180)

    return list(zip(lat[good].tolist(), lon[good].tolist(), t_ms[good].tolist()))


def _eumetsat_token(timeout=20):
    """Bearer token via client_credentials, cached until shortly before expiry."""
    tok = cache.get("eumetsat:token")
    if tok:
        return tok
    creds = f"{EUMETSAT_CONSUMER_KEY}:{EUMETSAT_CONSUMER_SECRET}".encode()
    req = urllib.request.Request(
        EUMETSAT_TOKEN_URL,
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": "Basic " + base64.b64encode(creds).decode(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    import json
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    tok = data["access_token"]
    cache.set("eumetsat:token", tok, max(60, int(data.get("expires_in", 3400)) - 120))
    return tok


def _fetch_recent_flashes(timeout=30):
    """Latest LFL flashes (whole disc), cached ~TL_REFRESH_SECONDS. list of (lat,lon,t_ms)."""
    cached = cache.get("mtgli:flashes")
    if cached is not None:
        return cached

    import json
    tok = _eumetsat_token()
    hdr = {"Authorization": f"Bearer {tok}"}
    q = urllib.parse.urlencode({"format": "json", "pi": LFL_COLLECTION, "c": TL_PRODUCTS, "sort": "start,time,0"})
    with urllib.request.urlopen(urllib.request.Request(f"{EUMETSAT_SEARCH_URL}?{q}", headers=hdr), timeout=timeout) as r:
        feats = json.loads(r.read().decode()).get("features", [])

    flashes = []
    for feat in feats[:TL_PRODUCTS]:
        pid = urllib.parse.quote(feat["id"], safe="")
        coll = urllib.parse.quote(LFL_COLLECTION, safe="")
        url = f"{EUMETSAT_DOWNLOAD_BASE}/{coll}/products/{pid}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=timeout) as r:
            blob = r.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            body = next((nm for nm in z.namelist() if "CHK-BODY" in nm and nm.endswith(".nc")), None)
            if not body:
                continue
            flashes.extend(parse_lfl_netcdf(z.read(body)))

    cache.set("mtgli:flashes", flashes, TL_REFRESH_SECONDS)
    return flashes


class EumetsatMtgLiProvider:
    """Real MTG-LI over the 0° disc (Europe/Africa/Middle East). Because the game's
    hottest zones are worldwide, zones OUTSIDE the disc get no satellite flashes —
    there we fall back to the strike-derived field so the layer is useful globally.
    The response `source` tells them apart ("mtg-li" vs "mtg-li-mock")."""

    def __init__(self):
        self._fallback = MockProvider()

    def flashes(self, min_lat, max_lat, min_lon, max_lon, n):
        allf = _fetch_recent_flashes()
        lo, hi = min(min_lat, max_lat), max(min_lat, max_lat)
        wrap = min_lon > max_lon  # antimeridian (rare for the MTG disc)
        pts = [
            (la, ln, t) for (la, ln, t) in allf
            if lo <= la <= hi and ((min_lon <= ln <= max_lon) if not wrap else (ln >= min_lon or ln <= max_lon))
        ]
        now_ms = timezone.now().timestamp() * 1000.0
        out = aggregate_flash_field(pts, min_lat, max_lat, min_lon, max_lon, n, now_ms, 1.0, "mtg-li")
        # No satellite flashes here (zone outside the LI disc, or genuinely quiet) →
        # give the strike-derived estimate instead of an empty layer.
        if out["summary"]["flashTotal"] == 0:
            return self._fallback.flashes(min_lat, max_lat, min_lon, max_lon, n)
        return out


def _active_provider() -> TotalLightningProvider:
    if TOTAL_LIGHTNING_SOURCE == "eumetsat" and EUMETSAT_CONSUMER_KEY and EUMETSAT_CONSUMER_SECRET:
        return EumetsatMtgLiProvider()
    return MockProvider()


@api_view(["GET"])
def total_lightning(request):
    """
    Total lightning (IC+CG) flash field over a zone bbox (cached).
    GET /api/lightning/total/?minLat=&maxLat=&minLon=&maxLon=&n=6

    Graceful: 200 {"available": false, "points": []} on any error so the layer
    shows "data unavailable" and the game keeps working.
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
        n = max(3, min(8, int(request.GET.get("n", TL_SAMPLE_N))))
    except ValueError:
        n = TL_SAMPLE_N

    ckey = f"totallx:{round(min_lat, 1)}:{round(max_lat, 1)}:{round(min_lon, 1)}:{round(max_lon, 1)}:{n}"
    cached = cache.get(ckey)
    if cached is not None:
        return Response(cached)

    try:
        out = _active_provider().flashes(min_lat, max_lat, min_lon, max_lon, n)
    except Exception:
        out = {"available": False, "points": []}
        cache.set(ckey, out, min(TL_CACHE_SECONDS, 15))
        return Response(out)

    cache.set(ckey, out, TL_CACHE_SECONDS)
    return Response(out)
