import time

from django.core.management.base import BaseCommand

from account import services
from account.models import StrikeBet


class Command(BaseCommand):
    help = "Settle pending Up/Down bets whose game window has closed (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="One pass then exit (cron mode).")
        parser.add_argument("--interval", type=float, default=2.0)

    def _pass(self):
        settled = 0
        for bet_id in StrikeBet.objects.filter(status="pending").values_list("id", flat=True):
            bet = services.settle_bet(bet_id)  # no-op if window still open
            if bet and bet.status == "settled":
                settled += 1
        return settled

    def handle(self, *args, **opts):
        if opts["once"]:
            self.stdout.write(self.style.SUCCESS(f"settled {self._pass()} bets"))
            return
        self.stdout.write(self.style.SUCCESS("strike-bet resolver started"))
        while True:
            try:
                self._pass()
            except Exception as exc:
                self.stderr.write(self.style.ERROR(f"resolver error: {exc}"))
            time.sleep(opts["interval"])