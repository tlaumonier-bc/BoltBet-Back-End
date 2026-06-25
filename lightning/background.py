"""
Background workers folded into the backend process.

Instead of separate Nomad jobs, the live ingest (start_lightning_stream) and the
retention purge (purge_strikes) run as daemon threads inside the backend.

IMPORTANT: the ingest is a singleton. It is the only writer of the per-minute
rollup counters and the only process broadcasting strikes, so when it runs
in-process the backend MUST run as a single instance (Nomad count: 1). Running
more than one backend instance would double-count rollups and duplicate strikes.

Both threads are opt-in via env flags (default off), so local dev is unaffected
unless you explicitly enable them. They are started from asgi.py, i.e. only when
the app is actually served by daphne (not during migrate / collectstatic).
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

PURGE_INTERVAL_S = 3600   # hourly, matches the old Nomad periodic cron
PURGE_HOURS = 72          # retention window for raw strikes
PURGE_START_DELAY_S = 60  # let boot/migrations settle before the first purge

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


def start_background_threads():
    """Start the enabled background daemon threads exactly once per process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True

    run_ingest = _truthy("RUN_INGEST_IN_PROCESS", False)
    run_purge = _truthy("RUN_PURGE_IN_PROCESS", False)

    if run_ingest:
        threading.Thread(target=_run_ingest, name="lightning-ingest", daemon=True).start()
        logger.info("Started background thread: lightning-ingest")
    if run_purge:
        threading.Thread(target=_run_purge, name="lightning-purge", daemon=True).start()
        logger.info("Started background thread: lightning-purge")

    if not (run_ingest or run_purge):
        logger.info("No background threads enabled (RUN_INGEST_IN_PROCESS / RUN_PURGE_IN_PROCESS off)")