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
