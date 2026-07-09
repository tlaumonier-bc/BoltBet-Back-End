"""
Background workers folded into the backend process.

For now the live ingest (start_lightning_stream), the retention purge
(purge_strikes), the per-country trim (trim_country_strikes) and the Up/Down bet
resolver (resolve_strikes_bets) all run as daemon threads INSIDE the backend
instead of as separate jobs.

IMPORTANT: the ingest is a singleton. It is the only writer of the per-minute
rollup counters and the only process broadcasting strikes, so while it runs
in-process the backend MUST run as a single instance (count: 1). Running more
than one backend instance would double-count rollups and duplicate strikes.

Every thread is opt-in via its own env flag (default off), which keeps two
properties:
  * local dev is unaffected unless you explicitly enable a thread, and
  * splitting any task back out into its own service later is a CONFIG change,
    not a code change: flip the flag off here and run the same
    `python manage.py <command>` in a new app that shares the SAME Redis.

Threads are started from asgi.py, i.e. only when the app is actually served by
daphne (not during migrate / collectstatic).
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# purge: delete raw strikes older than PURGE_HOURS, hourly.
PURGE_INTERVAL_S = 3600
PURGE_HOURS = 72
PURGE_START_DELAY_S = 60     # let boot/migrations settle before the first run

# trim: keep only the newest TRIM_KEEP strikes per country, hourly.
TRIM_INTERVAL_S = 3600
TRIM_KEEP = 10000
TRIM_START_DELAY_S = 90      # stagger off the purge thread

# resolver: settle pending Up/Down bets whose game window has closed.
RESOLVER_INTERVAL_S = 2.0

_started = False
_lock = threading.Lock()


def _truthy(name, default=False):
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


def _run_ingest():
    from django.core.management import call_command
    while True:
        try:
            # Blocks forever (its own reconnect loop). If it ever returns or
            # raises, wait briefly and restart so ingest is self-healing.
            call_command("start_lightning_stream")
        except Exception as exc:
            logger.exception("ingest thread crashed, restarting in 5s: %r", exc)
        time.sleep(5)


def _run_purge():
    from django.core.management import call_command
    from django.db import close_old_connections
    time.sleep(PURGE_START_DELAY_S)
    while True:
        try:
            call_command("purge_strikes", hours=PURGE_HOURS)
        except Exception as exc:
            logger.exception("purge thread error: %r", exc)
        finally:
            close_old_connections()
        time.sleep(PURGE_INTERVAL_S)


def _run_trim():
    from django.core.management import call_command
    from django.db import close_old_connections
    time.sleep(TRIM_START_DELAY_S)
    while True:
        try:
            call_command("trim_country_strikes", keep=TRIM_KEEP)
        except Exception as exc:
            logger.exception("trim thread error: %r", exc)
        finally:
            close_old_connections()
        time.sleep(TRIM_INTERVAL_S)


def _run_resolver():
    from django.core.management import call_command
    from django.db import close_old_connections
    while True:
        try:
            # One idempotent settlement pass over pending bets, then sleep. This
            # cooperates safely with the lazy settle-on-poll in account.views:
            # settle_bet() is select_for_update + no-op once settled.
            call_command("resolve_strikes_bets", once=True)
        except Exception as exc:
            logger.exception("resolver thread error: %r", exc)
        finally:
            close_old_connections()
        time.sleep(RESOLVER_INTERVAL_S)


def start_background_threads():
    """Start the enabled background daemon threads exactly once per process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    specs = [
        ("RUN_INGEST_IN_PROCESS", _run_ingest, "lightning-ingest"),
        ("RUN_PURGE_IN_PROCESS", _run_purge, "lightning-purge"),
        ("RUN_TRIM_IN_PROCESS", _run_trim, "lightning-trim"),
        ("RUN_RESOLVER_IN_PROCESS", _run_resolver, "bet-resolver"),
    ]

    started_any = False
    for flag, target, name in specs:
        if _truthy(flag, False):
            threading.Thread(target=target, name=name, daemon=True).start()
            logger.info("Started background thread: %s", name)
            started_any = True

    if not started_any:
        logger.info(
            "No background threads enabled (RUN_INGEST_IN_PROCESS / "
            "RUN_PURGE_IN_PROCESS / RUN_TRIM_IN_PROCESS / "
            "RUN_RESOLVER_IN_PROCESS all off)"
        )
