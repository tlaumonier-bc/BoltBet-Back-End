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
    country_code = models.CharField(max_length=2, blank=True, default="")
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
    One Up/Down bet. The counting window starts at `placed_at` and runs for 30s.
    `round_id` is retained as a legacy/audit identifier for old clients.
    Resolution is server-authoritative: prev_count is snapshotted at placement
    and final_count is recomputed from the strike store.
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


class GridPlayerStats(models.Model):
    """Grid-game ranking state, kept outside account_player to avoid ALTERs."""

    player_id = models.BigIntegerField(unique=True, db_index=True)
    grid_elo = models.IntegerField(default=1200)
    games_played = models.IntegerField(default=0)
    wins = models.IntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"player {self.player_id}: {self.grid_elo}"


class GridMatch(models.Model):
    """
    One 30s grid game against a bot.

    The bot is intentionally server-side from day one so the client shape will
    still work when matchmaking swaps it for a real opponent later.
    """

    STATUS = [
        ("preparing", "preparing"),
        ("active", "active"),
        ("settled", "settled"),
    ]

    player_id = models.BigIntegerField(db_index=True)
    country = models.CharField(max_length=2, db_index=True)
    status = models.CharField(max_length=10, choices=STATUS, default="preparing", db_index=True)
    bot_name = models.CharField(max_length=40, default="StormBot")
    bot_elo = models.IntegerField(default=1200)
    bot_elo_after = models.IntegerField(null=True, blank=True)
    player_score = models.IntegerField(default=0)
    bot_score = models.IntegerField(default=0)
    grid_cols = models.IntegerField(default=10)
    grid_rows = models.IntegerField(default=8)
    # EAGZ-1 zone geometry (equirectangular bbox of the grid) + model tags.
    area_min_lat = models.FloatField(null=True, blank=True)
    area_max_lat = models.FloatField(null=True, blank=True)
    area_min_lon = models.FloatField(null=True, blank=True)
    area_max_lon = models.FloatField(null=True, blank=True)
    cell_size_km = models.FloatField(null=True, blank=True)
    zone_h_norm = models.FloatField(null=True, blank=True)
    zone_round_strikes = models.IntegerField(null=True, blank=True)
    model_name = models.CharField(max_length=20, default="EAGZ-1")
    model_params = models.JSONField(null=True, blank=True)
    strikes_30s_at_start = models.IntegerField(default=0)
    elo_before = models.IntegerField(default=1200)
    elo_after = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    prepare_ends_at = models.DateTimeField()
    started_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["player_id", "status"], name="account_gri_player_41cdb2_idx"),
            models.Index(fields=["country", "-created_at"], name="account_gri_country_71f19d_idx"),
        ]

    def __str__(self):
        return f"{self.player_id} vs {self.bot_name} in {self.country} ({self.status})"


class GridCellSelection(models.Model):
    """One cell a player or the bot committed to for a 3s lock window. Scoring is
    authoritative: at (and during) settlement the server counts strikes that fall
    in the selected cell within its effective window. The bot's whole schedule is
    generated up front at match creation; the player's are appended as they tap."""

    ACTORS = [("player", "player"), ("bot", "bot")]

    match = models.ForeignKey(GridMatch, on_delete=models.CASCADE, related_name="selections")
    actor = models.CharField(max_length=6, choices=ACTORS)
    cell = models.IntegerField()
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["match", "actor", "started_at"])]

    def __str__(self):
        return f"{self.actor} cell {self.cell} @ {self.started_at:%H:%M:%S}"