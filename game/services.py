"""
game/services.py — all game logic, framework-agnostic so it can be called from
the run_game loop, DRF views, or tests.

Scoring is server-authoritative: the client never reports captures. A strike
scores for a pick iff strike.timestamp is within [locked_at, expires_at) AND its
lat/lon is inside the pick's stored box. Because feed strikes arrive a few
seconds late, picks are finalized only SCORE_GRACE_SECONDS after they expire, so
late-but-in-window strikes still count.
"""

import datetime as dt
import os

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from lightning.models import LightningStrike
from lightning.grid import zone_bounds
from .models import GameRound, Pick, RoundResult, PlayerStats
from .live_leaderboard import get_live_board

User = get_user_model()

ROUND_SECONDS = int(os.environ.get("GAME_ROUND_SECONDS", "60"))
LOCK_SECONDS = int(os.environ.get("GAME_LOCK_SECONDS", "5"))
SCORE_GRACE_SECONDS = int(os.environ.get("GAME_SCORE_GRACE_SECONDS", "6"))
MIN_GAMES_FOR_AVG = int(os.environ.get("LEADERBOARD_MIN_GAMES", "5"))
LEADERBOARD_LIMIT = int(os.environ.get("LEADERBOARD_LIMIT", "20"))


class PickError(Exception):
    """Raised for invalid pick attempts (no_active_round / already_locked / bad_zone)."""


# --------------------------- round lifecycle ---------------------------------

def get_active_round():
    return GameRound.objects.filter(status="active").order_by("-number").first()


def start_round(now=None) -> GameRound:
    now = now or timezone.now()
    last = GameRound.objects.order_by("-number").first()
    number = (last.number + 1) if last else 1
    return GameRound.objects.create(
        number=number,
        started_at=now,
        ends_at=now + dt.timedelta(seconds=ROUND_SECONDS),
        duration_seconds=ROUND_SECONDS,
        status="active",
    )


@transaction.atomic
def close_round(rnd: GameRound, now=None):
    """Finalize remaining picks, decide the winner(s), roll lifetime stats."""
    now = now or timezone.now()

    for pick in Pick.objects.filter(round=rnd, finalized=False).select_related("round", "user").iterator():
        _finalize_pick(pick)

    rnd.status = "finished"
    rnd.save(update_fields=["status"])

    results = list(RoundResult.objects.filter(round=rnd))
    top = max((r.points for r in results), default=0)
    winner_ids = {r.user_id for r in results if r.points == top and top > 0}

    for r in results:
        if r.user_id in winner_ids and not r.won:
            RoundResult.objects.filter(pk=r.pk).update(won=True)
        stats, _ = PlayerStats.objects.get_or_create(user_id=r.user_id)
        PlayerStats.objects.filter(pk=stats.pk).update(
            games_played=F("games_played") + 1,
            total_strikes_captured=F("total_strikes_captured") + r.points,
            games_won=F("games_won") + (1 if r.user_id in winner_ids else 0),
        )
    return winner_ids


# ------------------------------- picks ---------------------------------------

@transaction.atomic
def place_pick(user, zone_id, now=None) -> Pick:
    now = now or timezone.now()
    rnd = get_active_round()
    if not rnd or rnd.ends_at <= now:
        raise PickError("no_active_round")
    if Pick.objects.filter(round=rnd, user=user, expires_at__gt=now).exists():
        raise PickError("already_locked")
    try:
        lon_min, lon_max, lat_min, lat_max = zone_bounds(zone_id)
    except Exception:
        raise PickError("bad_zone")

    # Clamp the lock to the round end so every pick finalizes within this round.
    expires = min(now + dt.timedelta(seconds=LOCK_SECONDS), rnd.ends_at)
    return Pick.objects.create(
        round=rnd, user=user, zone_id=zone_id,
        lon_min=lon_min, lon_max=lon_max, lat_min=lat_min, lat_max=lat_max,
        locked_at=now, expires_at=expires,
    )


def _count_for_pick(pick: Pick) -> int:
    return LightningStrike.objects.filter(
        timestamp__gte=pick.locked_at,
        timestamp__lt=pick.expires_at,
        lat__gte=pick.lat_min, lat__lt=pick.lat_max,
        lon__gte=pick.lon_min, lon__lt=pick.lon_max,
    ).count()


@transaction.atomic
def _finalize_pick(pick: Pick) -> int:
    captured = _count_for_pick(pick)
    pick.strikes_captured = captured
    pick.finalized = True
    pick.save(update_fields=["strikes_captured", "finalized"])
    result, _ = RoundResult.objects.get_or_create(round_id=pick.round_id, user_id=pick.user_id)
    if captured:
        RoundResult.objects.filter(pk=result.pk).update(points=F("points") + captured)
    get_live_board().incr(pick.round, pick.user_id, captured)
    return captured


def finalize_due_picks(now=None) -> int:
    """Score every pick whose window + grace has elapsed. Returns how many."""
    now = now or timezone.now()
    cutoff = now - dt.timedelta(seconds=SCORE_GRACE_SECONDS)
    n = 0
    for pick in Pick.objects.filter(finalized=False, expires_at__lte=cutoff).select_related("round", "user").iterator():
        _finalize_pick(pick)
        n += 1
    return n


# ----------------------------- leaderboards ----------------------------------

def leaderboard_for_round(rnd, limit=LEADERBOARD_LIMIT):
    if rnd is None:
        return []
    rows = (RoundResult.objects.filter(round=rnd)
            .select_related("user").order_by("-points")[:limit])
    stats = {s.user_id: s.country
             for s in PlayerStats.objects.filter(user_id__in=[r.user_id for r in rows])}
    return [{"username": r.user.username,
             "country": stats.get(r.user_id, "XX"),
             "points": r.points} for r in rows]


def current_leaderboard(limit=LEADERBOARD_LIMIT):
    return get_live_board().top(get_active_round(), limit)


def wins_leaderboard(limit=LEADERBOARD_LIMIT):
    rows = (PlayerStats.objects.select_related("user")
            .filter(games_won__gt=0).order_by("-games_won")[:limit])
    return [{"username": s.user.username, "country": s.country, "games_won": s.games_won}
            for s in rows]


def average_leaderboard(limit=LEADERBOARD_LIMIT):
    # Min-games gate so a single lucky round can't top the board forever.
    rows = (PlayerStats.objects.select_related("user")
            .filter(games_played__gte=MIN_GAMES_FOR_AVG))
    scored = [{"username": s.user.username, "country": s.country,
               "avg_strikes": round(s.total_strikes_captured / s.games_played, 2),
               "games_played": s.games_played}
              for s in rows if s.games_played]
    scored.sort(key=lambda x: x["avg_strikes"], reverse=True)
    return scored[:limit]
