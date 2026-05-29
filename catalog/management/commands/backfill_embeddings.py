"""
Backfill ``ml_embeddings`` documents for materials, recipes, and DFT records.

Iterates the primary collections and upserts one ``MLEmbedding`` per record:

    - scope="material" -> composition_embedding + structure_embedding
    - scope="recipe"   -> synthesis_embedding
    - scope="comp"     -> structure_embedding (per embedded DFT calc)

By default, records that already have a non-empty vector in the target field
are skipped. Pass ``--force`` to re-embed everything (useful after changing
the embedding model).

The first embedding call in a process loads the sentence-transformers model
(~90 MB for all-MiniLM-L6-v2). Subsequent calls are fast.

Example::

    python manage.py backfill_embeddings
    python manage.py backfill_embeddings --scope material --force
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from catalog import embeddings as embeddings_mod
from catalog.documents import Material, MLEmbedding, Recipe


class Command(BaseCommand):
    help = "Compute and upsert sentence-transformer embeddings into ml_embeddings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--scope",
            default="all",
            choices=["all", "material", "recipe", "comp"],
            help="Restrict backfill to a single scope (default: all).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-embed records that already have a vector in the target field.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Optional cap on number of records processed per scope (0 = no cap).",
        )

    def handle(self, *args, **options):
        scope = options["scope"]
        force = bool(options["force"])
        limit = int(options["limit"] or 0)

        try:
            embeddings_mod.get_embedder()
        except embeddings_mod.EmbeddingUnavailable as exc:
            self.stderr.write(self.style.ERROR(f"Embedding model unavailable: {exc}"))
            return

        totals = {"material": 0, "recipe": 0, "comp": 0}
        skipped = {"material": 0, "recipe": 0, "comp": 0}

        if scope in ("all", "material"):
            totals["material"], skipped["material"] = self._backfill_materials(force, limit)
        if scope in ("all", "recipe"):
            totals["recipe"], skipped["recipe"] = self._backfill_recipes(force, limit)
        if scope in ("all", "comp"):
            totals["comp"], skipped["comp"] = self._backfill_comps(force, limit)

        self.stdout.write(
            self.style.SUCCESS(
                "Backfill complete. "
                f"materials={totals['material']} (skipped {skipped['material']}), "
                f"recipes={totals['recipe']} (skipped {skipped['recipe']}), "
                f"comps={totals['comp']} (skipped {skipped['comp']})."
            )
        )
        self.stdout.write(
            "Next step: `python manage.py mongo_admin ensure-vector-indexes` "
            "so Atlas picks up the vector dimensions."
        )

    def _backfill_materials(self, force: bool, limit: int):
        wrote = skipped = 0
        qs = Material.objects.no_cache()
        if limit:
            qs = qs.limit(limit)
        for material in qs:
            existing = MLEmbedding.objects(
                scope="material", material_auid=material.id
            ).first()
            if existing and existing.composition_embedding and not force:
                skipped += 1
                continue
            text = embeddings_mod.material_text(material)
            vector = embeddings_mod.embed_text(text)
            MLEmbedding.objects(scope="material", material_auid=material.id).update_one(
                set__composition_embedding=vector,
                set__structure_embedding=vector,
                set__model_version=embeddings_mod.EMBEDDING_MODEL_VERSION,
                upsert=True,
            )
            wrote += 1
            if wrote % 25 == 0:
                self.stdout.write(f"  materials embedded: {wrote}")
        self.stdout.write(self.style.SUCCESS(f"materials done: {wrote} written, {skipped} skipped"))
        return wrote, skipped

    def _backfill_recipes(self, force: bool, limit: int):
        wrote = skipped = 0
        qs = Recipe.objects.no_cache()
        if limit:
            qs = qs.limit(limit)
        for recipe in qs:
            existing = MLEmbedding.objects(
                scope="recipe", recipe_auid=recipe.id
            ).first()
            if existing and existing.synthesis_embedding and not force:
                skipped += 1
                continue
            text = embeddings_mod.recipe_text(recipe)
            vector = embeddings_mod.embed_text(text)
            MLEmbedding.objects(scope="recipe", recipe_auid=recipe.id).update_one(
                set__material_auid=recipe.material_auid,
                set__synthesis_embedding=vector,
                set__model_version=embeddings_mod.EMBEDDING_MODEL_VERSION,
                upsert=True,
            )
            wrote += 1
            if wrote % 25 == 0:
                self.stdout.write(f"  recipes embedded: {wrote}")
        self.stdout.write(self.style.SUCCESS(f"recipes done: {wrote} written, {skipped} skipped"))
        return wrote, skipped

    def _backfill_comps(self, force: bool, limit: int):
        wrote = skipped = 0
        qs = Material.objects.no_cache()
        processed = 0
        for material in qs:
            dft_list = list(material.dft_calculations or [])
            if not dft_list:
                continue
            for dft in dft_list:
                if not getattr(dft, "comp_auid", None):
                    continue
                existing = MLEmbedding.objects(
                    scope="comp", comp_auid=dft.comp_auid
                ).first()
                if existing and existing.structure_embedding and not force:
                    skipped += 1
                    continue
                text = embeddings_mod.comp_text(material, dft)
                vector = embeddings_mod.embed_text(text)
                MLEmbedding.objects(scope="comp", comp_auid=dft.comp_auid).update_one(
                    set__material_auid=material.id,
                    set__structure_embedding=vector,
                    set__model_version=embeddings_mod.EMBEDDING_MODEL_VERSION,
                    upsert=True,
                )
                wrote += 1
                if wrote % 25 == 0:
                    self.stdout.write(f"  comps embedded: {wrote}")
            processed += 1
            if limit and processed >= limit:
                break
        self.stdout.write(self.style.SUCCESS(f"comps done: {wrote} written, {skipped} skipped"))
        return wrote, skipped
