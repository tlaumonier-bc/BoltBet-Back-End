"""
Runs the global game loop. ONE instance only (it owns round creation).

Each tick (~1s):
  * if the current round has ended: close it, broadcast its final board, and
    schedule the next round GAME_INTERMISSION_SECONDS later (a buffer between games),
  * if there is no active round and the intermission has elapsed: start the next one,
  * score every live pick (delta-based, so the leaderboard climbs in near-real-time)
    and broadcast the updated board.

Intermission is derived statelessly from the last finished round's end time, so
it matches what /api/game/state/ reports to fresh page loads.

    python manage.py run_game
"""

import datetime as dt
import time

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand
from django.utils import timezone

from game import services
from game.models import GameRound

TICK_SECONDS = 1.0
GROUP = "lightning_group"


class Command(BaseCommand):
    help = "Run the game loop: round lifecycle + intermission + real-time scoring + broadcasts."

    def handle(self, *args, **options):
        layer = get_channel_layer()

        def broadcast(message):
            if layer is not None:
                async_to_sync(layer.group_send)(
                    GROUP, {"type": "broadcast_message", "message": message}
                )

        def round_start_msg(rnd):
            return {
                "type": "round_start",
                "round": rnd.number,
                "endsAt": rnd.ends_at.isoformat(),
                "durationSeconds": rnd.duration_seconds,
            }

        self.stdout.write(self.style.SUCCESS("Game loop started."))

        while True:
            try:
                now = timezone.now()
                rnd = services.get_active_round()

                if rnd is not None and rnd.ends_at <= now:
                    final_board = services.leaderboard_for_round(rnd)
                    services.close_round(rnd, now)
                    next_at = rnd.ends_at + dt.timedelta(seconds=services.INTERMISSION_SECONDS)
                    broadcast({
                        "type": "round_end",
                        "round": rnd.number,
                        "leaderboard": final_board,
                        "nextRoundAt": next_at.isoformat(),
                    })
                    self.stdout.write(f"Round {rnd.number} closed; next at {next_at:%H:%M:%S}.")

                elif rnd is None:
                    last = GameRound.objects.order_by("-number").first()
                    next_at = (
                        last.ends_at + dt.timedelta(seconds=services.INTERMISSION_SECONDS)
                        if last else None
                    )
                    if next_at is None or now >= next_at:
                        new_round = services.start_round(now)
                        broadcast(round_start_msg(new_round))
                        self.stdout.write(f"Round {new_round.number} started.")

                # Delta-based scoring (climbs ~once per tick). Push the board every
                # tick while a round is live so freshly-picked players show at 0
                # promptly and scores stay in sync.
                services.score_picks(now)
                if services.get_active_round() is not None:
                    broadcast({"type": "leaderboard", "leaderboard": services.current_leaderboard()})

            except Exception as exc:  # never let the loop die on a transient error
                self.stderr.write(self.style.ERROR(f"game loop error: {exc}"))

            time.sleep(TICK_SECONDS)
