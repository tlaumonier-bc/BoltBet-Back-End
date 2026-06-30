import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Player, Session, StrikeBet
from . import services


USERNAME_CHANGE_DAYS = 30
VERIFIED_PROVIDERS = {"firebase_google", "google"}


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
            )
            token = secrets.token_urlsafe(32)
            Session.objects.create(token=token, player=player)
    except IntegrityError:
        return Response({"error": "username_taken"}, status=409)
    payload = _profile_payload(player)
    payload["token"] = token
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

    return Response(_profile_payload(locked))


# ----------------------------- game ------------------------------------------

@api_view(["POST"])
def place_bet(request):
    player = _player_from_request(request)
    if player is None:
        return Response(status=401)

    data = request.data
    try:
        round_id = int(data["roundId"])
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

    # 1) Betting window open: current cycle == roundId-1 AND we're in the buffer.
    now = services.now_ms()
    cycle = now // services.CYCLE_MS
    offset = now - cycle * services.CYCLE_MS
    if not (cycle == round_id - 1 and offset >= services.GAME_MS):
        return Response({"error": "betting_closed"}, status=400)

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

            # 5) snapshot prev (game window of roundId-1), debit, create
            prev = services.count_window(scope_kind, scope_id, round_id - 1)
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
        if services.now_ms() < bet.round_id * services.CYCLE_MS + services.GAME_MS:
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
        if locked.tokens <= 0:  # anti-abuse: only top up at zero
            locked.tokens = services.START_TOKENS
            locked.save(update_fields=["tokens"])
        payload = _profile_payload(locked)
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
    return Response([
        {"username": p.username, "tokens": p.tokens,
         "wins": p.wins, "gamesPlayed": p.games_played,
         "verified": _is_verified(p)}
        for p in rows
    ])
