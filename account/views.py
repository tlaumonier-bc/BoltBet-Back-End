import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import analytics
from .models import Player, Session, StrikeBet
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
    return {
        "username": player.username,
        "tokens": player.tokens,
        "verified": _is_verified(player),
        "country": player.country_code,
        "canChangeUsername": _can_change_username(player),
        "usernameChangeAvailableAt": available_at.isoformat() if available_at else None,
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
