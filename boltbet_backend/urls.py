"""
URL configuration for boltbet_backend project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponse
from rest_framework.routers import DefaultRouter

from game.views import (
    game_state, place_pick,
    leaderboard_current, leaderboard_wins, leaderboard_average,
)
from lightning.views import strike_stats 


def healthz(request):
    # Cheap, no auth, no DB.
    return HttpResponse("ok", content_type="text/plain", status=200)


router = DefaultRouter()

urlpatterns = [
    path("api/game/state/", game_state),
    path("api/game/pick/", place_pick),
    path("api/game/leaderboard/current/", leaderboard_current),
    path("api/game/leaderboard/wins/", leaderboard_wins),
    path("api/game/leaderboard/average/", leaderboard_average),
    path("api/stats/strikes/", strike_stats),
]