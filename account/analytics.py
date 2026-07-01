import json
import os
import threading
import urllib.request


POSTHOG_TOKEN = os.environ.get("POSTHOG_PROJECT_TOKEN") or os.environ.get("NEXT_PUBLIC_POSTHOG_PROJECT_TOKEN", "")
POSTHOG_HOST = (os.environ.get("POSTHOG_HOST") or os.environ.get("NEXT_PUBLIC_POSTHOG_HOST") or "https://eu.i.posthog.com").rstrip("/")


def distinct_id(player):
    return f"player:{player.id}"


def capture(event, player=None, distinct=None, properties=None):
    """Best-effort server-side analytics; never block game flows on PostHog."""
    if not POSTHOG_TOKEN:
        return

    props = dict(properties or {})
    if player is not None:
        props.update({
            "player_id": player.id,
            "verified": bool(player.provider),
            "country": player.country_code,
            "tokens": player.tokens,
        })
        distinct = distinct or distinct_id(player)
    if not distinct:
        return

    payload = {
        "api_key": POSTHOG_TOKEN,
        "event": event,
        "distinct_id": distinct,
        "properties": props,
    }

    def send():
        try:
            req = urllib.request.Request(
                f"{POSTHOG_HOST}/capture/",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=1.5).close()
        except Exception:
            pass

    threading.Thread(target=send, daemon=True).start()
