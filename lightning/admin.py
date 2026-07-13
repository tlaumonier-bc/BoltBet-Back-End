from django.contrib import admin

from .models import CityStrikeAggregate


@admin.register(CityStrikeAggregate)
class CityStrikeAggregateAdmin(admin.ModelAdmin):
    list_display = (
        "city_name", "country", "period_kind", "period_start",
        "count", "good", "medium", "bad",
    )
    list_filter = ("country", "period_kind", "period_start")
    search_fields = ("city_name", "city_id")
    ordering = ("-period_start", "-count")
