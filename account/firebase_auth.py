import os
import re
import secrets
from datetime import timedelta

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import services
from .models import Player, Session


FIREBASE_PROVIDER = "firebase"
FIREBASE_GOOGLE_PROVIDER = "firebase_google"
VERIFIED_FIREBASE_PROVIDERS = {"google.com": FIREBASE_GOOGLE_PROVIDER}
USERNAME_CHANGE_DAYS = 30


def _clean_country_code(value):
    raw = str(value or "").strip().upper()
    return raw if len(raw) == 2 and raw.isalpha() else ""


def _username_change_available_at(player):
    if not player.username_changed_at:
        return None
    return player.username_changed_at + timedelta(days=USERNAME_CHANGE_DAYS)


def _firebase_app():
    """Initialize Firebase Admin lazily so local commands do not need credentials."""
    if firebase_admin._apps:
        return firebase_admin.get_app()
    try:
        cred = credentials.ApplicationDefault()
        project_id = os.environ.get("FIREBASE_PROJECT_ID")
        if project_id:
            return firebase_admin.initialize_app(cred, {"projectId": project_id})
        return firebase_admin.initialize_app(cred)
    except Exception as exc:
        raise RuntimeError("firebase_unconfigured") from exc


def _unique_username(base):
    base = re.sub(r"[^a-zA-Z0-9_-]", "", base or "")[:20]
    if len(base) < 3:
        base = (base + "player")[:20]
    candidate = base
    i = 0
    while Player.objects.filter(username_lower=candidate.lower()).exists():
        i += 1
        suffix = str(i)
        candidate = base[: 20 - len(suffix)] + suffix
    return candidate


def _random_username():
    return f"player-{secrets.token_hex(3)}"


def _legacy_google_subject(decoded_token):
    identities = (decoded_token.get("firebase") or {}).get("identities") or {}
    google_ids = identities.get("google.com") or []
    return str(google_ids[0]) if google_ids else ""


@api_view(["POST"])
def firebase_exchange(request):
    id_token = (request.data.get("idToken") or "").strip()
    link_token = (request.data.get("linkToken") or "").strip()
    country_code = _clean_country_code(request.data.get("countryCode"))
    if not id_token:
        return Response({"error": "missing_id_token"}, status=400)

    try:
        decoded = firebase_auth.verify_id_token(id_token, app=_firebase_app())
    except RuntimeError:
        return Response({"error": "firebase_unconfigured"}, status=503)
    except Exception:
        return Response({"error": "invalid_firebase_token"}, status=401)

    firebase_uid = str(decoded.get("uid") or "")
    if not firebase_uid:
        return Response({"error": "no_subject"}, status=400)

    sign_in_provider = str((decoded.get("firebase") or {}).get("sign_in_provider") or "")
    provider = VERIFIED_FIREBASE_PROVIDERS.get(sign_in_provider)
    if not provider:
        return Response({"error": "unsupported_firebase_provider"}, status=400)

    legacy_google_subject = _legacy_google_subject(decoded)

    try:
        with transaction.atomic():
            existing = (
                Player.objects.select_for_update()
                .filter(provider__in=[provider, FIREBASE_PROVIDER], provider_subject=firebase_uid)
                .first()
            )
            if existing and existing.provider != provider:
                existing.provider = provider
                existing.save(update_fields=["provider"])

            if not existing and legacy_google_subject:
                existing = (
                    Player.objects.select_for_update()
                    .filter(provider="google", provider_subject=legacy_google_subject)
                    .first()
                )
                if existing:
                    existing.provider = provider
                    existing.provider_subject = firebase_uid
                    existing.save(update_fields=["provider", "provider_subject"])

            guest = None
            if link_token:
                sess = Session.objects.select_related("player").filter(token=link_token).first()
                if sess and not sess.player.retired and not sess.player.provider:
                    guest = Player.objects.select_for_update().get(pk=sess.player_id)

            if existing:
                player = existing
                if guest and guest.pk != player.pk:
                    player.tokens += guest.tokens
                    player.wins += guest.wins
                    player.games_played += guest.games_played
                    if country_code and not player.country_code:
                        player.country_code = country_code
                    elif guest.country_code and not player.country_code:
                        player.country_code = guest.country_code
                    guest.bets.update(player=player)
                    guest.retired = True
                    guest.username_lower = f"_r{guest.pk}"[:20]
                    guest.save(update_fields=["retired", "username_lower"])
                    player.save(update_fields=["tokens", "wins", "games_played", "country_code"])
                elif country_code and not player.country_code:
                    player.country_code = country_code
                    player.save(update_fields=["country_code"])
            elif guest:
                guest.provider = provider
                guest.provider_subject = firebase_uid
                if country_code and not guest.country_code:
                    guest.country_code = country_code
                guest.save(update_fields=["provider", "provider_subject", "country_code"])
                player = guest
            else:
                username = _unique_username(_random_username())
                player = Player.objects.create(
                    username=username,
                    username_lower=username.lower(),
                    tokens=services.START_TOKENS,
                    provider=provider,
                    provider_subject=firebase_uid,
                    country_code=country_code,
                )

            token = secrets.token_urlsafe(32)
            Session.objects.create(token=token, player=player)
    except IntegrityError:
        return Response({"error": "identity_conflict"}, status=409)

    available_at = _username_change_available_at(player)
    return Response({
        "username": player.username,
        "token": token,
        "tokens": player.tokens,
        "verified": True,
        "country": player.country_code,
        "canChangeUsername": available_at is None or timezone.now() >= available_at,
        "usernameChangeAvailableAt": available_at.isoformat() if available_at else None,
    })
