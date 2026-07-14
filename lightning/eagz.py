"""
lightning/eagz.py — EAGZ-1 (Entropy-Adaptive Grid Zoning, v1).

Spec: docs/grid-models/EAGZ-1.md. This module is the PURE algorithm — no Django
or DB imports — so it can be unit-tested and A/B compared in isolation. The
caller (account.services) fetches strikes over W_obs, maps DB rows to `Strike`
tuples, runs the pipeline, and persists the chosen zone's geometry on the match.

A `Strike` is (lat, lon, ts) where ts is epoch seconds.

Pipeline (see spec §2):
  Stage 1  coarse binning over W_obs  -> active regions (connected components)
  Stage 2  adaptive cell sizing scaled to W_round + weighted centroid -> grid bbox
  Stage 3  temporally-weighted Shannon entropy over W_obs -> accept / reject

Design choices for this codebase (see spec §3.1 and implementation notes):
  * Stage 1 uses an EQUIVALENT FIXED GRID (`coarse_deg`) instead of a geohash
    library — the spec explicitly permits this and adjacency-merge becomes a
    trivial 8-neighbour flood fill.
  * The output grid is defined EQUIRECTANGULARLY (linear lat/lon within the zone
    bbox). This is the canonical mapping the server scores on; the client must
    hit-test with the same mapping so the displayed score equals the
    authoritative score.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import NamedTuple

MODEL_NAME = "EAGZ-1.1"
KM_PER_DEG = 111.32


class Strike(NamedTuple):
    lat: float
    lon: float
    ts: float  # epoch seconds


@dataclass(frozen=True)
class Eagz1Config:
    """EAGZ-1 baseline parameters (spec §7). Snapshotted onto every zone."""

    model: str = MODEL_NAME
    w_round_seconds: float = 60.0
    w_obs_seconds: float = 600.0          # 10 min
    coarse_deg: float = 0.3               # ~33 km fixed grid (~geohash p4-5)
    min_region_activity: int = 5          # coarse-cell strike floor (Stage 1)
    sigma_k: float = 2.0                  # grid half-extent = k * weighted std-dev of strikes
    lambda_target: float = 0.7            # (legacy density target; unused since sizing is extent-based)
    cell_size_min_km: float = 0.2         # 200 m
    cell_size_max_km: float = 10.0
    min_total_strikes: float = 8.0        # weighted activity floor over W_obs (Stage 3)
    min_round_strikes: int = 10           # raw strikes IN THE GRID over the last W_round
    entropy_threshold: float = 0.5        # H_norm floor (Stage 3)
    tau_seconds: float = 180.0            # recency decay (3 min)
    grid_cols: int = 10
    grid_rows: int = 8

    def snapshot(self) -> dict:
        return asdict(self)


def _cos_lat(lat: float) -> float:
    return max(0.01, math.cos(math.radians(lat)))


def _weight(ts: float, now: float, tau: float) -> float:
    """Temporal recency weight exp(-Δt/τ) (spec §5.3)."""
    return math.exp(-max(0.0, now - ts) / tau)


def _coarse_key(lat: float, lon: float, deg: float):
    return (int(math.floor((lon + 180.0) / deg)), int(math.floor((lat + 90.0) / deg)))


def _regions(strikes: list[Strike], cfg: Eagz1Config):
    """Stage 1: bin, apply activity floor, merge adjacent active buckets.

    Yields (bucket_count, region_strikes) per connected component.
    """
    buckets: dict[tuple[int, int], list[Strike]] = {}
    for s in strikes:
        buckets.setdefault(_coarse_key(s.lat, s.lon, cfg.coarse_deg), []).append(s)
    active = {k: v for k, v in buckets.items() if len(v) >= cfg.min_region_activity}

    seen: set[tuple[int, int]] = set()
    for start in active:
        if start in seen:
            continue
        comp = []
        stack = [start]
        seen.add(start)
        while stack:
            ck = stack.pop()
            comp.append(ck)
            for dc in (-1, 0, 1):
                for dr in (-1, 0, 1):
                    nk = (ck[0] + dc, ck[1] + dr)
                    if nk in active and nk not in seen:
                        seen.add(nk)
                        stack.append(nk)
        yield [s for ck in comp for s in active[ck]]


def _weighted_centroid(strikes: list[Strike], now: float, tau: float):
    sw = sx = sy = 0.0
    for s in strikes:
        w = _weight(s.ts, now, tau)
        sw += w
        sx += w * s.lon
        sy += w * s.lat
    if sw <= 0:
        return None
    return sy / sw, sx / sw  # (lat, lon)


def _weighted_spread_km(strikes, center_lat, center_lon, now, tau):
    """Temporally-weighted std-dev of strike positions around the centroid, in km."""
    sw = svlat = svlon = 0.0
    for s in strikes:
        w = _weight(s.ts, now, tau)
        sw += w
        svlat += w * (s.lat - center_lat) ** 2
        svlon += w * (s.lon - center_lon) ** 2
    if sw <= 0:
        return 0.0, 0.0
    sig_lat_km = math.sqrt(svlat / sw) * KM_PER_DEG
    sig_lon_km = math.sqrt(svlon / sw) * KM_PER_DEG * _cos_lat(center_lat)
    return sig_lat_km, sig_lon_km


def _normalized_entropy(counts: list[float], total: float, n_cells: int) -> float:
    if total <= 0:
        return 0.0
    h = 0.0
    for n in counts:
        if n > 0:
            p = n / total
            h -= p * math.log(p)
    return h / math.log(n_cells)


def _size_and_validate(region_strikes: list[Strike], cfg: Eagz1Config, now: float):
    """Stages 2 + 3 for one region. Returns a zone dict or None."""
    if not region_strikes:
        return None

    centroid = _weighted_centroid(region_strikes, now, cfg.tau_seconds)
    if centroid is None:
        return None
    center_lat, center_lon = centroid

    # Stage 2 — frame the grid to the recent strike cluster's spatial spread so
    # strikes populate MANY cells instead of concentrating in a few. Each axis is
    # sized to +/- k*sigma; the per-axis cell size is clamped to [min, max].
    sig_lat_km, sig_lon_km = _weighted_spread_km(region_strikes, center_lat, center_lon, now, cfg.tau_seconds)
    cell_h_km = min(cfg.cell_size_max_km, max(cfg.cell_size_min_km, 2.0 * cfg.sigma_k * sig_lat_km / cfg.grid_rows))
    cell_w_km = min(cfg.cell_size_max_km, max(cfg.cell_size_min_km, 2.0 * cfg.sigma_k * sig_lon_km / cfg.grid_cols))
    cell_km = (cell_h_km + cell_w_km) / 2.0

    # Grid bbox (equirectangular), centered on the weighted centroid.
    half_h_deg = (cfg.grid_rows * cell_h_km / 2.0) / KM_PER_DEG
    half_w_deg = (cfg.grid_cols * cell_w_km / 2.0) / (KM_PER_DEG * _cos_lat(center_lat))
    min_lat, max_lat = center_lat - half_h_deg, center_lat + half_h_deg
    min_lon, max_lon = center_lon - half_w_deg, center_lon + half_w_deg
    span_lat = max_lat - min_lat
    span_lon = max_lon - min_lon
    if span_lat <= 0 or span_lon <= 0:
        return None

    # Stage 3 — temporally-weighted entropy over W_obs on this grid, plus a raw
    # per-grid activity gate over the last W_round. The round count is geographic
    # (strike falls inside the grid bbox), so a grid straddling a border counts
    # strikes from both countries — the whole reason we gate per grid, not per
    # country.
    cols, rows = cfg.grid_cols, cfg.grid_rows
    counts = [0.0] * (cols * rows)
    round_cutoff = now - cfg.w_round_seconds
    round_strikes = 0
    for s in region_strikes:
        rx = (s.lon - min_lon) / span_lon
        ry = (max_lat - s.lat) / span_lat  # row 0 = north (top), matches client
        if not (0.0 <= rx < 1.0 and 0.0 <= ry < 1.0):
            continue
        col = min(cols - 1, int(rx * cols))
        row = min(rows - 1, int(ry * rows))
        counts[row * cols + col] += _weight(s.ts, now, cfg.tau_seconds)
        if s.ts >= round_cutoff:
            round_strikes += 1

    if round_strikes < cfg.min_round_strikes:
        return None
    total = sum(counts)
    if total < cfg.min_total_strikes:
        return None
    h_norm = _normalized_entropy(counts, total, cols * rows)
    if h_norm < cfg.entropy_threshold:
        return None

    return {
        "min_lat": min_lat,
        "max_lat": max_lat,
        "min_lon": min_lon,
        "max_lon": max_lon,
        "center_lat": center_lat,
        "center_lon": center_lon,
        "cell_size_km": cell_km,
        "cols": cols,
        "rows": rows,
        "h_norm": h_norm,
        "total_strikes_obs": total,
        "round_strikes": round_strikes,
        "model": cfg.model,
        "params": cfg.snapshot(),
    }


def run_eagz1(strikes: list[Strike], cfg: Eagz1Config, now: float) -> list[dict]:
    """Run the full EAGZ-1 pipeline and return all accepted playable zones."""
    zones = []
    for region_strikes in _regions(strikes, cfg):
        zone = _size_and_validate(region_strikes, cfg, now)
        if zone is not None:
            zones.append(zone)
    return zones


def best_zone(strikes: list[Strike], cfg: Eagz1Config, now: float):
    """Pick the single most dispersed, most-active playable zone, or None."""
    zones = run_eagz1(strikes, cfg, now)
    if not zones:
        return None
    return max(zones, key=lambda z: (z["h_norm"], z["total_strikes_obs"]))


def cell_for_point(lat: float, lon: float, zone: dict) -> int | None:
    """Equirectangular hit-test: map a strike to a zone cell index, or None if
    outside. MUST match the client's rendering so displayed == authoritative."""
    span_lon = zone["max_lon"] - zone["min_lon"]
    span_lat = zone["max_lat"] - zone["min_lat"]
    if span_lon <= 0 or span_lat <= 0:
        return None
    rx = (lon - zone["min_lon"]) / span_lon
    ry = (zone["max_lat"] - lat) / span_lat
    if not (0.0 <= rx < 1.0 and 0.0 <= ry < 1.0):
        return None
    cols, rows = zone["cols"], zone["rows"]
    col = min(cols - 1, int(rx * cols))
    row = min(rows - 1, int(ry * rows))
    return row * cols + col
