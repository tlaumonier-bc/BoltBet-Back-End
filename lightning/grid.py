"""
lightning/grid.py — globe zoning.

Two INDEPENDENT grids:
  * ZONE grid    (ZONE_SIZE_DEG, default 10°)  -> the clickable game zones.
                 10° over the full globe = (360/10) * (180/10) = 36 * 18 = 648.
  * ROLLUP grid  (ROLLUP_SIZE_DEG, default 5°) -> only for dashboard rollups.

Why two grids: the rollup history must NOT be tied to how many zones the game
currently has, so changing ZONE_SIZE_DEG (e.g. 648 -> more zones) never
invalidates stored aggregates. Strikes are stored with raw lat/lon only; zone
membership is always derived from geometry, so re-zoning is a config change, not
a data migration.

Zone ids are index-based ("z_<col>_<row>") so they survive non-integer cell
sizes and resolution changes. zone_bounds() is the source of truth used for
scoring: a strike scores for a pick iff its lat/lon falls inside the pick's box.
"""

import math
import os

ZONE_SIZE_DEG = float(os.environ.get("ZONE_SIZE_DEG", "10"))
ROLLUP_SIZE_DEG = float(os.environ.get("ROLLUP_SIZE_DEG", "5"))


def _norm_lon(lon: float) -> float:
    """Normalize longitude to [-180, 180)."""
    return ((lon + 180.0) % 360.0) - 180.0


def _clamp_lat(lat: float) -> float:
    # 89.999999 keeps the north pole inside the last row instead of overflowing.
    return max(-90.0, min(89.999999, lat))


def _cols_rows(size: float):
    return int(round(360.0 / size)), int(round(180.0 / size))


def _cell(lat: float, lon: float, size: float, prefix: str):
    lat = _clamp_lat(lat)
    lon = _norm_lon(lon)
    cols, rows = _cols_rows(size)
    col = min(cols - 1, int((lon + 180.0) // size))
    row = min(rows - 1, int((lat + 90.0) // size))
    return f"{prefix}_{col}_{row}", col, row


def rollup_cell_for(lat: float, lon: float):
    """Strike -> fine rollup cell id at ROLLUP_SIZE_DEG. Returns (cell_id, col, row)."""
    return _cell(lat, lon, ROLLUP_SIZE_DEG, "r")