"""URL configuration for boltbet_backend."""
from django.contrib import admin
from django.urls import include, path
from django.http import HttpResponse

from lightning.views import (
    country_strikes, recent_strikes, strikes_per_minute,
    weather_now, weather_tile, strikes_count, country_news,
)
from account.views import (
    username_available, register, profile,
    change_username, place_bet, bet_result, claim_tokens,
    leaderboard, leaderboard_summary, leaderboard_context,
)
from account.firebase_auth import firebase_exchange
from account.oauth import oauth_start, oauth_callback


def health(request):
    # Cheap, no auth, no DB — for ingress / health checks.
    return HttpResponse("ok", content_type="text/plain", status=200)


# Public REST API. Mounted under both /api/ and /public/api/ so it works whether
# or not the ingress strips the /public prefix before the request reaches us.
api_urlpatterns = [
    # --- Strike feeds ---
    path("strikes/by-country/", country_strikes),
    path("strikes/recent/", recent_strikes),
    path("strikes/per-minute/", strikes_per_minute),
    path("strikes/count/", strikes_count),

    # --- Weather (server-side OWM key) ---
    path("weather/now/", weather_now),
    path("weather/tiles/<str:layer>/<int:z>/<int:x>/<int:y>.png", weather_tile),

    # --- Local SEO freshness ---
    path("news/country/", country_news),

    # --- Up/Down game: identity ---
    path("game/username/", username_available),
    path("game/username/change/", change_username),
    path("game/register/", register),
    path("game/profile/", profile),

    # --- Up/Down game: play ---
    path("game/bet/", place_bet),
    path("game/bet/<int:bet_id>/result/", bet_result),
    path("game/claim/", claim_tokens),
    path("game/leaderboard/", leaderboard),
    path("game/leaderboard/summary/", leaderboard_summary),
    path("game/leaderboard/context/", leaderboard_context),

    # --- OAuth (Google to start; extensible) ---
    path("auth/firebase/", firebase_exchange),
    path("auth/<str:provider>/start/", oauth_start),
    path("auth/<str:provider>/callback/", oauth_callback),
]

urlpatterns = [
    path("health", health),
    path("admin/", admin.site.urls),

    path("api/", include(api_urlpatterns)),
    path("public/api/", include(api_urlpatterns)),
]
