"""Backfill top-city strike aggregates from raw strikes still in hot storage."""

import datetime as dt

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone

from lightning.city_stats import city_aggregate_rows, upsert_city_aggregate_rows
from lightning.geo import country_for_batch
from lightning.models import CityStrikeAggregate, LightningStrike


class Command(BaseCommand):
    help = "Backfill city strike aggregates from LightningStrike rows still retained."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=72)
        parser.add_argument("--batch", type=int, default=5000)
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing city aggregates before rebuilding from retained raw strikes.",
        )

    def handle(self, *args, **options):
        if options["reset"]:
            deleted, _ = CityStrikeAggregate.objects.all().delete()
            self.stdout.write(f"Deleted {deleted} existing city aggregate rows.")

        cutoff = timezone.now() - dt.timedelta(hours=options["hours"])
        batch_size = options["batch"]
        last_id = 0
        total_strikes = 0
        total_rows = 0

        while True:
            rows = list(
                LightningStrike.objects
                .filter(id__gt=last_id, received_at__gte=cutoff)
                .order_by("id")
                .values("id", "lat", "lon", "timestamp", "quality")[:batch_size]
            )
            if not rows:
                break

            last_id = rows[-1]["id"]
            countries = country_for_batch([(row["lat"], row["lon"]) for row in rows])
            source_rows = []
            for row, country in zip(rows, countries):
                source_rows.append({
                    "lat": row["lat"],
                    "lon": row["lon"],
                    "country": country,
                    "timestamp": row["timestamp"],
                    "quality": row["quality"],
                })

            aggregates = city_aggregate_rows(source_rows)
            upsert_city_aggregate_rows(connection, aggregates)
            total_strikes += len(rows)
            total_rows += len(aggregates)
            self.stdout.write(
                f"processed {total_strikes} strikes; upserted {total_rows} aggregate rows"
            )

        self.stdout.write(self.style.SUCCESS(
            f"Done. Processed {total_strikes} retained strikes."
        ))
