import math
import os

LAT_LO, LAT_HI = -90, 70
LON_LO, LON_HI = -180, 160

BASE_RATE = float(os.environ.get("MULTIPLIER_BASE_RATE", "6"))
MIN_MULT, MAX_MULT = 1.1, 9.0


def grid_for(lat, lon):
    """Map a strike to the same 20x20 cells the frontend builds."""
    lat_min = int(math.floor((lat + 90) / 20) * 20 - 90)
    lon_min = int(math.floor((lon + 180) / 20) * 20 - 180)
    lat_min = max(LAT_LO, min(LAT_HI, lat_min))
    lon_min = max(LON_LO, min(LON_HI, lon_min))
    return f"lon_{lon_min}_lat_{lat_min}", lon_min, lat_min


def multiplier_for(count_1h):
    # Mirrors the model hint: base_rate / (count_1h + 0.5), clamped.
    return round(max(MIN_MULT, min(MAX_MULT, BASE_RATE / (count_1h + 0.5))), 1)


def cell_payload(cell):
    """Shape a GridCell for the frontend's updateCells()."""
    return {
        "id": cell.cell_id,
        "lonMin": cell.lon_min,
        "latMin": cell.lat_min,
        "multiplier": cell.multiplier,
        "strikeCount24h": cell.strike_count_24h,
        "isHot": cell.multiplier < 1.5,
    }