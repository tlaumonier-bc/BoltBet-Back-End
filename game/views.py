from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import services
from .models import GameRound, PlayerStats

User = get_user_model()


def _player(request, username=None):
    """
    Temporary MVP identity: authenticated user if present, else a user keyed by
    the supplied `username` (so distinct guests get distinct leaderboard rows),
    else the shared 'anonymous' fallback. Replace with real auth later.
    """
    if request.user.is_authenticated:
        return request.user
    name = (username or "").strip()[:32] or "anonymous"
    user, _ = User.objects.get_or_create(username=name, defaults={"is_active": True})
    return user


@api_view(["GET"])
def game_state(request):
    rnd = services.get_active_round()
    now = timezone.now()
    if rnd:
        return Response({
            "active": True,
            "round_number": rnd.number,
            "ends_at": rnd.ends_at.isoformat(),
            "server_time": now.isoformat(),       # client computes remaining = ends_at - server_time
            "duration_seconds": rnd.duration_seconds,
            "lock_seconds": services.LOCK_SECONDS,
        })

    # No active round — are we in the intermission buffer between games?
    last = GameRound.objects.order_by("-number").first()
    if last and last.status == "finished":
        next_at = last.ends_at + timedelta(seconds=services.INTERMISSION_SECONDS)
        if now < next_at:
            return Response({
                "active": False,
                "intermission": True,
                "next_round_at": next_at.isoformat(),
                "server_time": now.isoformat(),
            })
    return Response({"active": False, "server_time": now.isoformat()})


@api_view(["POST"])
def place_pick(request):
    zone_id = request.data.get("zone_id")
    if not zone_id:
        return Response({"error": "zone_id required"}, status=400)

    user = _player(request, request.data.get("username"))
    country = request.data.get("country")
    if country:
        PlayerStats.objects.update_or_create(user=user, defaults={"country": str(country)[:2].upper()})

    try:
        pick = services.place_pick(user, zone_id)
    except services.PickError as exc:
        status = 409 if str(exc) == "already_locked" else 400
        return Response({"error": str(exc)}, status=status)

    return Response({
        "id": pick.id,
        "zone_id": pick.zone_id,
        "locked_at": pick.locked_at.isoformat(),
        "expires_at": pick.expires_at.isoformat(),
    }, status=201)


@api_view(["GET"])
def leaderboard_current(request):
    return Response(services.current_leaderboard())


@api_view(["GET"])
def leaderboard_wins(request):
    return Response(services.wins_leaderboard())


@api_view(["GET"])
def leaderboard_average(request):
    return Response(services.average_leaderboard())
