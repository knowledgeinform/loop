"""
Backfill Material.latest_trial_date for all existing materials.

Reads max(trial.trial_date) across all recipes per material and writes it
to the materials collection using MongoDB's $max operator (idempotent — only
advances the stored date, never goes backward).

Run once after deploying the latest_trial_date field:

    python manage.py backfill_trial_dates

Safe to run on a live database; uses the $max operator so concurrent Recipe
saves won't race.
"""

from django.core.management.base import BaseCommand
from mongoengine.connection import get_db

from catalog.documents import Material, Recipe


BATCH_SIZE = 1000


class Command(BaseCommand):
    help = "Populate Material.latest_trial_date from existing recipe trial dates"

    def handle(self, *args, **options):
        db = get_db()
        materials_coll = db[Material._meta["collection"]]
        recipes_coll = db[Recipe._meta["collection"]]

        self.stdout.write("Aggregating max trial dates per material...")

        # One aggregation pass over recipes: group by material_auid, find max trial date.
        pipeline = [
            {"$unwind": {"path": "$trials", "preserveNullAndEmptyArrays": False}},
            {"$match": {"trials.trial_date": {"$ne": None}}},
            {
                "$group": {
                    "_id": "$material_auid",
                    "latest_trial_date": {"$max": "$trials.trial_date"},
                }
            },
        ]

        rows = list(recipes_coll.aggregate(pipeline, allowDiskUse=True))
        self.stdout.write(f"Found {len(rows)} materials with trial dates. Writing...")

        updated = 0
        batch = []
        for row in rows:
            material_auid = row["_id"]
            latest = row["latest_trial_date"]
            if not material_auid or not latest:
                continue
            batch.append((material_auid, latest))
            if len(batch) >= BATCH_SIZE:
                self._flush(materials_coll, batch)
                updated += len(batch)
                self.stdout.write(f"  {updated}/{len(rows)} done...")
                batch = []

        if batch:
            self._flush(materials_coll, batch)
            updated += len(batch)

        self.stdout.write(self.style.SUCCESS(f"Done. Updated {updated} materials."))

    def _flush(self, coll, batch):
        from pymongo import UpdateOne
        ops = [
            UpdateOne(
                {"_id": material_auid},
                {"$max": {"latest_trial_date": latest}},
            )
            for material_auid, latest in batch
        ]
        coll.bulk_write(ops, ordered=False)
