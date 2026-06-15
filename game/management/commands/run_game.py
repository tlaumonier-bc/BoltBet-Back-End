"""
Runs the global game loop. ONE instance only (it owns round creation).

Each tick (~1s):
  * ensure there is an active round (create one if missing),
  * if the current round has ended: close it, broadcast its final board, open the next,
  * finalize any picks whose 5s window + grace has passed and broadcast the live board.

Broadcasts reuse the existing 'lightning_group' Channels group and the
LightningConsumer.broadcast_message handler, so the frontend's existing socket
receives game events alongside strikes.

    python manage.py run_game
"""

import time

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.management.base import BaseCommand
from django.utils import timezone

from game import services

TICK_SECONDS = 1.0
GROUP = "lightning_group"


class Command(BaseCommand):
    help = "Run the game loop: round lifecycle + strike scoring + broadcasts."

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

                if rnd is None:
                    rnd = services.start_round(now)
                    broadcast(round_start_msg(rnd))
                    self.stdout.write(f"Round {rnd.number} started.")

                elif rnd.ends_at <= now:
                    final_board = services.leaderboard_for_round(rnd)
                    services.close_round(rnd, now)
                    broadcast({"type": "round_end", "round": rnd.number, "leaderboard": final_board})
                    self.stdout.write(f"Round {rnd.number} closed.")
                    new_round = services.start_round(now)
                    broadcast(round_start_msg(new_round))
                    self.stdout.write(f"Round {new_round.number} started.")

                if services.finalize_due_picks(now):
                    broadcast({"type": "leaderboard", "leaderboard": services.current_leaderboard()})

            except Exception as exc:  # never let the loop die on a transient DB error
                self.stderr.write(self.style.ERROR(f"game loop error: {exc}"))

            time.sleep(TICK_SECONDS)
