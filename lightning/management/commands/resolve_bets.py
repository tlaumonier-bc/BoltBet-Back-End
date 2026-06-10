from django.core.management.base import BaseCommand
from django.utils import timezone
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from bets.models import Bet
from lightning.models import LightningStrike


class Command(BaseCommand):
    help = "Resolves pending bets: win if a qualifying strike landed in the window."

    def handle(self, *args, **options):
        now = timezone.now()
        channel_layer = get_channel_layer()
        pending = Bet.objects.filter(status="pending")

        for bet in pending:
            window_end = min(now, bet.expires_at)
            strike = (
                LightningStrike.objects.filter(
                    grid_cell=bet.cell_id,
                    timestamp__gte=bet.placed_at,
                    timestamp__lte=window_end,
                )
                .order_by("timestamp")
                .first()
            )

            if strike:
                bet.status = "won"
                bet.payout = round(bet.amount * bet.multiplier_at_bet)
                bet.winning_strike = strike
            elif bet.expires_at <= now:
                bet.status = "lost"
                bet.payout = 0
            else:
                continue  # still live, leave pending

            bet.resolved_at = now
            bet.save(update_fields=["status", "payout", "winning_strike", "resolved_at"])

            async_to_sync(channel_layer.group_send)(
                "lightning_group",
                {
                    "type": "broadcast_message",
                    "message": {
                        "type": "bet_resolved",
                        "betId": str(bet.id),   # matches String(data.id) on the client
                        "won": bet.status == "won",
                        "payout": bet.payout,
                    },
                },
            )
            self.stdout.write(f"Bet {bet.id} -> {bet.status} ({bet.payout})")