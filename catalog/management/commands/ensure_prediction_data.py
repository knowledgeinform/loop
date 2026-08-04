"""Idempotently ensure the ranked Predictions tab has enough source data."""
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError

from catalog.documents import Material


_3D_TRANSITION_METALS = {"Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn"}


def eligible_material_count() -> int:
    count = 0
    for material in Material.objects(element_symbols__all=["O"], num_elements=6).only(
        "elements"
    ):
        cations = {str(symbol) for symbol in (material.elements or {}) if symbol != "O"}
        if len(cations) == 5 and cations.issubset(_3D_TRANSITION_METALS):
            count += 1
    return count


class Command(BaseCommand):
    help = "Import ChemScreen candidates when the Predictions tab has too few rows."

    def add_arguments(self, parser):
        parser.add_argument("--minimum", type=int, default=20)
        parser.add_argument("--root", default=str(settings.CHEMSCREEN_ROOT))

    def handle(self, *args, **options):
        minimum = max(1, int(options["minimum"]))
        before = eligible_material_count()
        if before >= minimum:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Prediction data ready: {before} eligible materials; import skipped."
                )
            )
            return

        root = Path(options["root"]).expanduser().resolve()
        required = root / "Data" / "comp_pool" / "generated_compositions.json"
        if not required.is_file():
            raise CommandError(
                f"ChemScreen candidate data is missing at {required}. "
                "Initialize the vendor/ChemScreen submodule first."
            )

        self.stdout.write(
            f"Only {before} eligible prediction material(s) found; importing ChemScreen."
        )
        call_command("import_chemscreen", root=str(root))
        call_command("backfill_synthesis_predictions")

        after = eligible_material_count()
        if after < minimum:
            raise CommandError(
                f"ChemScreen import completed but only {after} eligible materials are available; "
                f"at least {minimum} are required."
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Prediction data ready: {after} eligible materials imported."
            )
        )
