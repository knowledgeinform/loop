"""Generate and persist missing composition-specific synthesis predictions."""
from django.core.management.base import BaseCommand

from catalog.documents import SynthesisPrediction
from catalog.prediction_table import screen_3d_transition_metal_oxides


class Command(BaseCommand):
    help = "Backfill auditable route/temperature pseudo-labels for the 3d oxide screen."

    def handle(self, *args, **options):
        before = SynthesisPrediction.objects.count()
        result = screen_3d_transition_metal_oxides(limit=10000)
        after = SynthesisPrediction.objects.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Synthesis predictions: {after} stored "
                f"({after - before} new) across {result['eligible_count']} eligible materials."
            )
        )
