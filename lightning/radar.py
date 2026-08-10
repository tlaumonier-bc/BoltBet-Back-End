"""
lightning/radar.py — radar reflectivity (dBZ) frame index for the Grid Game.

Powers the "Radar" map layer: raw reflectivity cores, the strongest short-term
predictor of where the next strikes land (distinct from the Rain layer, which is
precipitation/rain rate).

The browser fetches the *tile images* directly from the radar host (that's fine —
they're plain image tiles), but the FRAME INDEX (which frames exist right now,
their timestamps and tile paths) is fetched here, server-side, and cached, so the
provider's index endpoint is hit at most once per RADAR_CACHE_SECONDS regardless
of how many players are on the map.

Provider is behind the `RadarProvider` interface so it's swappable. The default is
RainViewer's free public tier.

  ⚠ LICENSING: RainViewer's free tier is for PERSONAL / EDUCATIONAL use only. For
  commercial production, swap the provider for EUMETNET OPERA composites (European
  radar), a national service (e.g. NOAA MRMS for the US), or a paid RainViewer /
  commercial plan. Because access goes through `RadarProvider`, that's a one-class
  change here — no frontend or view changes required.

`rainviewer_index()` is a PURE function (urllib + stdlib only, no Django) so it can
be unit-tested in isolation; `radar_frames()` is the cached DRF view.

Docs: docs/layers/PLAN.md
"""

import json
import os
import urllib.request
from typing import Protocol

from django.core.cache import cache
from rest_framework.decorators import api_view
from rest_framework.response import Response

# ── config (env, sane defaults) ──────────────────────────────────────────────
RADAR_CACHE_SECONDS = int(os.environ.get("RADAR_CACHE_SECONDS", "60"))  # index refresh
RADAR_TILE_SIZE = int(os.environ.get("RADAR_TILE_SIZE", "256"))          # 256 or 512
# RainViewer colour scheme: 4 = "The Weather Channel" (dBZ-oriented). See
# https://www.rainviewer.com/api/color-schemes.html
RADAR_COLOR = int(os.environ.get("RADAR_COLOR", "4"))
# tile options "{smooth}_{snow}". Smoothing OFF (0) keeps reflectivity cores sharp
# and readable; snow-tinting OFF (0).
RADAR_OPTIONS = os.environ.get("RADAR_OPTIONS", "0_0")
# How many trailing frames to expose (for the short animation loop). Past frames
# are ~10 min apart on the free tier; 4 ≈ the last ~30 min.
RADAR_FRAMES = int(os.environ.get("RADAR_FRAMES", "4"))
RAINVIEWER_INDEX_URL = os.environ.get(
    "RAINVIEWER_INDEX_URL", "https://api.rainviewer.com/public/weather-maps.json"
)


class RadarProvider(Protocol):
    """A radar tile source. Return value is JSON-serialisable and shaped exactly
    like the frontend expects (see `_normalise`)."""

    def index(self, timeout: int = 8) -> dict:  # pragma: no cover - interface
        ...


def _normalise(host: str, frames: list[dict]) -> dict:
    """Shared response shape for any provider."""
    return {
        "available": True,
        "host": host,
        "size": RADAR_TILE_SIZE,
        "color": RADAR_COLOR,
        "options": RADAR_OPTIONS,
        # each frame: {time: epoch_seconds, path: "/v2/radar/...", kind: past|nowcast}
        "frames": frames,
    }


def rainviewer_index(timeout: int = 8) -> dict:
    """Fetch + normalise RainViewer's frame index (PURE — no Django).

    Returns the trailing `RADAR_FRAMES` past frames plus any nowcast frames, each
    as {time, path, kind}. The browser builds tile URLs as
    `{host}{path}/{size}/{z}/{x}/{y}/{color}/{options}.png`.
    """
    req = urllib.request.Request(
        RAINVIEWER_INDEX_URL, headers={"User-Agent": "lightning-map-game/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))

    host = data.get("host") or "https://tilecache.rainviewer.com"
    radar = data.get("radar") or {}
    past = radar.get("past") or []
    nowcast = radar.get("nowcast") or []

    frames = [
        {"time": f["time"], "path": f["path"], "kind": "past"}
        for f in past[-RADAR_FRAMES:]
        if f.get("path")
    ]
    frames += [
        {"time": f["time"], "path": f["path"], "kind": "nowcast"}
        for f in nowcast
        if f.get("path")
    ]
    return _normalise(host, frames)


class RainViewerProvider:
    """Default free-tier provider (personal/educational use — see module docstring)."""

    def index(self, timeout: int = 8) -> dict:
        return rainviewer_index(timeout=timeout)


# The active provider. Swap this line to change radar sources (see LICENSING note).
_PROVIDER: RadarProvider = RainViewerProvider()


@api_view(["GET"])
def radar_frames(request):
    """
    Latest radar reflectivity frame index (cached ~60s), provider-agnostic.
    GET /api/weather/radar/

    On provider downtime returns 200 with {"available": false, "frames": []} so the
    layer can show a graceful "data unavailable" state and the game keeps working.
    """
    ckey = "radar:index"
    cached = cache.get(ckey)
    if cached is not None:
        return Response(cached)

    try:
        out = _PROVIDER.index()
    except Exception:
        # Don't cache failures for the full TTL — retry soon, but keep responding.
        out = {"available": False, "frames": []}
        cache.set(ckey, out, min(RADAR_CACHE_SECONDS, 15))
        return Response(out)

    cache.set(ckey, out, RADAR_CACHE_SECONDS)
    return Response(out)
