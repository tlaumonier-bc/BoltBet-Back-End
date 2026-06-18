from datetime import timedelta
import os
import urllib.request
import urllib.parse
import json
import datetime as dt
from django.utils import timezone
from django.db import connection
from django.db.models import Sum
from django.core.cache import cache
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import StrikeRollupMinute, LightningStrike, CountryStrike


@api_view(["GET"])
def recent_strikes(request):
    """
    ?minutes=60 ?older_than=0 ?limit=5000
    ?after=<iso>     only strikes received strictly after this (delta polling)
    ?downsample=N    return every Nth row (old bands don't need full density)
    """
    minutes = int(request.GET.get("minutes", 10))
    older_than = int(request.GET.get("older_than", 0))
    limit = min(int(request.GET.get("limit", 5000)), 200000)  # raised cap
    downsample = max(1, int(request.GET.get("downsample", 1)))
    after = request.GET.get("after")
    now = timezone.now()
    since = now - timedelta(minutes=minutes)

    qs = LightningStrike.objects.filter(received_at__gte=since)
    if older_than > 0:
        qs = qs.filter(received_at__lt=now - timedelta(minutes=older_than))
    if after:
        try:
            qs = qs.filter(received_at__gt=dt.datetime.fromisoformat(after))
        except ValueError:
            pass
    qs = (qs.order_by("-received_at")
            .values("lat", "lon", "quality", "timestamp", "received_at")[:limit])

    rows = list(qs)
    if downsample > 1:
        rows = rows[::downsample]

    return Response({
        "minutes": minutes,
        "older_than": older_than,
        "count": len(rows),
        "strikes": rows,
    })


@api_view(["GET"])
def strikes_per_minute(request):
    minutes = int(request.GET.get("minutes", 15))
    now = timezone.now()
    current_min = now.replace(second=0, microsecond=0)
    since = current_min - timedelta(minutes=minutes - 1)

    rows = (StrikeRollupMinute.objects
            .filter(bucket__gte=since)
            .values("bucket")
            .annotate(n=Sum("count")))

    def k(d):
        if timezone.is_naive(d):
            d = timezone.make_aware(d, dt.timezone.utc)
        return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")

    by_bucket = {k(r["bucket"]): r["n"] for r in rows}

    series = []
    for i in range(minutes):
        b = since + timedelta(minutes=i)
        series.append({"minute": b.isoformat(), "count": by_bucket.get(k(b), 0)})

    return Response({"minutes": minutes, "series": series})


@api_view(["GET"])
def country_strikes(request):
    limit = min(int(request.GET.get("limit", 1000)), 1000)
    country = request.GET.get("country")

    if country:
        rows = (CountryStrike.objects
                .filter(country=country.upper())
                .order_by("-received_at")
                .values("lat", "lon", "timestamp", "quality", "received_at")[:limit])
        return Response({country.upper(): list(rows)})

    sql = """
        SELECT country, lat, lon, timestamp, quality, received_at FROM (
            SELECT country, lat, lon, timestamp, quality, received_at,
                   ROW_NUMBER() OVER (PARTITION BY country ORDER BY received_at DESC) AS rn
            FROM lightning_countrystrike
        ) t
        WHERE rn <= %s
        ORDER BY country, received_at DESC
    """
    out = {}
    with connection.cursor() as cur:
        cur.execute(sql, [limit])
        for cc, lat, lon, ts, quality, recv in cur.fetchall():
            out.setdefault(cc, []).append(
                {"lat": lat, "lon": lon, "timestamp": ts,
                 "quality": quality, "received_at": recv}
            )
    return Response(out)


OWM_KEY = os.environ.get("OWM_API_KEY")  # server-only, NO NEXT_PUBLIC_


@api_view(["GET"])
def weather_now(request):
    if not OWM_KEY:
        return Response({"error": "owm_key_unset"}, status=503)
    try:
        lat = float(request.GET["lat"])
        lon = float(request.GET["lon"])
    except (KeyError, ValueError):
        return Response({"error": "lat_lon_required"}, status=400)

    ckey = f"owm:{round(lat, 2)}:{round(lon, 2)}"
    cached = cache.get(ckey)
    if cached:
        return Response(cached)

    q = urllib.parse.urlencode({"lat": lat, "lon": lon, "units": "metric", "appid": OWM_KEY})
    url = f"https://api.openweathermap.org/data/2.5/weather?{q}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            d = json.loads(r.read())
    except Exception:
        return Response({"error": "owm_fetch_failed"}, status=502)

    out = {
        "tempC": round(d.get("main", {}).get("temp", 0)),
        "clouds": round(d.get("clouds", {}).get("all", 0)),
        "windKph": round(d.get("wind", {}).get("speed", 0) * 3.6),
        "humidity": round(d.get("main", {}).get("humidity", 0)),
        "main": (d.get("weather") or [{}])[0].get("main", "—"),
        "icon": (d.get("weather") or [{}])[0].get("icon", ""),
        "country": d.get("sys", {}).get("country"),
    }
    cache.set(ckey, out, 300)  # 5 min
    return Response(out)