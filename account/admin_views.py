import os
from datetime import timedelta

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone
from firebase_admin import auth as firebase_auth
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .firebase_auth import _firebase_app
from .models import Player


DEFAULT_ADMIN_EMAILS = "tlaumonier@blockchain.com,andreas@blockchain.com"


def _admin_emails():
    raw = os.environ.get("ADMIN_EMAILS", DEFAULT_ADMIN_EMAILS)
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


def _admin_email_from_request(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None, Response({"error": "missing_token"}, status=401)
    id_token = auth[len("Bearer "):].strip()
    if not id_token:
        return None, Response({"error": "missing_token"}, status=401)

    try:
        decoded = firebase_auth.verify_id_token(id_token, app=_firebase_app())
    except RuntimeError:
        return None, Response({"error": "firebase_unconfigured"}, status=503)
    except Exception:
        return None, Response({"error": "invalid_token"}, status=401)

    email = str(decoded.get("email") or "").strip().lower()
    if not email or email not in _admin_emails():
        return None, Response({"error": "forbidden"}, status=403)
    return email, None


@api_view(["GET"])
def admin_accounts_growth(request):
    admin_email, error = _admin_email_from_request(request)
    if error:
        return error

    try:
        days = int(request.GET.get("days", 180))
    except ValueError:
        days = 180
    days = max(7, min(days, 730))

    since = timezone.now() - timedelta(days=days)
    rows = (
        Player.objects
        .filter(created_at__gte=since, retired=False)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(new_accounts=Count("id"))
        .order_by("day")
    )

    cumulative = Player.objects.filter(created_at__lt=since, retired=False).count()
    series = []
    for row in rows:
        new_accounts = int(row["new_accounts"])
        cumulative += new_accounts
        series.append({
            "date": row["day"].isoformat(),
            "newAccounts": new_accounts,
            "cumulativeAccounts": cumulative,
        })

    base = Player.objects.filter(retired=False)
    return Response({
        "adminEmail": admin_email,
        "days": days,
        "totalAccounts": base.count(),
        "guestAccounts": base.filter(provider="").count(),
        "verifiedAccounts": base.exclude(provider="").count(),
        "series": series,
    })
