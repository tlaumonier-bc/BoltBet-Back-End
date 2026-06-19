from django.contrib import admin
from .models import Player, Session, StrikeBet


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