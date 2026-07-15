"""lat/lon -> ISO-3166 alpha-2 country code, offline."""
import reverse_geocoder as _rg


def country_for_batch(coords):
    """coords: list of (lat, lon). Returns list of alpha-2 codes, same order."""
    if not coords:
        return []
    results = _rg.search(coords, mode=1)  # mode=1 = single-threaded, safe in the worker
    return [(r.get("cc") or "XX") for r in results]


def places_for_points(coords):
    """coords: list of (lat, lon). Returns the nearest populated place for each,
    as {name, admin1, cc, lat, lon} (the place's own coordinates)."""
    if not coords:
        return []
    out = []
    for r in _rg.search(coords, mode=1):
        try:
            out.append({
                "name": r.get("name") or "",
                "admin1": r.get("admin1") or "",
                "cc": r.get("cc") or "XX",
                "lat": float(r.get("lat")),
                "lon": float(r.get("lon")),
            })
        except (TypeError, ValueError):
            continue
    return out