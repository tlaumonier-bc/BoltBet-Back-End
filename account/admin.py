from django.contrib import admin
from .models import GridMatch, GridPlayerStats, Player, Session, StrikeBet


@admin.register(Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ("id", "username", "tokens", "wins", "games_played", "provider", "retired")
    search_fields = ("username", "username_lower", "provider_subject")
    list_filter = ("provider", "retired")


@admin.register(Session)
class SessionAdmin(admin.ModelAdmin):
    list_display = ("id", "player", "created_at")


@admin.register(StrikeBet)
class StrikeBetAdmin(admin.ModelAdmin):
    list_display = ("id", "player", "round_id", "side", "amount",
                    "scope_kind", "scope_id", "status", "outcome", "payout")
    list_filter = ("status", "outcome", "scope_kind")
    search_fields = ("scope_id",)


@admin.register(GridPlayerStats)
class GridPlayerStatsAdmin(admin.ModelAdmin):
    list_display = ("id", "player_id", "grid_elo", "wins", "games_played", "updated_at")
    search_fields = ("player_id",)


@admin.register(GridMatch)
class GridMatchAdmin(admin.ModelAdmin):
    list_display = (
        "id", "player_id", "country", "status", "player_score", "bot_score",
        "elo_before", "elo_after", "created_at",
    )
    list_filter = ("status", "country")
    search_fields = ("player_id", "bot_name", "country")