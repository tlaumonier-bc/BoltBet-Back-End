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

from random import randint

from lightning.models import LightningStrike, CountryStrike
from lightning.eagz import Eagz1Config, Strike, best_zone, cell_for_point
from . import analytics
from .models import GridCellSelection, GridMatch, GridPlayerStats, Player, StrikeBet

GAME_MS = 30_000
PAYOUT_MULTIPLIER = 2
START_TOKENS = 100
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,20}$")
GRID_PREPARE_SECONDS = 5
GRID_GAME_SECONDS = 60
GRID_CELL_LOCK_SECONDS = 3          # how long a picked cell stays locked & scoring
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


# --------------------------- grid zoning (EAGZ-1) ----------------------------

EAGZ_CONFIG = Eagz1Config()          # baseline: W_round=60, W_obs=600, min_round_strikes=10
EAGZ_BBOX_MARGIN_DEG = 1.0           # expand the country box so border grids see both sides
EAGZ_MAX_STRIKES = 20000             # cap the observation query


def _area_strikes(country: str, now):
    """Strikes over W_obs for zone-finding around `country`.

    Sourced GLOBALLY from LightningStrike within a bbox derived from the
    country's own recent strikes (expanded by a margin). This makes zone
    detection and the per-grid activity gate COUNTRY-AGNOSTIC: a grid straddling
    a border counts strikes from both countries, which per-country counting
    (CountryStrike) cannot do.
    """
    since = now - dt.timedelta(seconds=EAGZ_CONFIG.w_obs_seconds)
    box = list(
        CountryStrike.objects
        .filter(country=country.upper(), received_at__gte=since)
        .values_list("lat", "lon")
    )
    if not box:
        return []
    lats = [p[0] for p in box]
    lons = [p[1] for p in box]
    m = EAGZ_BBOX_MARGIN_DEG
    rows = (
        LightningStrike.objects
        .filter(
            received_at__gte=since,
            lat__gte=min(lats) - m, lat__lte=max(lats) + m,
            lon__gte=min(lons) - m, lon__lte=max(lons) + m,
        )
        .order_by("-received_at")
        .values_list("lat", "lon", "received_at")[:EAGZ_MAX_STRIKES]
    )
    return [Strike(lat, lon, ra.timestamp()) for (lat, lon, ra) in rows]


def eagz_zone_for_country(country: str, now=None):
    """Run EAGZ-1 over the area around `country` and return the best playable
    zone (dict with grid geometry + tags), or None if none qualifies."""
    now = now or timezone.now()
    strikes = _area_strikes(country, now)
    if not strikes:
        return None
    return best_zone(strikes, EAGZ_CONFIG, now.timestamp())


# --------------------- server-authoritative grid scoring ---------------------

def _zone_from_match(match: GridMatch):
    """Reconstruct the zone dict (for cell_for_point) from the stored geometry,
    or None for legacy matches without a persisted zone."""
    if match.area_min_lat is None:
        return None
    return {
        "min_lat": match.area_min_lat,
        "max_lat": match.area_max_lat,
        "min_lon": match.area_min_lon,
        "max_lon": match.area_max_lon,
        "cols": match.grid_cols,
        "rows": match.grid_rows,
    }


def generate_bot_selections(match: GridMatch):
    """Bot's full cell schedule for the round: back-to-back random cells, one per
    lock window. Created once at match start so scoring is reproducible."""
    n_cells = max(1, match.grid_cols * match.grid_rows)
    lock = dt.timedelta(seconds=GRID_CELL_LOCK_SECONDS)
    sels, t = [], match.started_at
    while t < match.ends_at:
        end = min(t + lock, match.ends_at)
        sels.append(GridCellSelection(
            match=match, actor="bot", cell=randint(0, n_cells - 1),
            started_at=t, expires_at=end,
        ))
        t = end
    GridCellSelection.objects.bulk_create(sels)


def _score_selections(selections, zone, strikes) -> int:
    """Count strikes landing in each selection's cell within its 3s lock window
    (capped at the next selection's start so windows never double-count)."""
    sels = sorted(selections, key=lambda s: s.started_at)
    score = 0
    for i, s in enumerate(sels):
        end = s.expires_at
        if i + 1 < len(sels):
            end = min(end, sels[i + 1].started_at)
        for lat, lon, received_at in strikes:
            if s.started_at <= received_at <= end and cell_for_point(lat, lon, zone) == s.cell:
                score += 1
    return score


def _round_strikes_in_zone(match: GridMatch, until):
    """LightningStrike inside the zone bbox over [started_at, until] (global, so
    it is border-agnostic like the zone gate)."""
    zone = _zone_from_match(match)
    if zone is None:
        return []
    return list(
        LightningStrike.objects.filter(
            received_at__gte=match.started_at, received_at__lte=until,
            lat__gte=zone["min_lat"], lat__lte=zone["max_lat"],
            lon__gte=zone["min_lon"], lon__lte=zone["max_lon"],
        ).values_list("lat", "lon", "received_at")
    )


def live_scores(match: GridMatch, now):
    """Authoritative (player, bot) scores computed from strikes-in-cell up to
    min(now, ends_at). Falls back to stored scores for legacy zoneless matches."""
    zone = _zone_from_match(match)
    if zone is None:
        return match.player_score, match.bot_score
    until = min(now, match.ends_at)
    strikes = _round_strikes_in_zone(match, until)
    sels = list(match.selections.all())
    player = _score_selections([s for s in sels if s.actor == "player"], zone, strikes)
    bot = _score_selections([s for s in sels if s.actor == "bot" and s.started_at <= until], zone, strikes)
    return player, bot


def bot_cell_at(match: GridMatch, now):
    """The bot's currently-active cell (for client display), or None."""
    s = (
        match.selections
        .filter(actor="bot", started_at__lte=now, expires_at__gt=now)
        .first()
    )
    return s.cell if s else None


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
    if _zone_from_match(match) is not None:
        # Server-authoritative: score both sides from strikes-in-cell.
        match.player_score, match.bot_score = live_scores(match, match.ends_at)
    else:
        # Legacy zoneless match: fall back to the simulated bot.
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
        "status", "player_score", "bot_score", "elo_after", "bot_elo_after", "settled_at",
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