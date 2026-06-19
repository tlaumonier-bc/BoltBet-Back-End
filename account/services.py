"""
Up/Down game rules. Timing is anchored to wall-clock (epoch ms, UTC), matching
lib/game/useStrikeGame.ts on the client.

  cycle c spans [c*40000, c*40000+40000):
    game window = [c*40000, c*40000+30000)  -> strikes counted (half-open)
    buffer      = [c*40000+30000, c*40000+40000)  -> betting open for game c+1

Strikes are counted by `received_at` (the ingest time the globe draws), so the
resolver agrees with what the strike feeds expose.
"""

import datetime as dt
import re

from django.db import transaction
from django.utils import timezone

from lightning.models import LightningStrike, CountryStrike
from .models import Player, StrikeBet

GAME_MS = 30_000
BUFFER_MS = 10_000
CYCLE_MS = 40_000
PAYOUT_MULTIPLIER = 2
START_TOKENS = 100
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,20}$")


def now_ms() -> int:
    return int(timezone.now().timestamp() * 1000)


def _ms_to_dt(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)


def count_window(scope_kind: str, scope_id: str, round_id: int) -> int:
    """Strike count in the game window of `round_id`, for the given scope."""
    lo_ms = round_id * CYCLE_MS
    lo = _ms_to_dt(lo_ms)
    hi = _ms_to_dt(lo_ms + GAME_MS)  # half-open [lo, hi)
    if scope_kind == "country":
        return CountryStrike.objects.filter(
            country=scope_id.upper(),
            received_at__gte=lo,
            received_at__lt=hi,
        ).count()
    return LightningStrike.objects.filter(
        received_at__gte=lo,
        received_at__lt=hi,
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
    if now_ms() < bet.round_id * CYCLE_MS + GAME_MS:
        return None  # window not closed yet

    prev = count_window(bet.scope_kind, bet.scope_id, bet.round_id - 1)
    final = count_window(bet.scope_kind, bet.scope_id, bet.round_id)
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
    return bet