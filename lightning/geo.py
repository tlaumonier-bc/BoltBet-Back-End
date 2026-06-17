"""lat/lon -> ISO-3166 alpha-2 country code, offline."""
import reverse_geocoder as _rg


def country_for_batch(coords):
    """coords: list of (lat, lon). Returns list of alpha-2 codes, same order."""
    if not coords:
        return []
    results = _rg.search(coords, mode=1)  # mode=1 = single-threaded, safe in the worker
    return [(r.get("cc") or "XX") for r in results]