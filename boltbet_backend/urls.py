"""URL configuration for boltbet_backend."""
from django.contrib import admin
from django.urls import path
from django.http import HttpResponse

from game.views import (
    game_state, place_pick,
    leaderboard_current, leaderboard_wins, leaderboard_average,
)
from lightning.views import strike_stats
from bets.views import create_bet


def healthz(request):
    # Cheap, no auth, no DB — for ingress / Nomad health checks.
    return HttpResponse("ok", content_type="text/plain", status=200)


urlpatterns = [
    path("healthz", healthz),
    path("admin/", admin.site.urls),

    path("api/game/state/", game_state),
    path("api/game/pick/", place_pick),
    path("api/game/leaderboard/current/", leaderboard_current),
    path("api/game/leaderboard/wins/", leaderboard_wins),
    path("api/game/leaderboard/average/", leaderboard_average),

    path("api/stats/strikes/", strike_stats),
    path("api/bets/", create_bet),
]