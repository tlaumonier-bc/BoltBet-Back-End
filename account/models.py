from django.db import models


class Player(models.Model):
    """
    A game account. Token-based identity (no Django auth User), so it stays
    independent of the legacy zone-game apps. Username uniqueness is enforced
    case-insensitively via `username_lower`. OAuth accounts also carry a
    (provider, provider_subject) identity.
    """

    username = models.CharField(max_length=20)
    username_lower = models.CharField(max_length=20, unique=True)
    tokens = models.IntegerField(default=100)
    provider = models.CharField(max_length=20, blank=True, default="")
    provider_subject = models.CharField(max_length=255, blank=True, default="")
    wins = models.IntegerField(default=0)
    games_played = models.IntegerField(default=0)
    retired = models.BooleanField(default=False)  # merged guest accounts
    username_changed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_subject"],
                condition=models.Q(provider__gt=""),
                name="uniq_oauth_identity",
            ),
        ]

    def __str__(self):
        return f"{self.username} ({self.tokens})"


class Session(models.Model):
    """Opaque session token -> player. Multiple per player (multi-device)."""

    token = models.CharField(max_length=64, unique=True, db_index=True)
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="sessions")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"session for {self.player_id}"


class StrikeBet(models.Model):
    """
    One Up/Down bet for a 40s cycle. The window is identified by round_id
    (= floor(now/40000) at game time). Resolution is server-authoritative:
    prev_count / final_count are (re)computed from the strike store.
    """

    SIDES = [("up", "up"), ("down", "down")]
    SCOPES = [("globe", "globe"), ("country", "country")]
    STATUS = [("pending", "pending"), ("settled", "settled")]
    OUTCOMES = [("won", "won"), ("lost", "lost"), ("push", "push")]

    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="bets")
    round_id = models.BigIntegerField(db_index=True)
    side = models.CharField(max_length=4, choices=SIDES)
    amount = models.IntegerField()
    scope_kind = models.CharField(max_length=8, choices=SCOPES)
    scope_id = models.CharField(max_length=8)  # 'GLOBE' or ISO-2 (uppercase)
    prev_count = models.IntegerField()
    final_count = models.IntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=5, choices=OUTCOMES, null=True, blank=True)
    payout = models.IntegerField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=STATUS, default="pending", db_index=True)
    placed_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "round_id"], name="account_str_status_idx")]
        constraints = [
            models.UniqueConstraint(
                fields=["player"],
                condition=models.Q(status="pending"),
                name="uniq_pending_bet_per_player",
            ),
        ]

    def __str__(self):
        return f"{self.player_id} {self.side} {self.amount} r{self.round_id} ({self.status})"