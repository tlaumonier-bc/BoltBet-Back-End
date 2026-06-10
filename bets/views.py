from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Sum, Count, Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Bet

User = get_user_model()


def _default_user():
    # MVP anonymous fallback. Replace with real auth later.
    user, _ = User.objects.get_or_create(
        username="anonymous", defaults={"is_active": True}
    )
    return user


@api_view(["POST"])
def create_bet(request):
    data = request.data
    required = ("cell_id", "multiplier_at_bet", "amount", "duration_minutes")
    if any(data.get(k) in (None, "") for k in required):
        return Response({"error": "missing fields"}, status=400)

    user = request.user if request.user.is_authenticated else _default_user()
    duration = int(data["duration_minutes"])
    bet = Bet.objects.create(
        user=user,
        cell_id=data["cell_id"],
        multiplier_at_bet=float(data["multiplier_at_bet"]),
        amount=int(data["amount"]),
        duration_minutes=duration,
        expires_at=timezone.now() + timedelta(minutes=duration),
        status="pending",
    )
    return Response({"id": bet.id, "status": bet.status}, status=201)


@api_view(["GET"])
def leaderboard(request):
    rows = (
        User.objects.annotate(
            credits=Sum("bet__payout", filter=Q(bet__status="won")),
            wins=Count("bet", filter=Q(bet__status="won")),
        )
        .filter(wins__gt=0)
        .order_by("-credits")[:10]
    )
    return Response(
        [{"username": u.username, "credits": u.credits or 0, "wins": u.wins} for u in rows]
    )