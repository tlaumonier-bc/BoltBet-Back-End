from django.conf import settings
from django.db import models


class GameRound(models.Model):
    """One 60s (configurable) round. The timer is global — one active round at a time."""

    STATUS = [("active", "Active"), ("finished", "Finished")]

    number = models.BigIntegerField(unique=True)
    started_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    duration_seconds = models.IntegerField()  # snapshot of the config at start time
    status = models.CharField(max_length=12, choices=STATUS, default="active", db_index=True)

    class Meta:
        indexes = [models.Index(fields=["status", "ends_at"])]

    def __str__(self):
        return f"Round {self.number} ({self.status})"


class Pick(models.Model):
    """
    A 5s zone lock by one player. The zone's bounding box is copied onto the row
    at creation time, so scoring stays correct even if ZONE_SIZE_DEG changes
    while a pick is in flight. A player may hold only one un-expired pick per round.
    """

    round = models.ForeignKey(GameRound, on_delete=models.CASCADE, related_name="picks")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    zone_id = models.CharField(max_length=32)
    lon_min = models.FloatField()
    lon_max = models.FloatField()
    lat_min = models.FloatField()
    lat_max = models.FloatField()
    locked_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)  # min(locked_at + lock, round.ends_at)
    strikes_captured = models.IntegerField(default=0)
    finalized = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=["round", "user"]),
            models.Index(fields=["finalized", "expires_at"]),
        ]

    def __str__(self):
        return f"{self.user} -> {self.zone_id} ({self.strikes_captured})"


class RoundResult(models.Model):
    """Per-player score within one round. `points` == strikes captured this round."""

    round = models.ForeignKey(GameRound, on_delete=models.CASCADE, related_name="results")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    points = models.IntegerField(default=0)
    won = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["round", "user"], name="uniq_round_user"),
        ]
        indexes = [models.Index(fields=["round", "points"])]

    def __str__(self):
        return f"R{self.round_id} {self.user}: {self.points}"


class PlayerStats(models.Model):
    """
    Lifetime aggregates, one row per player. Feeds the two all-time leaderboards.
    average strikes/game = total_strikes_captured / games_played.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="stats")
    country = models.CharField(max_length=2, default="XX")  # ISO-3166 alpha-2
    games_played = models.IntegerField(default=0)
    games_won = models.IntegerField(default=0)
    total_strikes_captured = models.BigIntegerField(default=0)

    class Meta:
        indexes = [
            models.Index(fields=["games_won"]),
            models.Index(fields=["games_played"]),
        ]

    def __str__(self):
        return f"{self.user}: {self.games_won}W / {self.games_played}G"
