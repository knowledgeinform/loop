"""
Management command: backfill_spacegroup

Sets spacegroup = "unknown" and element_sites = {} on every EmbeddedTrial,
EmbeddedLiterature, and EmbeddedDFT document that was created before these
fields were added to the schema.

Usage:
    python manage.py backfill_spacegroup          # dry-run (prints counts only)
    python manage.py backfill_spacegroup --write  # apply changes
"""

from django.core.management.base import BaseCommand

from catalog.documents import Material, Recipe


class Command(BaseCommand):
    help = "Backfill spacegroup='unknown' and element_sites={} on legacy embedded docs."

    def add_arguments(self, parser):
        parser.add_argument(
            "--write",
            action="store_true",
            default=False,
            help="Actually write changes; omit for a dry-run.",
        )

    def handle(self, *args, **options):
        write = options["write"]
        mode = "WRITE" if write else "DRY-RUN"
        self.stdout.write(f"[backfill_spacegroup] mode={mode}\n")

        # ── Recipes: trials + literature ──────────────────────────────────
        recipe_count = 0
        trial_count = 0
        lit_count = 0

        for recipe in Recipe.objects.no_cache():
            dirty = False

            for trial in recipe.trials:
                if not trial.spacegroup:
                    trial.spacegroup = "unknown"
                    trial_count += 1
                    dirty = True
                if trial.element_sites is None:
                    trial.element_sites = {}
                    dirty = True

            for lit in recipe.literature:
                if not lit.spacegroup:
                    lit.spacegroup = "unknown"
                    lit_count += 1
                    dirty = True
                if lit.element_sites is None:
                    lit.element_sites = {}
                    dirty = True

            if dirty:
                recipe_count += 1
                if write:
                    recipe.save()

        # ── Materials: dft_calculations ───────────────────────────────────
        material_count = 0
        dft_count = 0

        for material in Material.objects.no_cache():
            dirty = False

            for dft in material.dft_calculations:
                if not dft.spacegroup:
                    dft.spacegroup = "unknown"
                    dft_count += 1
                    dirty = True
                if dft.element_sites is None:
                    dft.element_sites = {}
                    dirty = True

            if dirty:
                material_count += 1
                if write:
                    material.save()

        # ── Summary ───────────────────────────────────────────────────────
        self.stdout.write(
            f"  Recipes to update : {recipe_count}  "
            f"({trial_count} trials, {lit_count} literature)\n"
            f"  Materials to update: {material_count}  "
            f"({dft_count} DFT records)\n"
        )

        if not write:
            self.stdout.write(
                self.style.WARNING(
                    "Dry-run complete — no changes written. "
                    "Re-run with --write to apply.\n"
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("Backfill complete.\n"))
