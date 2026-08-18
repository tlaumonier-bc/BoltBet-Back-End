"""URL configuration for boltbet_backend."""
from django.contrib import admin
from django.urls import include, path
from django.http import JsonResponse, HttpResponse

from lightning.views import (
    country_strikes, nearby_strikes, recent_strikes, strikes_per_minute, strikes_in_bounds,
    strikes_trend_24h,
    cities_in_bounds,
    weather_now, weather_tile, strikes_count, country_news, growth_hotspots,
    country_map_stats,
)
from lightning.weather import weather_zone, storm_track
from lightning.radar import radar_frames
from lightning.total_lightning import total_lightning
from account.views import (
    username_available, register, profile,
    change_username, change_country, place_bet, bet_result, claim_tokens,
    leaderboard, leaderboard_summary, leaderboard_context,
    grid_active_countries, grid_start_match, grid_match_state, grid_match_select_cell,
)
from account.admin_views import admin_accounts_growth
from account.firebase_auth import firebase_exchange
from account.oauth import oauth_start, oauth_callback


def health(request):
    # Cheap, no auth, no DB — for ingress / health checks.
    return HttpResponse("ok", content_type="text/plain", status=200)


def api_root(request):
    response = JsonResponse({
        "name": "Lightning Map Game API",
        "status": "ok",
        "documentation": "/api/",
    })
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def robots_txt(request):
    response = HttpResponse(
        "User-agent: *\nDisallow: /\n",
        content_type="text/plain",
    )
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


# Public REST API. Mounted under both /api/ and /public/api/ so it works whether
# or not the ingress strips the /public prefix before the request reaches us.
api_urlpatterns = [
    # --- Strike feeds ---
    path("strikes/by-country/", country_strikes),
    path("strikes/recent/", recent_strikes),
    path("strikes/nearby/", nearby_strikes),
    path("strikes/in-bounds/", strikes_in_bounds),
    path("cities/in-bounds/", cities_in_bounds),
    path("strikes/per-minute/", strikes_per_minute),
    path("strikes/trend-24h/", strikes_trend_24h),
    path("strikes/count/", strikes_count),

    # --- Weather (server-side OWM key) ---
    path("weather/now/", weather_now),
    path("weather/zone/", weather_zone),
    path("weather/storm-track/", storm_track),
    path("weather/radar/", radar_frames),
    path("lightning/total/", total_lightning),
    path("weather/tiles/<str:layer>/<int:z>/<int:x>/<int:y>.png", weather_tile),

    # --- Local SEO freshness ---
    path("news/country/", country_news),
    path("stats/country-map/", country_map_stats),

    # --- Growth engine signals ---
    path("growth/hotspots/", growth_hotspots),

    # --- Up/Down game: identity ---
    path("game/username/", username_available),
    path("game/username/change/", change_username),
    path("game/country/change/", change_country),
    path("game/register/", register),
    path("game/profile/", profile),

    # --- Up/Down game: play ---
    path("game/bet/", place_bet),
    path("game/bet/<int:bet_id>/result/", bet_result),
    path("game/claim/", claim_tokens),
    path("game/leaderboard/", leaderboard),
    path("game/leaderboard/summary/", leaderboard_summary),
    path("game/leaderboard/context/", leaderboard_context),
    path("game/grid/active-countries/", grid_active_countries),
    path("game/grid/match/", grid_start_match),
    path("game/grid/match/<int:match_id>/", grid_match_state),
    path("game/grid/match/<int:match_id>/select-cell/", grid_match_select_cell),

    # --- Private product admin ---
    path("admin/accounts-growth/", admin_accounts_growth),

    # --- OAuth (Google to start; extensible) ---
    path("auth/firebase/", firebase_exchange),
    path("auth/<str:provider>/start/", oauth_start),
    path("auth/<str:provider>/callback/", oauth_callback),
]

urlpatterns = [
    path("", api_root),
    path("health", health),
    path("robots.txt", robots_txt),
    path("admin/", admin.site.urls),

    path("api/", include(api_urlpatterns)),
    path("public/api/", include(api_urlpatterns)),
]
