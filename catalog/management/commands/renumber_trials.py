"""Renumber trials from date-based ids to per-recipe sequential ids ("1", "2", …)
and move their files to the recipe-keyed layout. Dry-run by default; pass
``--apply`` to execute (back up MongoDB and MEDIA_ROOT first)."""

from __future__ import annotations

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from catalog import xrd_store
from catalog.documents import Recipe
from catalog.xrd_store import UNIFIED_SUBDIR, LEGACY_SUBDIR, _LEGACY_EXTENSIONS


def _sort_key(indexed_trial):
    """Chronological order; undated trials keep insertion order and sort last."""
    index, trial = indexed_trial
    date = getattr(trial, "trial_date", None)
    return (date is None, date, index)


class Command(BaseCommand):
    help = "Renumber trials to a per-recipe sequential count and move their files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Perform the migration. Without this flag the command is a dry run.",
        )
        parser.add_argument(
            "--material",
            default=None,
            help="Only migrate recipes belonging to this material_auid.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        media_root = Path(settings.MEDIA_ROOT)
        qs = Recipe.objects
        if options["material"]:
            qs = qs(material_auid=options["material"])

        recipes = 0
        renamed = 0
        moved = 0
        collisions = 0

        # Load full documents: partial (`.only`) loads fail validation on save.
        for recipe in qs:
            trials = recipe.trials or []
            if not trials:
                continue
            recipes += 1
            material_auid = recipe.material_auid
            recipe_auid = recipe.id

            ordered = sorted(enumerate(trials), key=_sort_key)
            changed = False
            for new_num, (_, trial) in enumerate(ordered, start=1):
                old_id = trial.trial_id or ""
                new_id = str(new_num)
                if old_id == new_id:
                    continue  # already correctly numbered

                dest = xrd_store.trial_path(recipe_auid, new_id)
                src = self._locate_source(media_root, material_auid, recipe_auid, old_id)
                if src is not None and dest.exists():
                    # Never rewrite the id without its files: they'd cross-wire.
                    collisions += 1
                    self.stderr.write(
                        self.style.WARNING(
                            f"    SKIP {old_id!r} -> {new_id!r}: destination already exists: {dest}"
                        )
                    )
                    continue

                renamed += 1
                changed = True
                self.stdout.write(
                    f"  {recipe_auid}: trial {old_id!r} -> {new_id!r}"
                )
                if src is not None:
                    self.stdout.write(f"    move {src}  ->  {dest}")
                    moved += 1
                    if apply:
                        old_rel = src.relative_to(media_root).as_posix()
                        new_rel = dest.relative_to(media_root).as_posix()
                        if src.is_file():
                            new_rel += f"/raw{src.suffix.lower()}"
                        self._move(src, dest, old_id)
                        link = getattr(trial, "raw_data_link", None) or ""
                        if old_rel in link:
                            trial.raw_data_link = link.replace(old_rel, new_rel)

                if apply:
                    trial.trial_id = new_id

            if changed and apply:
                recipe.save()

        verb = "Applied" if apply else "Planned (dry run)"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb}: {renamed} trials renumbered across {recipes} recipes; "
                f"{moved} file locations moved; {collisions} collisions skipped."
            )
        )
        if not apply and renamed:
            self.stdout.write("Re-run with --apply to perform the migration.")

    def _locate_source(self, media_root, material_auid, recipe_auid, old_id):
        """Return the existing source path (dir or legacy file) for a trial, or None."""
        # Unified layout, old flat material-keyed location (pre-recipe-nesting).
        cand = media_root / UNIFIED_SUBDIR / material_auid / old_id
        if cand.is_dir():
            return cand
        # Unified layout, already recipe-nested but not yet renumbered.
        cand = xrd_store.trial_path(recipe_auid, old_id)
        if cand.is_dir():
            return cand
        # Legacy single-file layout.
        legacy_dir = media_root / LEGACY_SUBDIR / material_auid
        for ext in _LEGACY_EXTENSIONS:
            cand = legacy_dir / f"{old_id}{ext}"
            if cand.is_file():
                return cand
        return None

    def _move(self, src: Path, dest: Path, old_id: str):
        if src.is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        else:
            # Legacy single file -> new folder as ``raw.<ext>``.
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest / f"raw{src.suffix.lower()}"))
