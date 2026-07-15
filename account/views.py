import secrets
from datetime import timedelta
from random import randint

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import analytics
from .models import GridCellSelection, GridMatch, Player, Session, StrikeBet
from . import services


USERNAME_CHANGE_DAYS = 30
VERIFIED_PROVIDERS = {"firebase_google", "google"}
TROPHIES = [
    {"key": "bolt-tracker", "points": 200, "image": "trophy-200.png", "label": "Bolt Tracker Trophy"},
    {"key": "could-reader", "points": 500, "image": "trophy-500.png", "label": "Could Reader Trophy"},
    {"key": "strike-predictor", "points": 1000, "image": "trophy-1000.png", "label": "Strike predictor Trophy"},
    {"key": "tempest-watcher", "points": 10_000, "image": "trophy-10000.png", "label": "Tempest Watcher Trophy"},
    {"key": "lightning-lord", "points": 100_000, "image": "trophy-100000.png", "label": "Lightning Lord Trophy"},
]


def _player_from_request(request):
    """Resolve the authenticated player from the Bearer token, or None."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token:
        return None
    try:
        session = Session.objects.select_related("player").get(token=token)
    except Session.DoesNotExist:
        return None
    return None if session.player.retired else session.player


def _is_verified(player):
    return player.provider in VERIFIED_PROVIDERS


def _clean_country_code(value):
    raw = str(value or "").strip().upper()
    return raw if len(raw) == 2 and raw.isalpha() else ""


def _highest_trophy(tokens):
    earned = None
    for trophy in TROPHIES:
        if tokens >= trophy["points"]:
            earned = trophy
        else:
            break
    return earned


def _next_trophy(tokens):
    for trophy in TROPHIES:
        if tokens < trophy["points"]:
            return trophy
    return None


def _leaderboard_row(player, rank=None):
    return {
        "rank": rank,
        "username": player.username,
        "tokens": player.tokens,
        "wins": player.wins,
        "gamesPlayed": player.games_played,
        "verified": _is_verified(player),
        "country": player.country_code,
        "trophy": _highest_trophy(player.tokens),
    }


def _trophy_payloads():
    base = Player.objects.filter(retired=False)
    return [
        {
            **trophy,
            "achievedCount": base.filter(tokens__gte=trophy["points"]).count(),
        }
        for trophy in TROPHIES
    ]


def _username_change_available_at(player):
    if not player.username_changed_at:
        return None
    return player.username_changed_at + timedelta(days=USERNAME_CHANGE_DAYS)


def _can_change_username(player):
    available_at = _username_change_available_at(player)
    return available_at is None or timezone.now() >= available_at


def _profile_payload(player):
    available_at = _username_change_available_at(player)
    grid_stats = services.grid_stats_for_player(player)
    return {
        "username": player.username,
        "tokens": player.tokens,
        "gridElo": grid_stats.grid_elo,
        "verified": _is_verified(player),
        "country": player.country_code,
        "canChangeUsername": _can_change_username(player),
        "usernameChangeAvailableAt": available_at.isoformat() if available_at else None,
    }


def _grid_match_payload(match, now=None, player=None):
    now = now or timezone.now()
    if match.status != "settled" and now >= match.ends_at:
        match = services.settle_grid_match(match.id, now) or match
    if match.status == "settled":
        player_score, bot_score = match.player_score, match.bot_score
        bot_cell, bot_expires = None, None
    else:
        player_score, bot_score, bot_cell, bot_expires = services.live_state(match, now)
    player = player or Player.objects.get(pk=match.player_id)
    return {
        "matchId": str(match.id),
        "status": match.status,
        "country": match.country,
        "grid": {
            "cols": match.grid_cols,
            "rows": match.grid_rows,
            "bounds": (
                {
                    "minLat": match.area_min_lat,
                    "maxLat": match.area_max_lat,
                    "minLon": match.area_min_lon,
                    "maxLon": match.area_max_lon,
                }
                if match.area_min_lat is not None
                else None
            ),
            "cellSizeKm": match.cell_size_km,
        },
        "player": {
            "username": player.username,
            "score": player_score,
            "eloBefore": match.elo_before,
            "eloAfter": match.elo_after,
        },
        "opponent": {
            "username": match.bot_name,
            "score": bot_score,
            "elo": match.bot_elo,
            "eloAfter": match.bot_elo_after,
            "bot": True,
            "selectedCell": bot_cell,
            "selectedCellExpiresAt": bot_expires.isoformat() if bot_expires else None,
        },
        "timing": {
            "createdAt": match.created_at.isoformat(),
            "prepareEndsAt": match.prepare_ends_at.isoformat(),
            "startedAt": match.started_at.isoformat(),
            "endsAt": match.ends_at.isoformat(),
            "serverNow": now.isoformat(),
        },
        "strikes30sAtStart": match.strikes_30s_at_start,
        "model": match.model_name,
        "zone": {"hNorm": match.zone_h_norm, "roundStrikes": match.zone_round_strikes},
        "eloDelta": (
            match.elo_after - match.elo_before
            if match.elo_after is not None
            else None
        ),
    }


def _random_username():
    return f"player-{secrets.token_hex(3)}"


def _unique_username(base=None):
    candidate = base or _random_username()
    while Player.objects.filter(username_lower=candidate.lower()).exists():
        candidate = _random_username()
    return candidate


# --------------------------- identity ----------------------------------------

@api_view(["GET"])
def username_available(request):
    name = (request.GET.get("username") or "").strip()
    if not services.USERNAME_RE.match(name):
        return Response({"available": False})
    taken = Player.objects.filter(username_lower=name.lower()).exists()
    return Response({"available": not taken})


@api_view(["POST"])
def register(request):
    name = (request.data.get("username") or "").strip()
    country_code = _clean_country_code(request.data.get("countryCode"))
    if name and not services.USERNAME_RE.match(name):
        return Response({"error": "invalid_username"}, status=400)
    if not name:
        name = _unique_username()
    lower = name.lower()
    try:
        with transaction.atomic():
            if Player.objects.filter(username_lower=lower).exists():
                return Response({"error": "username_taken"}, status=409)
            player = Player.objects.create(
                username=name, username_lower=lower, tokens=services.START_TOKENS,
                country_code=country_code,
            )
            token = secrets.token_urlsafe(32)
            Session.objects.create(token=token, player=player)
    except IntegrityError:
        return Response({"error": "username_taken"}, status=409)
    payload = _profile_payload(player)
    payload["token"] = token
    analytics.capture("user_registered", player, properties={"method": "server"})
    return Response(payload)


@api_view(["GET"])
def profile(request):
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)
    return Response(_profile_payload(player))


@api_view(["POST"])
def change_username(request):
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)

    name = (request.data.get("username") or "").strip()
    if not services.USERNAME_RE.match(name):
        return Response({"error": "invalid_username"}, status=400)

    lower = name.lower()
    if lower == player.username_lower:
        return Response(_profile_payload(player))

    available_at = _username_change_available_at(player)
    if available_at and timezone.now() < available_at:
        return Response({
            "error": "username_change_locked",
            "usernameChangeAvailableAt": available_at.isoformat(),
        }, status=429)

    try:
        with transaction.atomic():
            locked = Player.objects.select_for_update().get(pk=player.pk)
            if Player.objects.filter(username_lower=lower).exclude(pk=locked.pk).exists():
                return Response({"error": "username_taken"}, status=409)
            locked.username = name
            locked.username_lower = lower
            locked.username_changed_at = timezone.now()
            locked.save(update_fields=["username", "username_lower", "username_changed_at"])
    except IntegrityError:
        return Response({"error": "username_taken"}, status=409)

    payload = _profile_payload(locked)
    analytics.capture("username_changed", locked)
    return Response(payload)


@api_view(["POST"])
def change_country(request):
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)

    country_code = _clean_country_code(request.data.get("countryCode"))
    if not country_code:
        return Response({"error": "invalid_country"}, status=400)

    with transaction.atomic():
        locked = Player.objects.select_for_update().get(pk=player.pk)
        locked.country_code = country_code
        locked.save(update_fields=["country_code"])

    payload = _profile_payload(locked)
    analytics.capture("flag_changed", locked, properties={"country_code": country_code})
    return Response(payload)


# ----------------------------- game ------------------------------------------

@api_view(["POST"])
def place_bet(request):
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)

    data = request.data
    try:
        side = str(data["side"])
        amount = int(data["amount"])
        scope_kind = str(data["scopeKind"])
        scope_id = str(data.get("scopeId") or "").upper()
    except (KeyError, TypeError, ValueError):
        return Response({"error": "bad_request"}, status=400)

    if side not in ("up", "down") or scope_kind not in ("globe", "country"):
        return Response({"error": "bad_request"}, status=400)
    if scope_kind == "globe":
        scope_id = "GLOBE"
    elif not scope_id or scope_id == "GLOBE":
        return Response({"error": "bad_scope"}, status=400)

    now = services.timezone.now()
    round_id = services.now_ms()  # legacy/audit id; the bet window starts at placed_at.

    try:
        with transaction.atomic():
            locked = Player.objects.select_for_update().get(pk=player.pk)

            # 2) one pending bet per player
            if StrikeBet.objects.filter(player=locked, status="pending").exists():
                return Response({"error": "bet_pending"}, status=409)

            # 3) amount: integer >= 1 and <= balance
            if amount < 1 or amount > locked.tokens:
                return Response({"error": "bad_amount"}, status=400)

            # 4) country must be playable
            if scope_kind == "country" and not services.country_playable(scope_id):
                return Response({"error": "not_playable"}, status=400)

            # 5) snapshot previous 30s, debit, create
            prev = services.count_between(
                scope_kind,
                scope_id,
                now - services.dt.timedelta(milliseconds=services.GAME_MS),
                now,
            )
            locked.tokens -= amount
            locked.save(update_fields=["tokens"])
            bet = StrikeBet.objects.create(
                player=locked, round_id=round_id, side=side, amount=amount,
                scope_kind=scope_kind, scope_id=scope_id, prev_count=prev,
                status="pending",
            )
            new_balance = locked.tokens
    except IntegrityError:
        return Response({"error": "bet_pending"}, status=409)

    analytics.capture("bet_placed", locked, properties={
        "bet_id": bet.id,
        "side": side,
        "amount": amount,
        "scope": scope_kind,
        "scope_id": scope_id,
        "prev_count": prev,
    })
    return Response({"betId": str(bet.id), "roundId": round_id, "tokens": new_balance})


@api_view(["GET"])
def bet_result(request, bet_id):
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)
    try:
        bet = StrikeBet.objects.get(pk=bet_id, player=player)
    except StrikeBet.DoesNotExist:
        return Response(status=404)

    # Lazy settlement: settle the first time it's polled after the window closes.
    if bet.status != "settled":
        window_end = bet.placed_at + services.dt.timedelta(milliseconds=services.GAME_MS)
        if services.timezone.now() < window_end:
            return Response(status=204)  # window still open / unsettled
        services.settle_bet(bet.id)
        bet.refresh_from_db()

    if bet.status != "settled":
        return Response(status=204)

    balance = Player.objects.values_list("tokens", flat=True).get(pk=bet.player_id)
    return Response({
        "betId": str(bet.id),
        "outcome": bet.outcome,
        "finalCount": bet.final_count,
        "payout": bet.payout,
        "tokens": balance,
    })


@api_view(["POST"])
def claim_tokens(request):
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)
    with transaction.atomic():
        locked = Player.objects.select_for_update().get(pk=player.pk)
        claimed = locked.tokens <= 0
        if locked.tokens <= 0:  # anti-abuse: only top up at zero
            locked.tokens = services.START_TOKENS
            locked.save(update_fields=["tokens"])
        payload = _profile_payload(locked)
    analytics.capture("tokens_claimed", locked, properties={"claimed": claimed})
    return Response(payload)


# -------------------------- leaderboard --------------------------------------

@api_view(["GET"])
def leaderboard(request):
    try:
        limit = int(request.GET.get("limit", 50))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))
    rows = (Player.objects.filter(retired=False)
            .order_by("-tokens", "id")[:limit])
    return Response([_leaderboard_row(p) for p in rows])


@api_view(["GET"])
def leaderboard_summary(request):
    try:
        limit = int(request.GET.get("limit", 50))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))
    players = Player.objects.filter(retired=False).order_by("-tokens", "id")[:limit]
    rows = [_leaderboard_row(player, rank=index + 1) for index, player in enumerate(players)]
    return Response({
        "entries": rows,
        "trophies": _trophy_payloads(),
        "totalPlayers": Player.objects.filter(retired=False).count(),
    })


@api_view(["GET"])
def leaderboard_context(request):
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)

    above_count = Player.objects.filter(retired=False).filter(
        Q(tokens__gt=player.tokens)
        | (Q(tokens=player.tokens) & Q(id__lt=player.id))
    ).count()
    current_rank = above_count + 1

    above = (
        Player.objects.filter(retired=False)
        .filter(
            Q(tokens__gt=player.tokens)
            | (Q(tokens=player.tokens) & Q(id__lt=player.id))
        )
        .order_by("tokens", "-id")
        .first()
    )
    below = (
        Player.objects.filter(retired=False)
        .filter(
            Q(tokens__lt=player.tokens)
            | (Q(tokens=player.tokens) & Q(id__gt=player.id))
        )
        .order_by("-tokens", "id")
        .first()
    )

    rows = []
    if above:
        rows.append(_leaderboard_row(above, current_rank - 1))
    rows.append(_leaderboard_row(player, current_rank))
    if below:
        rows.append(_leaderboard_row(below, current_rank + 1))

    return Response({
        "rows": rows,
        "currentRank": current_rank,
        "nextTrophy": _next_trophy(player.tokens),
        "trophies": _trophy_payloads(),
    })


# -------------------------- grid game ----------------------------------------

@api_view(["GET"])
def grid_active_countries(request):
    try:
        limit = int(request.GET.get("limit", 8))
    except ValueError:
        limit = 8
    return Response({
        "countries": services.active_countries(limit=limit),
        "windowSeconds": 30,
        "fallbackWindowSeconds": 300,
        "model": services.EAGZ_CONFIG.model,
    })


@api_view(["POST"])
def grid_start_match(request):
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)

    country = _clean_country_code(request.data.get("country"))
    if not country:
        return Response({"error": "bad_country"}, status=400)

    now = timezone.now()
    # EAGZ-1: pick a playable sub-country zone whose grid has >= min_round_strikes
    # (10) strikes in the last minute, counted geographically (border-agnostic).
    zone = services.eagz_zone_for_country(country, now)
    if zone is None:
        return Response({"error": "not_playable"}, status=400)

    with transaction.atomic():
        locked = Player.objects.select_for_update().get(pk=player.pk)
        grid_stats = services.grid_stats_for_player(locked, lock=True)
        existing = (
            GridMatch.objects
            .select_for_update()
            .filter(player_id=locked.id, status__in=("preparing", "active"), ends_at__gt=now)
            .order_by("-created_at")
            .first()
        )
        if existing:
            return Response(_grid_match_payload(existing, now, locked))

        started_at = now + timedelta(seconds=services.GRID_PREPARE_SECONDS)
        match = GridMatch.objects.create(
            player_id=locked.id,
            country=country,
            status="preparing",
            bot_name=f"StormBot-{randint(100, 999)}",
            bot_elo=max(800, grid_stats.grid_elo + randint(-80, 80)),
            grid_cols=zone["cols"],
            grid_rows=zone["rows"],
            strikes_30s_at_start=zone["round_strikes"],
            area_min_lat=zone["min_lat"],
            area_max_lat=zone["max_lat"],
            area_min_lon=zone["min_lon"],
            area_max_lon=zone["max_lon"],
            cell_size_km=zone["cell_size_km"],
            zone_h_norm=zone["h_norm"],
            zone_round_strikes=zone["round_strikes"],
            model_name=zone["model"],
            model_params=zone["params"],
            elo_before=grid_stats.grid_elo,
            prepare_ends_at=started_at,
            started_at=started_at,
            ends_at=started_at + timedelta(seconds=services.GRID_GAME_SECONDS),
        )

    return Response(_grid_match_payload(match, now, player))


@api_view(["GET"])
def grid_match_state(request, match_id):
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)
    try:
        match = GridMatch.objects.get(pk=match_id, player_id=player.id)
    except GridMatch.DoesNotExist:
        return Response(status=404)
    return Response(_grid_match_payload(match, player=player))


@api_view(["POST"])
def grid_match_select_cell(request, match_id):
    """Player commits to a cell for a lock window. Scoring is server-side: the
    strikes that land in this cell during its window count toward the score at
    settlement (and in live payloads). Replaces the old click-per-point endpoint."""
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)

    try:
        cell = int(request.data.get("cell"))
    except (TypeError, ValueError):
        return Response({"error": "bad_cell"}, status=400)

    now = timezone.now()
    with transaction.atomic():
        try:
            match = GridMatch.objects.select_for_update().get(pk=match_id, player_id=player.id)
        except GridMatch.DoesNotExist:
            return Response(status=404)
        if now < match.started_at:
            return Response({"error": "preparing"}, status=409)
        if now >= match.ends_at:
            settled = services.settle_grid_match(match.id, now) or match
            return Response(_grid_match_payload(settled, now, player))
        max_cell = match.grid_cols * match.grid_rows
        if cell < 0 or cell >= max_cell:
            return Response({"error": "bad_cell"}, status=400)
        if match.status != "active":
            match.status = "active"
            match.save(update_fields=["status"])
        GridCellSelection.objects.create(
            match=match, actor="player", cell=cell, started_at=now,
            expires_at=now + timedelta(seconds=services.GRID_CELL_LOCK_SECONDS),
        )

    return Response(_grid_match_payload(match, now, player))
