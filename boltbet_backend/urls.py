"""URL configuration for boltbet_backend."""
from django.contrib import admin
from django.urls import include, path
from django.http import HttpResponse

from game.views import (
    game_state, place_pick,
    leaderboard_current, leaderboard_wins, leaderboard_average,
)
from lightning.views import country_strikes, recent_strikes, strikes_per_minute, weather_now
from bets.views import create_bet


def healthz(request):
    # Cheap, no auth, no DB — for ingress / Nomad health checks.
    return HttpResponse("ok", content_type="text/plain", status=200)


# Public REST API. Mounted under both /api/ and /public/api/ so it works whether
# or not the ingress strips the /public prefix before the request reaches us.
api_urlpatterns = [
    path("game/state/", game_state),
    path("game/pick/", place_pick),
    path("game/leaderboard/current/", leaderboard_current),
    path("game/leaderboard/wins/", leaderboard_wins),
    path("game/leaderboard/average/", leaderboard_average),
    path("bets/", create_bet),
    path("strikes/by-country/", country_strikes),
    path("strikes/recent/", recent_strikes),
    path("strikes/per-minute/", strikes_per_minute),
    path("weather/now/", weather_now),
]

urlpatterns = [
    path("healthz", healthz),
    path("admin/", admin.site.urls),

    path("api/", include(api_urlpatterns)),
    path("public/api/", include(api_urlpatterns)),
]