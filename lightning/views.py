from datetime import timedelta
from django.utils import timezone
from django.db import connection
from django.db.models import Sum
from rest_framework.decorators import api_view
from rest_framework import viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import StrikeRollupMinute, LightningStrike, CountryStrike
# from .serializers import GridCellSerializer, LightningStrikeSerializer


@api_view(["GET"])
def recent_strikes(request):
    """
    Raw strike positions, newest first.
      ?minutes=60       older bound: strikes received in the last N minutes
      ?older_than=0     younger bound: exclude strikes newer than this many minutes.
                        Combine the two to get a NON-overlapping age band, e.g.
                        minutes=360&older_than=60  -> strikes 1–6 hours old.
      ?limit=5000       hard ceiling 10000
    """
    minutes = int(request.GET.get("minutes", 10))
    older_than = int(request.GET.get("older_than", 0))
    limit = min(int(request.GET.get("limit", 5000)), 10000)
    now = timezone.now()
    since = now - timedelta(minutes=minutes)

    qs = LightningStrike.objects.filter(received_at__gte=since)
    if older_than > 0:
        qs = qs.filter(received_at__lt=now - timedelta(minutes=older_than))
    qs = (qs.order_by("-received_at")
            .values("lat", "lon", "quality", "timestamp", "received_at")[:limit])

    return Response({
        "minutes": minutes,
        "older_than": older_than,
        "count": len(qs),          # number actually returned (may hit the cap)
        "strikes": list(qs),
    })


@api_view(["GET"])
def strikes_per_minute(request):
    """
    Global strike count grouped by minute over the last 15 minutes.
    Reads the per-minute rollups, so it never scans raw strikes.
    Returns one entry per minute bucket, oldest first, zero-filled.
    """
    minutes = int(request.GET.get("minutes", 15))
    now = timezone.now()
    current_min = now.replace(second=0, microsecond=0)
    since = current_min - timedelta(minutes=minutes - 1)

    rows = (StrikeRollupMinute.objects
            .filter(bucket__gte=since)
            .values("bucket")
            .annotate(n=Sum("count")))
    by_bucket = {r["bucket"].replace(second=0, microsecond=0): r["n"] for r in rows}

    series = []
    for i in range(minutes):
        b = since + timedelta(minutes=i)
        series.append({"minute": b.isoformat(), "count": by_bucket.get(b, 0)})

    return Response({"minutes": minutes, "series": series})


@api_view(["GET"])
def country_strikes(request):
    """
    Newest N (<=1000) strikes per country, regardless of age.
      ?country=FR   -> just that country (recommended)
      no param      -> ALL countries (large payload — see note)
      ?limit=1000
    """
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