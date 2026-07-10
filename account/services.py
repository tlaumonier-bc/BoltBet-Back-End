"""
Up/Down game rules.

Each bet opens its own 30-second counting window at placement time. The baseline
is the previous 30 seconds for the same scope, snapshotted when the bet is
created. Strikes are counted by `received_at` (the ingest time the globe draws),
so the resolver agrees with what the strike feeds expose.
"""

import datetime as dt
import math
import re

from django.db.models import Count
from django.db import transaction
from django.utils import timezone

from lightning.models import LightningStrike, CountryStrike
from . import analytics
from .models import GridMatch, GridPlayerStats, Player, StrikeBet

GAME_MS = 30_000
PAYOUT_MULTIPLIER = 2
START_TOKENS = 100
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,20}$")
GRID_PREPARE_SECONDS = 5
GRID_GAME_SECONDS = 30
GRID_ELO_K = 32


def now_ms() -> int:
    return int(timezone.now().timestamp() * 1000)


def _ms_to_dt(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


def count_between(scope_kind: str, scope_id: str, start, end) -> int:
    """Strike count in the half-open [start, end) window for the given scope."""
    if scope_kind == "country":
        return CountryStrike.objects.filter(
            country=scope_id.upper(),
            received_at__gte=start,
            received_at__lt=end,
        ).count()
    return LightningStrike.objects.filter(
        received_at__gte=start,
        received_at__lt=end,
    ).count()


def country_playable(scope_id: str, now=None) -> bool:
    """A country is playable iff it saw >= 1 strike in the last 30s."""
    now = now or timezone.now()
    lo = now - dt.timedelta(milliseconds=GAME_MS)
    return CountryStrike.objects.filter(
        country=scope_id.upper(),
        received_at__gte=lo,
        received_at__lte=now,
    ).exists()


def country_recent_count(country: str, seconds: int = 30, now=None) -> int:
    now = now or timezone.now()
    return CountryStrike.objects.filter(
        country=country.upper(),
        received_at__gte=now - dt.timedelta(seconds=seconds),
        received_at__lte=now,
    ).count()


def grid_stats_for_player(player_or_id, lock=False) -> GridPlayerStats:
    player_id = getattr(player_or_id, "id", player_or_id)
    queryset = GridPlayerStats.objects
    if lock:
        queryset = queryset.select_for_update()
    stats, _ = queryset.get_or_create(player_id=player_id)
    return stats


def active_countries(limit: int = 8, now=None):
    now = now or timezone.now()
    since_5m = now - dt.timedelta(minutes=5)
    since_30s = now - dt.timedelta(seconds=30)
    rows_5m = (
        CountryStrike.objects
        .filter(received_at__gte=since_5m)
        .exclude(country="")
        .exclude(country="XX")
        .values("country")
        .annotate(strikes_5m=Count("id"))
        .order_by("-strikes_5m")[: max(1, min(limit * 3, 50))]
    )
    countries = [row["country"] for row in rows_5m]
    counts_30s = {
        row["country"]: row["strikes_30s"]
        for row in (
            CountryStrike.objects
            .filter(country__in=countries, received_at__gte=since_30s)
            .values("country")
            .annotate(strikes_30s=Count("id"))
        )
    }
    active = [
        {
            "country": row["country"],
            "strikes30s": counts_30s.get(row["country"], 0),
            "strikes5m": row["strikes_5m"],
        }
        for row in rows_5m
    ]
    active.sort(key=lambda row: (row["strikes30s"], row["strikes5m"]), reverse=True)
    return active[: max(1, min(limit, 20))]


def _expected_score(elo: int, opponent_elo: int) -> float:
    return 1 / (1 + math.pow(10, (opponent_elo - elo) / 400))


def _elo_delta(elo: int, opponent_elo: int, result: float) -> int:
    return round(GRID_ELO_K * (result - _expected_score(elo, opponent_elo)))


def bot_score_for(match: GridMatch, now=None) -> int:
    now = now or timezone.now()
    if now <= match.started_at:
        return 0
    elapsed = min(GRID_GAME_SECONDS, max(0, (now - match.started_at).total_seconds()))
    base_rate = max(0.4, min(4.0, match.strikes_30s_at_start / 45))
    skill = max(0.72, min(1.28, match.bot_elo / 1200))
    return int(elapsed * base_rate * skill)


@transaction.atomic
def settle_grid_match(match_id: int, now=None):
    now = now or timezone.now()
    try:
        match = GridMatch.objects.select_for_update().get(pk=match_id)
    except GridMatch.DoesNotExist:
        return None
    if match.status == "settled":
        return match
    if now < match.ends_at:
        return match

    player = Player.objects.select_for_update().get(pk=match.player_id)
    stats = grid_stats_for_player(player.id, lock=True)
    match.bot_score = max(match.bot_score, bot_score_for(match, now))
    if match.player_score > match.bot_score:
        result = 1.0
    elif match.player_score == match.bot_score:
        result = 0.5
    else:
        result = 0.0

    delta = _elo_delta(stats.grid_elo, match.bot_elo, result)
    stats.grid_elo += delta
    stats.games_played += 1
    player.games_played += 1
    if result == 1.0:
        stats.wins += 1
        player.wins += 1
    stats.save(update_fields=["grid_elo", "games_played", "wins", "updated_at"])
    player.save(update_fields=["games_played", "wins"])

    match.status = "settled"
    match.elo_after = stats.grid_elo
    match.bot_elo_after = match.bot_elo - delta
    match.settled_at = now
    match.save(update_fields=[
        "status", "bot_score", "elo_after", "bot_elo_after", "settled_at",
    ])
    analytics.capture("grid_match_resolved", player, properties={
        "match_id": match.id,
        "country": match.country,
        "player_score": match.player_score,
        "bot_score": match.bot_score,
        "elo_delta": delta,
    })
    return match


def outcome_for(side: str, prev: int, final: int) -> str:
    if final > prev:
        return "won" if side == "up" else "lost"
    if final < prev:
        return "won" if side == "down" else "lost"
    return "push"


def payout_for(outcome: str, amount: int) -> int:
    if outcome == "won":
        return amount * PAYOUT_MULTIPLIER
    if outcome == "push":
        return amount
    return 0


@transaction.atomic
def settle_bet(bet_id):
    """
    Idempotent settlement. Recomputes prev/final from the strike store, credits
    the payout exactly once, marks the bet settled, and rolls leaderboard
    counters. No-op if the window is still open or the bet is already settled.
    """
    try:
        bet = StrikeBet.objects.select_for_update().get(pk=bet_id)
    except StrikeBet.DoesNotExist:
        return None
    if bet.status == "settled":
        return bet

    window_start = bet.placed_at
    window_end = window_start + dt.timedelta(milliseconds=GAME_MS)
    if timezone.now() < window_end:
        return None  # window not closed yet

    prev = bet.prev_count
    final = count_between(bet.scope_kind, bet.scope_id, window_start, window_end)
    outcome = outcome_for(bet.side, prev, final)
    payout = payout_for(outcome, bet.amount)

    player = Player.objects.select_for_update().get(pk=bet.player_id)
    player.tokens += payout
    player.games_played += 1
    if outcome == "won":
        player.wins += 1
    player.save(update_fields=["tokens", "games_played", "wins"])

    bet.prev_count = prev
    bet.final_count = final
    bet.outcome = outcome
    bet.payout = payout
    bet.status = "settled"
    bet.settled_at = timezone.now()
    bet.save(update_fields=[
        "prev_count", "final_count", "outcome", "payout", "status", "settled_at",
    ])
    analytics.capture("bet_resolved", player, properties={
        "bet_id": bet.id,
        "outcome": outcome,
        "amount": bet.amount,
        "payout": payout,
        "scope": bet.scope_kind,
        "scope_id": bet.scope_id,
        "prev_count": prev,
        "final_count": final,
    })
    return bet