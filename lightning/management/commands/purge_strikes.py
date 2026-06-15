"""
Retention for the raw strike table. Rollups are kept; only raw rows are deleted.
Run on a schedule (cron / Nomad periodic), e.g. hourly:

    python manage.py purge_strikes --hours 72
"""

import datetime as dt

from django.core.management.base import BaseCommand
from django.utils import timezone

from lightning.models import LightningStrike


class Command(BaseCommand):
    help = "Delete raw strikes older than --hours (default 72). Rollups are untouched."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=72)
        parser.add_argument("--batch", type=int, default=50_000,
                            help="Delete in batches to avoid a single huge transaction.")

    def handle(self, *args, **options):
        cutoff = timezone.now() - dt.timedelta(hours=options["hours"])
        batch = options["batch"]
        total = 0
        while True:
            ids = list(
                LightningStrike.objects.filter(received_at__lt=cutoff)
                .values_list("id", flat=True)[:batch]
            )
            if not ids:
                break
            deleted, _ = LightningStrike.objects.filter(id__in=ids).delete()
            total += deleted
            self.stdout.write(f"  deleted {deleted} (running total {total})")
        self.stdout.write(self.style.SUCCESS(
            f"Done. Removed {total} strikes older than {options['hours']}h."
        ))
