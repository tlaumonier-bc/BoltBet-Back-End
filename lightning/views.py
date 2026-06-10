from datetime import timedelta
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework import viewsets
from rest_framework.response import Response
from .models import GridCell, LightningStrike
from .serializers import GridCellSerializer, LightningStrikeSerializer

class GridCellViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only endpoint for the frontend to fetch current grid states and multipliers.
    """
    queryset = GridCell.objects.all()
    serializer_class = GridCellSerializer

class LightningStrikeViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only endpoint to fetch recent historical strikes.
    (Live strikes will be handled by WebSockets, but this is good for initial load).
    """
    queryset = LightningStrike.objects.order_by('-timestamp')[:200] # Limit to last 200
    serializer_class = LightningStrikeSerializer


@api_view(["GET"])
def strike_count(request):
    """Count strikes in a lat/lon box over the last `minutes` (default 60)."""
    try:
        min_lat = float(request.GET["min_lat"])
        max_lat = float(request.GET["max_lat"])
        min_lon = float(request.GET["min_lon"])
        max_lon = float(request.GET["max_lon"])
    except (KeyError, ValueError):
        return Response(
            {"error": "min_lat, max_lat, min_lon, max_lon are required floats"},
            status=400,
        )

    try:
        minutes = max(1, min(int(request.GET.get("minutes", 60)), 1440))
    except ValueError:
        minutes = 60

    since = timezone.now() - timedelta(minutes=minutes)
    count = LightningStrike.objects.filter(
        timestamp__gte=since,
        lat__gte=min_lat, lat__lte=max_lat,
        lon__gte=min_lon, lon__lte=max_lon,
    ).count()
    return Response({"count": count, "minutes": minutes})