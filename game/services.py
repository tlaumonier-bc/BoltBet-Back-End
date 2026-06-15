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
INTERMISSION_SECONDS = int(os.environ.get("GAME_INTERMISSION_SECONDS", "10"))


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
        _score_pick(pick, now, force_final=True)

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
    pick = Pick.objects.create(
        round=rnd, user=user, zone_id=zone_id,
        lon_min=lon_min, lon_max=lon_max, lat_min=lat_min, lat_max=lat_max,
        locked_at=now, expires_at=expires,
    )
    # Put the player on the live board at 0 immediately, before they score.
    RoundResult.objects.get_or_create(round_id=rnd.id, user_id=user.id)
    return pick


def _strikes_in(pick: Pick, upper) -> int:
    if upper <= pick.locked_at:
        return 0
    return LightningStrike.objects.filter(
        # received_at = when we ingested/broadcast the strike, i.e. when it was
        # drawn on the globe. Same wall clock as locked_at/expires_at, so the
        # count matches what the player watched land in the zone. (Scoring on the
        # feed's event `timestamp` undercounts, because strikes arrive a few
        # seconds after the event they describe.)
        received_at__gte=pick.locked_at,
        received_at__lt=upper,
        lat__gte=pick.lat_min, lat__lt=pick.lat_max,
        lon__gte=pick.lon_min, lon__lt=pick.lon_max,
    ).count()


@transaction.atomic
def _score_pick(pick: Pick, now, force_final: bool = False) -> int:
    """
    Idempotent, delta-based scoring. Recomputes the pick's captured count up to
    min(now, expires_at) and adds only the INCREASE since the last pass, so the
    live board climbs ~once per tick instead of in one lump — and re-running can
    never double count. Finalizes once the window + grace has elapsed.
    """
    window_end = min(now, pick.expires_at)
    new_count = _strikes_in(pick, window_end)
    delta = new_count - pick.strikes_captured
    fields = []
    if delta > 0:
        pick.strikes_captured = new_count
        fields.append("strikes_captured")
        result, _ = RoundResult.objects.get_or_create(round_id=pick.round_id, user_id=pick.user_id)
        RoundResult.objects.filter(pk=result.pk).update(points=F("points") + delta)
        get_live_board().incr(pick.round, pick.user_id, delta)
    done = force_final or now >= pick.expires_at + dt.timedelta(seconds=SCORE_GRACE_SECONDS)
    if done and not pick.finalized:
        pick.finalized = True
        fields.append("finalized")
    if fields:
        pick.save(update_fields=fields)
    return delta


def score_picks(now=None) -> bool:
    """Score every not-yet-finalized pick this tick. Returns True if anything changed."""
    now = now or timezone.now()
    changed = False
    for pick in Pick.objects.filter(finalized=False).select_related("round", "user").iterator():
        if _score_pick(pick, now) > 0:
            changed = True
    return changed


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
