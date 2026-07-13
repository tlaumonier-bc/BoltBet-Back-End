"""City-level lightning statistics for the world's top populated places."""

import datetime as dt
import json
import math
from functools import lru_cache
from pathlib import Path


CITY_RADIUS_KM = 20.0
_EARTH_RADIUS_KM = 6371.0088
_DATA_PATH = Path(__file__).resolve().parent / "data" / "top_cities_by_country.json"

CITY_AGG_UPSERT = """
INSERT INTO lightning_citystrikeaggregate
    (city_id, city_name, country, lat, lon, population, period_kind, period_start,
     count, good, medium, bad)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (city_id, period_kind, period_start) DO UPDATE SET
    city_name  = excluded.city_name,
    country    = excluded.country,
    lat        = excluded.lat,
    lon        = excluded.lon,
    population = excluded.population,
    count      = lightning_citystrikeaggregate.count  + excluded.count,
    good       = lightning_citystrikeaggregate.good   + excluded.good,
    medium     = lightning_citystrikeaggregate.medium + excluded.medium,
    bad        = lightning_citystrikeaggregate.bad    + excluded.bad;
"""


@lru_cache(maxsize=1)
def top_cities_by_country():
    return json.loads(_DATA_PATH.read_text())


def haversine_km(lat1, lon1, lat2, lon2):
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def city_for_strike(lat, lon, country):
    """Return the nearest top city within 20 km for a strike, or None."""
    cities = top_cities_by_country().get((country or "").upper(), [])
    if not cities:
        return None

    best = None
    best_distance = CITY_RADIUS_KM
    for city in cities:
        distance = haversine_km(lat, lon, city["lat"], city["lon"])
        if distance <= best_distance:
            best = city
            best_distance = distance
    return best


def period_starts(timestamp):
    day = timestamp.date()
    month = dt.date(day.year, day.month, 1)
    year = dt.date(day.year, 1, 1)
    return (("day", day), ("month", month), ("year", year))


def city_aggregate_rows(strikes):
    """
    Build aggregate rows keyed by (city_id, period_kind, period_start).

    `strikes` items must provide lat, lon, country, timestamp, and quality.
    """
    aggregates = {}
    for strike in strikes:
        city = city_for_strike(strike["lat"], strike["lon"], strike.get("country"))
        if city is None:
            continue

        for period_kind, period_start in period_starts(strike["timestamp"]):
            key = (city["id"], period_kind, period_start)
            row = aggregates.setdefault(key, {
                "city_id": city["id"],
                "city_name": city["name"],
                "country": city["country"],
                "lat": city["lat"],
                "lon": city["lon"],
                "population": city["population"],
                "period_kind": period_kind,
                "period_start": period_start,
                "count": 0,
                "good": 0,
                "medium": 0,
                "bad": 0,
            })
            row["count"] += 1
            quality = strike.get("quality")
            if quality in ("good", "medium", "bad"):
                row[quality] += 1
    return list(aggregates.values())


def upsert_city_aggregate_rows(connection, rows):
    if not rows:
        return
    with connection.cursor() as cur:
        for row in rows:
            cur.execute(CITY_AGG_UPSERT, [
                row["city_id"], row["city_name"], row["country"],
                row["lat"], row["lon"], row["population"],
                row["period_kind"], row["period_start"],
                row["count"], row["good"], row["medium"], row["bad"],
            ])
