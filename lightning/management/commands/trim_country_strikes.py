from django.core.management.base import BaseCommand
from lightning.models import CountryStrike


class Command(BaseCommand):
    help = "Keep only the newest --keep strikes per country."

    def add_arguments(self, parser):
        parser.add_argument("--keep", type=int, default=10000)

    def handle(self, *args, **opts):
        keep = opts["keep"]
        total = 0
        for cc in CountryStrike.objects.values_list("country", flat=True).distinct():
            keep_ids = list(
                CountryStrike.objects.filter(country=cc)
                .order_by("-received_at").values_list("id", flat=True)[:keep]
            )
            deleted, _ = CountryStrike.objects.filter(country=cc).exclude(id__in=keep_ids).delete()
            total += deleted
        self.stdout.write(self.style.SUCCESS(f"Trimmed {total} rows."))