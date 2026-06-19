"""
OAuth (Google to start, extensible). Carries `next` + `link` through a signed
`state`, exchanges the code server-side, finds/creates the player, optionally
merges a linked guest, then redirects to <next>#auth_token=...&auth_user=...

Env required (per provider):
  GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET
Optional:
  OAUTH_PUBLIC_BASE   e.g. https://api.lightningmapbets.com  (else built from request)
Register the callback URL <base>/api/auth/google/callback/ in the Google console.
"""

import json
import os
import re
import secrets
import urllib.parse
import urllib.request

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.http import HttpResponseRedirect, HttpResponseBadRequest, JsonResponse

from .models import Player, Session
from . import services

PROVIDERS = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
    },
}
STATE_SALT = "boltbet.oauth.state"


def _provider(name):
    return PROVIDERS.get(name)


def _public_base(request):
    base = os.environ.get("OAUTH_PUBLIC_BASE", "").rstrip("/")
    return base or f"{request.scheme}://{request.get_host()}"


def _redirect_uri(request, provider):
    return f"{_public_base(request)}/api/auth/{provider}/callback/"


def _safe_next(next_url):
    """Prevent open redirects: allow relative paths or known site origins."""
    if not next_url:
        return "/"
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    for origin in getattr(settings, "CORS_ALLOWED_ORIGINS", []) or []:
        if next_url.startswith(origin):
            return next_url
    try:
        return urllib.parse.urlparse(next_url).path or "/"
    except Exception:
        return "/"


def _post_form(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _get_json(url, bearer):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


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


def oauth_start(request, provider):
    cfg = _provider(provider)
    if not cfg:
        return JsonResponse({"error": "unknown_provider"}, status=404)
    client_id = os.environ.get(cfg["client_id_env"])
    if not client_id:
        return JsonResponse({"error": "oauth_unconfigured"}, status=503)

    state = signing.dumps(
        {"next": request.GET.get("next", "/"), "link": request.GET.get("link", "") or "", "p": provider},
        salt=STATE_SALT,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(request, provider),
        "response_type": "code",
        "scope": cfg["scope"],
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
        "include_granted_scopes": "true",
    }
    return HttpResponseRedirect(cfg["auth_url"] + "?" + urllib.parse.urlencode(params))


def oauth_callback(request, provider):
    cfg = _provider(provider)
    if not cfg:
        return JsonResponse({"error": "unknown_provider"}, status=404)
    client_id = os.environ.get(cfg["client_id_env"])
    client_secret = os.environ.get(cfg["client_secret_env"])
    if not (client_id and client_secret):
        return JsonResponse({"error": "oauth_unconfigured"}, status=503)

    if request.GET.get("error"):
        return HttpResponseBadRequest(f"oauth_error: {request.GET.get('error')}")
    code = request.GET.get("code")
    state = request.GET.get("state")
    if not code or not state:
        return HttpResponseBadRequest("missing_code_or_state")
    try:
        payload = signing.loads(state, salt=STATE_SALT, max_age=600)
    except signing.BadSignature:
        return HttpResponseBadRequest("bad_state")

    try:
        token_resp = _post_form(cfg["token_url"], {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": _redirect_uri(request, provider),
            "grant_type": "authorization_code",
        })
        access_token = token_resp.get("access_token")
        if not access_token:
            return HttpResponseBadRequest("token_exchange_failed")
        info = _get_json(cfg["userinfo_url"], access_token)
    except Exception:
        return HttpResponseBadRequest("oauth_provider_error")

    subject = str(info.get("sub") or "")
    if not subject:
        return HttpResponseBadRequest("no_subject")
    email = info.get("email") or ""
    pref = info.get("given_name") or (email.split("@")[0] if email else "") or info.get("name") or "player"
    link_token = payload.get("link") or ""

    with transaction.atomic():
        existing = (Player.objects.select_for_update()
                    .filter(provider=provider, provider_subject=subject).first())

        guest = None
        if link_token:
            sess = Session.objects.select_related("player").filter(token=link_token).first()
            if sess and not sess.player.retired and not sess.player.provider:
                guest = Player.objects.select_for_update().get(pk=sess.player_id)

        if existing:
            player = existing
            if guest and guest.pk != player.pk:
                # merge guest balance + history into the existing account
                player.tokens += guest.tokens
                player.wins += guest.wins
                player.games_played += guest.games_played
                guest.bets.update(player=player)
                guest.retired = True
                guest.username_lower = f"_r{guest.pk}"[:20]  # free the name, keep uniqueness
                guest.save(update_fields=["retired", "username_lower"])
                player.save(update_fields=["tokens", "wins", "games_played"])
        elif guest:
            # promote the guest account itself (keeps its balance + username)
            guest.provider = provider
            guest.provider_subject = subject
            guest.save(update_fields=["provider", "provider_subject"])
            player = guest
        else:
            username = _unique_username(pref)
            player = Player.objects.create(
                username=username, username_lower=username.lower(),
                tokens=services.START_TOKENS, provider=provider, provider_subject=subject,
            )

        token = secrets.token_urlsafe(32)
        Session.objects.create(token=token, player=player)

    next_url = _safe_next(payload.get("next"))
    fragment = (
        "auth_token=" + urllib.parse.quote(token)
        + "&auth_user=" + urllib.parse.quote(player.username)
    )
    return HttpResponseRedirect(f"{next_url}#{fragment}")