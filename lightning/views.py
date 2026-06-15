from datetime import timedelta
from django.utils import timezone
from django.db.models import Sum
from rest_framework.decorators import api_view
from rest_framework import viewsets
from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import StrikeRollupMinute
# from .serializers import GridCellSerializer, LightningStrikeSerializer


@api_view(["GET"])
def strike_stats(request):
    """Global strike counts from the minute rollups (cheap; no raw scan)."""
    now = timezone.now()
    def total_since(minutes):
        since = now - timedelta(minutes=minutes)
        agg = StrikeRollupMinute.objects.filter(bucket__gte=since).aggregate(n=Sum("count"))
        return agg["n"] or 0
    lat = StrikeRollupMinute.objects.filter(
        bucket__gte=now - timedelta(minutes=60)
    ).aggregate(s=Sum("latency_sum_ms"), n=Sum("latency_n"))
    avg_latency_ms = round(lat["s"] / lat["n"]) if lat["n"] else None
    return Response({
        "last_15_min": total_since(15),
        "last_60_min": total_since(60),
        "last_24h": total_since(60 * 24),
        "avg_latency_ms_60min": avg_latency_ms,
        "server_time": now.isoformat(),
    })