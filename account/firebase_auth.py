import os
import re
import secrets

import firebase_admin
from firebase_admin import auth as firebase_auth
from firebase_admin import credentials
from django.db import IntegrityError, transaction
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import services
from .models import Player, Session


FIREBASE_PROVIDER = "firebase"


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


def _preferred_username(decoded_token):
    name = decoded_token.get("name") or ""
    email = decoded_token.get("email") or ""
    return decoded_token.get("displayName") or name or (email.split("@")[0] if email else "") or "player"


def _legacy_google_subject(decoded_token):
    identities = (decoded_token.get("firebase") or {}).get("identities") or {}
    google_ids = identities.get("google.com") or []
    return str(google_ids[0]) if google_ids else ""


@api_view(["POST"])
def firebase_exchange(request):
    id_token = (request.data.get("idToken") or "").strip()
    link_token = (request.data.get("linkToken") or "").strip()
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

    legacy_google_subject = _legacy_google_subject(decoded)

    try:
        with transaction.atomic():
            existing = (
                Player.objects.select_for_update()
                .filter(provider=FIREBASE_PROVIDER, provider_subject=firebase_uid)
                .first()
            )

            if not existing and legacy_google_subject:
                existing = (
                    Player.objects.select_for_update()
                    .filter(provider="google", provider_subject=legacy_google_subject)
                    .first()
                )
                if existing:
                    existing.provider = FIREBASE_PROVIDER
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
                    guest.bets.update(player=player)
                    guest.retired = True
                    guest.username_lower = f"_r{guest.pk}"[:20]
                    guest.save(update_fields=["retired", "username_lower"])
                    player.save(update_fields=["tokens", "wins", "games_played"])
            elif guest:
                guest.provider = FIREBASE_PROVIDER
                guest.provider_subject = firebase_uid
                guest.save(update_fields=["provider", "provider_subject"])
                player = guest
            else:
                username = _unique_username(_preferred_username(decoded))
                player = Player.objects.create(
                    username=username,
                    username_lower=username.lower(),
                    tokens=services.START_TOKENS,
                    provider=FIREBASE_PROVIDER,
                    provider_subject=firebase_uid,
                )

            token = secrets.token_urlsafe(32)
            Session.objects.create(token=token, player=player)
    except IntegrityError:
        return Response({"error": "identity_conflict"}, status=409)

    return Response({"username": player.username, "token": token, "tokens": player.tokens})
