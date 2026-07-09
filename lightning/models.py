from django.db import models


class LightningStrike(models.Model):
    """
    Raw strikes (hot, short retention — see lightning/management/commands/purge_strikes.py).

    Deliberately stores only geometry + time, no zone_id: zone membership is
    derived from lat/lon so re-zoning never touches this table. Scoring queries
    are kept cheap by always constraining on the (small) time window first.

    `timestamp`   = event time reported by the feed.
    `received_at` = when we ingested it (used for latency + retention).
    """

    external_id = models.CharField(max_length=100, unique=True)
    lat = models.FloatField()
    lon = models.FloatField()
    timestamp = models.DateTimeField(db_index=True)
    received_at = models.DateTimeField(db_index=True)
    quality = models.CharField(max_length=20)  # good / medium / bad
    source = models.CharField(max_length=50, default="blitzortung")

    class Meta:
        indexes = [
            models.Index(fields=["timestamp"]),
        ]

    def __str__(self):
        return f"Strike at {self.lat}, {self.lon}"


class StrikeRollupMinute(models.Model):
    """
    Pre-aggregated per-minute counts on the FINE rollup grid. The dashboard and
    'last 24h / last 15 min' numbers read from here instead of scanning raw strikes.
    Maintained by the ingest worker via INSERT .. ON CONFLICT DO UPDATE.
    """

    bucket = models.DateTimeField()             # minute-truncated UTC
    cell_id = models.CharField(max_length=32)   # fine rollup cell (rollup_cell_for)
    count = models.IntegerField(default=0)
    good = models.IntegerField(default=0)
    medium = models.IntegerField(default=0)
    bad = models.IntegerField(default=0)
    latency_sum_ms = models.BigIntegerField(default=0)
    latency_n = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["bucket", "cell_id"], name="uniq_rollup_bucket_cell"),
        ]
        indexes = [
            models.Index(fields=["bucket"]),
            models.Index(fields=["cell_id", "bucket"]),
        ]

    def __str__(self):
        return f"{self.bucket:%Y-%m-%d %H:%M} {self.cell_id}: {self.count}"
    

class CountryStrike(models.Model):
    """
    Retained, per-country rolling window of recent strikes (target: newest
    ~5000 per country). Unlike LightningStrike, this table is NOT purged, so
    the 'last 5000 per country' endpoint works even for countries that haven't
    seen a strike in years. Populated at ingest from lat/lon.
    """
    country = models.CharField(max_length=2, db_index=True)  # ISO alpha-2, 'XX' if unknown
    lat = models.FloatField()
    lon = models.FloatField()
    timestamp = models.DateTimeField()
    received_at = models.DateTimeField()
    quality = models.CharField(max_length=20)

    class Meta:
        indexes = [models.Index(fields=["country", "-received_at"])]

    def __str__(self):
        return f"{self.country} {self.lat},{self.lon}"


class CityStrikeAggregate(models.Model):
    """
    Permanent city-level counters for the top populated cities per country.

    Raw strikes still expire from LightningStrike, but these aggregates are kept
    forever for historical stats such as city/month/year rankings.
    """

    PERIOD_DAY = "day"
    PERIOD_MONTH = "month"
    PERIOD_YEAR = "year"
    PERIOD_CHOICES = (
        (PERIOD_DAY, PERIOD_DAY),
        (PERIOD_MONTH, PERIOD_MONTH),
        (PERIOD_YEAR, PERIOD_YEAR),
    )

    city_id = models.CharField(max_length=32)
    city_name = models.CharField(max_length=120)
    country = models.CharField(max_length=2, db_index=True)
    lat = models.FloatField()
    lon = models.FloatField()
    population = models.IntegerField(default=0)
    period_kind = models.CharField(max_length=8, choices=PERIOD_CHOICES)
    period_start = models.DateField()
    count = models.IntegerField(default=0)
    good = models.IntegerField(default=0)
    medium = models.IntegerField(default=0)
    bad = models.IntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["city_id", "period_kind", "period_start"],
                name="uniq_city_period",
            ),
        ]
        indexes = [
            models.Index(fields=["period_kind", "period_start"]),
            models.Index(fields=["country", "period_kind", "period_start"]),
            models.Index(fields=["city_id", "period_kind", "period_start"]),
        ]

    def __str__(self):
        return f"{self.city_name} {self.period_kind} {self.period_start}: {self.count}"


