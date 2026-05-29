"""
MongoDB admin utilities for LOOP (Material + Recipe nested model).

Subcommands:
  health                    Ping, list collection + index counts for the main
                            and raw databases.
  reindex                   Re-run ``ensure_indexes`` for every document in
                            both aliases.
  audit                     Audit materials/recipes/raw_files for invariant drift.
  drop-legacy               Drop pre-nested collections (compositions,
                            trial_records, literature_records,
                            computational_records, material_annotations,
                            chem_id_legacy_map) and any stale chem_id indexes.
                            Destructive; requires --yes.
  ensure-vector-indexes     Create/refresh Atlas Vector Search indexes for
                            MLEmbedding. Declared Mongo-specific boundary.
  backup-hint / restore-hint / backup-run / restore-run
                            Wrappers around mongodump/mongorestore for the
                            main DB.
  backup-raw / restore-raw  Same, but for the flat ``loop_raw`` backup DB
                            behind the ``raw`` alias.
  recipe-doc-stats          Top recipes by embedded trial+literature counts (16MB document risk proxy).

Notes:
  - ``ensure-vector-indexes`` requires Atlas (or a deployment that has the
    ``$vectorSearch`` stage enabled); locally this will be a no-op and print
    a warning instead of failing.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError
from mongoengine.connection import get_db

from catalog.documents import (
    DOIMapping,
    Material,
    MLEmbedding,
    Recipe,
    UserAffiliation,
    UserPrecursor,
)
from catalog.raw_db import RAW_DB_ALIAS, RawFile


# Documents in the primary database.
MAIN_DOCS = (
    Material,
    Recipe,
    MLEmbedding,
    DOIMapping,
    UserAffiliation,
    UserPrecursor,
)
# Documents in the secondary / raw-file database.
RAW_DOCS = (RawFile,)
ALL_DOCS = MAIN_DOCS + RAW_DOCS

MAIN_COLLECTION_NAMES = (
    "materials",
    "recipes",
    "ml_embeddings",
    "doi_mappings",
    "user_affiliations",
    "user_precursors",
)
RAW_COLLECTION_NAMES = ("raw_files",)

# Collections and indexes that belonged to the pre-nested / chem_id era.
# ``drop-legacy`` removes these so nothing in Mongo references the old schema.
LEGACY_COLLECTIONS = (
    "compositions",
    "chem_id_legacy_map",
    "trial_records",
    "literature_records",
    "computational_records",
    "material_annotations",
)

LEGACY_FIELD_TOKENS = ("chem_id",)


VECTOR_INDEXES = (
    {
        "field": "composition_embedding",
        "index_name": "ml_embedding_composition_vidx",
        "dimensions": None,
    },
    {
        "field": "structure_embedding",
        "index_name": "ml_embedding_structure_vidx",
        "dimensions": None,
    },
    {
        "field": "synthesis_embedding",
        "index_name": "ml_embedding_synthesis_vidx",
        "dimensions": None,
    },
)


class Command(BaseCommand):
    help = (
        "MongoDB admin utilities for LOOP (Material + Recipe nested model). "
        "Supports health, reindex, audit, drop-legacy, ensure-vector-indexes, "
        "recipe-doc-stats, backup/restore for both the main DB and the flat raw-file DB."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "action",
            choices=[
                "health",
                "reindex",
                "audit",
                "drop-legacy",
                "ensure-vector-indexes",
                "recipe-doc-stats",
                "backup-hint",
                "restore-hint",
                "backup-run",
                "restore-run",
                "backup-raw",
                "restore-raw",
            ],
            help="Operation to run",
        )
        parser.add_argument("--yes", action="store_true", help="Required for destructive actions.")
        parser.add_argument(
            "--archive",
            default="",
            help="Archive filename. If omitted for backup actions, uses timestamped default.",
        )
        parser.add_argument(
            "--dimensions",
            type=int,
            default=None,
            help="Override dimensions for ensure-vector-indexes (otherwise inferred from data).",
        )
        parser.add_argument(
            "--similarity",
            default="cosine",
            choices=["cosine", "euclidean", "dotProduct"],
            help="Similarity metric for vector search indexes (default: cosine).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=20,
            help="For recipe-doc-stats: number of largest recipes to list (default 20).",
        )

    def handle(self, *args, **options):
        action = options["action"]
        db = get_db()
        raw_db = get_db(alias=RAW_DB_ALIAS)

        if action == "health":
            return self._health(db, raw_db)
        if action == "reindex":
            return self._reindex()
        if action == "audit":
            return self._audit(db, raw_db)
        if action == "drop-legacy":
            return self._drop_legacy(db, confirmed=options["yes"])
        if action == "ensure-vector-indexes":
            return self._ensure_vector_indexes(
                db,
                dimensions=options["dimensions"],
                similarity=options["similarity"],
            )
        if action == "recipe-doc-stats":
            return self._recipe_doc_stats(db, limit=options["limit"])
        if action == "backup-hint":
            return self._backup_hint(db, archive=options["archive"])
        if action == "restore-hint":
            return self._restore_hint(db, archive=options["archive"], confirmed=options["yes"])
        if action == "backup-run":
            return self._backup_run(db, archive=options["archive"], confirmed=options["yes"])
        if action == "restore-run":
            return self._restore_run(db, archive=options["archive"], confirmed=options["yes"])
        if action == "backup-raw":
            return self._backup_run(raw_db, archive=options["archive"], confirmed=options["yes"])
        if action == "restore-raw":
            return self._restore_run(raw_db, archive=options["archive"], confirmed=options["yes"])

        raise CommandError(f"Unsupported action: {action}")

    def _recipe_doc_stats(self, db, *, limit: int):
        coll = db[Recipe._meta["collection"]]
        limit = max(1, min(int(limit), 500))
        pipeline = [
            {
                "$project": {
                    "_id": 1,
                    "material_auid": 1,
                    "nt": {"$size": {"$ifNull": ["$trials", []]}},
                    "nl": {"$size": {"$ifNull": ["$literature", []]}},
                }
            },
            {"$addFields": {"n_embedded": {"$add": ["$nt", "$nl"]}}},
            {"$sort": {"n_embedded": -1}},
            {"$limit": limit},
        ]
        self.stdout.write(
            "Largest recipes by embedded trial + literature row counts "
            "(proxy for approaching the 16MB BSON limit per document):"
        )
        for row in coll.aggregate(pipeline):
            self.stdout.write(
                f"  {row.get('_id')} material={row.get('material_auid')}  "
                f"trials={row.get('nt', 0)}  literature={row.get('nl', 0)}  "
                f"sum={row.get('n_embedded', 0)}"
            )

    def _health(self, db, raw_db):
        for label, d, names in (
            ("main", db, MAIN_COLLECTION_NAMES),
            ("raw", raw_db, RAW_COLLECTION_NAMES),
        ):
            ping = d.command("ping")
            if ping.get("ok") != 1.0:
                raise CommandError(f"Mongo ping failed on {label} db")
            self.stdout.write(self.style.SUCCESS(f"MongoDB ping successful ({label})"))
            self.stdout.write(f"Database ({label}): {d.name}")
            for coll_name in names:
                try:
                    count = d[coll_name].count_documents({})
                    index_count = len(list(d[coll_name].list_indexes()))
                except Exception as exc:
                    self.stdout.write(self.style.WARNING(f"- {coll_name}: unavailable ({exc})"))
                    continue
                self.stdout.write(f"- {coll_name}: {count} docs, {index_count} indexes")

    def _reindex(self):
        for doc in ALL_DOCS:
            doc.ensure_indexes()
            self.stdout.write(self.style.SUCCESS(f"Ensured indexes for {doc.__name__}"))

    def _audit(self, db, raw_db):
        materials = db[Material._meta["collection"]]
        recipes = db[Recipe._meta["collection"]]

        mat_total = materials.count_documents({})
        mat_missing = materials.count_documents(
            {
                "$or": [
                    {"structure_family": {"$exists": False}},
                    {"structure_family": ""},
                    {"element_symbols": {"$exists": False}},
                    {"element_symbols": {"$size": 0}},
                ]
            }
        )
        self.stdout.write(f"materials: {mat_total} docs, {mat_missing} missing required fields")

        rec_total = recipes.count_documents({})
        rec_missing = recipes.count_documents(
            {
                "$or": [
                    {"material_auid": {"$exists": False}},
                    {"material_auid": ""},
                    {"structure_family": {"$exists": False}},
                    {"structure_family": ""},
                ]
            }
        )
        self.stdout.write(f"recipes: {rec_total} docs, {rec_missing} missing required fields")

        orphan_recipes = recipes.aggregate(
            [
                {
                    "$lookup": {
                        "from": Material._meta["collection"],
                        "localField": "material_auid",
                        "foreignField": "_id",
                        "as": "_m",
                    }
                },
                {"$match": {"_m": {"$size": 0}}},
                {"$count": "n"},
            ]
        )
        orphan_count = next(orphan_recipes, {}).get("n", 0)
        self.stdout.write(f"recipes with no matching material: {orphan_count}")

        trials_with_hash = recipes.aggregate(
            [
                {"$unwind": {"path": "$trials", "preserveNullAndEmptyArrays": False}},
                {
                    "$match": {
                        "$or": [
                            {"trials.file_hash": {"$exists": False}},
                            {"trials.file_hash": ""},
                        ]
                    }
                },
                {"$count": "n"},
            ]
        )
        trials_no_hash = next(trials_with_hash, {}).get("n", 0)
        self.stdout.write(f"embedded trials missing file_hash: {trials_no_hash}")

        lit_no_doi = recipes.aggregate(
            [
                {"$unwind": {"path": "$literature", "preserveNullAndEmptyArrays": False}},
                {
                    "$match": {
                        "$or": [
                            {"literature.doi": {"$exists": False}},
                            {"literature.doi": ""},
                        ]
                    }
                },
                {"$count": "n"},
            ]
        )
        self.stdout.write(
            f"embedded literature missing DOI: {next(lit_no_doi, {}).get('n', 0)}"
        )

        orphan_doi_maps = db["doi_mappings"].count_documents({"material_auids": {"$size": 0}})
        self.stdout.write(f"DOI mappings with empty material_auids: {orphan_doi_maps}")

        raw_total = raw_db[RawFile._meta["collection"]].count_documents({})
        raw_orphans = raw_db[RawFile._meta["collection"]].count_documents(
            {
                "$or": [
                    {"material_auid": {"$exists": False}},
                    {"material_auid": ""},
                ]
            }
        )
        self.stdout.write(f"raw_files: {raw_total} docs, {raw_orphans} missing material_auid")

        legacy_hits = self._scan_legacy(db)
        if legacy_hits:
            self.stdout.write(
                self.style.WARNING(
                    f"Pre-nested artifacts still present: {legacy_hits}. "
                    "Run ``mongo_admin drop-legacy --yes`` to remove them."
                )
            )
        else:
            self.stdout.write("No pre-nested artifacts detected.")

    def _scan_legacy(self, db):
        """Return a summary of pre-nested collections and indexes still in the DB."""
        findings = []
        existing_colls = set(db.list_collection_names())
        for coll_name in LEGACY_COLLECTIONS:
            if coll_name in existing_colls:
                count = db[coll_name].count_documents({})
                findings.append(f"collection {coll_name} ({count} docs)")
        for coll_name in MAIN_COLLECTION_NAMES:
            if coll_name not in existing_colls:
                continue
            for idx in db[coll_name].list_indexes():
                name = idx.get("name", "")
                key = idx.get("key", {})
                if any(tok in name for tok in LEGACY_FIELD_TOKENS) or any(
                    tok in str(k) for tok in LEGACY_FIELD_TOKENS for k in key.keys()
                ):
                    findings.append(f"index {coll_name}.{name}")
        return findings

    def _drop_legacy(self, db, confirmed=False):
        """Drop pre-nested collections and any stale indexes referencing chem_id.

        Safe to run repeatedly; no-op once the database is clean.
        """
        if not confirmed:
            raise CommandError(
                "drop-legacy is destructive. Re-run with --yes to confirm "
                "you want to remove pre-nested collections and stale indexes."
            )

        existing_colls = set(db.list_collection_names())
        for coll_name in LEGACY_COLLECTIONS:
            if coll_name in existing_colls:
                db.drop_collection(coll_name)
                self.stdout.write(self.style.SUCCESS(f"Dropped legacy collection: {coll_name}"))
            else:
                self.stdout.write(f"Legacy collection already absent: {coll_name}")

        for coll_name in MAIN_COLLECTION_NAMES:
            if coll_name not in existing_colls:
                continue
            coll = db[coll_name]
            for idx in list(coll.list_indexes()):
                name = idx.get("name", "")
                key = idx.get("key", {})
                if name == "_id_":
                    continue
                hit = any(tok in name for tok in LEGACY_FIELD_TOKENS) or any(
                    tok in str(k) for tok in LEGACY_FIELD_TOKENS for k in key.keys()
                )
                if hit:
                    try:
                        coll.drop_index(name)
                        self.stdout.write(
                            self.style.SUCCESS(f"Dropped stale index: {coll_name}.{name}")
                        )
                    except Exception as exc:
                        self.stdout.write(
                            self.style.WARNING(
                                f"Could not drop index {coll_name}.{name}: {exc}"
                            )
                        )

        self.stdout.write("Re-applying document indexes...")
        for doc in ALL_DOCS:
            try:
                doc.ensure_indexes()
                self.stdout.write(self.style.SUCCESS(f"Ensured indexes for {doc.__name__}"))
            except Exception as exc:
                self.stdout.write(
                    self.style.WARNING(f"ensure_indexes failed for {doc.__name__}: {exc}")
                )

        self.stdout.write(self.style.SUCCESS("Legacy cleanup complete."))

    def _ensure_vector_indexes(self, db, dimensions, similarity):
        """
        Create or refresh Atlas Vector Search indexes for ``MLEmbedding``.

        This is an explicit, declared Mongo-specific boundary. On a non-Atlas
        deployment the ``createSearchIndexes`` command will fail; we trap that
        and print a warning rather than crash.
        """
        collection_name = MLEmbedding._meta["collection"]

        # `createSearchIndexes` fails with NamespaceNotFound on a fresh install
        # because no one has written to `ml_embeddings` yet (signals only fire
        # on Material/Recipe save). Create an empty collection up-front so the
        # command is idempotent on a brand-new `docker compose down -v` setup
        # without requiring the user to seed data first.
        try:
            if collection_name not in db.list_collection_names():
                db.create_collection(collection_name)
                self.stdout.write(f"Created empty collection: {collection_name}")
        except Exception as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"Could not ensure {collection_name} exists: {exc}"
                )
            )

        coll = db[collection_name]
        existing = []
        try:
            existing = list(coll.aggregate([{"$listSearchIndexes": {}}]))
        except Exception:
            existing = []
        existing_names = {idx.get("name") for idx in existing}

        def _detect_dim(field_name):
            if dimensions:
                return dimensions
            doc = coll.find_one({field_name: {"$exists": True, "$ne": []}})
            if not doc:
                return None
            vec = doc.get(field_name) or []
            return len(vec) if isinstance(vec, list) and vec else None

        for spec in VECTOR_INDEXES:
            field = spec["field"]
            name = spec["index_name"]
            dim = _detect_dim(field)
            if not dim:
                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping {name}: no sample vector for {field} and no --dimensions given."
                    )
                )
                continue
            if name in existing_names:
                self.stdout.write(f"Vector index already exists: {name}")
                continue
            definition = {
                "fields": [
                    {
                        "type": "vector",
                        "path": field,
                        "numDimensions": dim,
                        "similarity": similarity,
                    }
                ]
            }
            try:
                db.command(
                    "createSearchIndexes",
                    MLEmbedding._meta["collection"],
                    indexes=[{"name": name, "type": "vectorSearch", "definition": definition}],
                )
                self.stdout.write(self.style.SUCCESS(f"Created vector index: {name} (dim={dim})"))
            except Exception as exc:
                self.stdout.write(
                    self.style.WARNING(
                        f"Could not create {name}: {exc}. "
                        "This requires MongoDB Atlas with Vector Search enabled."
                    )
                )

    def _safe_uri(self, db):
        uri = db.client.HOST or ""
        if not uri.startswith("mongodb://") and not uri.startswith("mongodb+srv://"):
            return f"mongodb://{uri}"
        return uri

    def _resolve_archive(self, archive, db_name):
        if archive:
            return archive
        stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        return f"{db_name}-{stamp}.archive.gz"

    def _backup_hint(self, db, archive):
        archive = self._resolve_archive(archive, db.name)
        uri = self._safe_uri(db)
        cmd = f'mongodump --uri "{uri}" --db "{db.name}" --gzip --archive="{archive}"'
        self.stdout.write("Backup command (run in shell with mongodump available):")
        self.stdout.write(self.style.SUCCESS(cmd))

    def _restore_hint(self, db, archive, confirmed=False):
        if not confirmed:
            raise CommandError("Restore hint requires --yes to acknowledge destructive intent.")
        uri = self._safe_uri(db)
        cmd = (
            f'mongorestore --uri "{uri}" --nsInclude "{db.name}.*" '
            f'--drop --gzip --archive="{archive}"'
        )
        self.stdout.write("Restore command (destructive: drops matched collections first):")
        self.stdout.write(self.style.WARNING(cmd))

    def _backup_run(self, db, archive, confirmed=False):
        if not confirmed:
            raise CommandError("backup requires --yes.")
        exe = shutil.which("mongodump")
        if not exe:
            raise CommandError("mongodump not found in PATH.")
        archive = self._resolve_archive(archive, db.name)
        uri = self._safe_uri(db)
        cmd = [exe, "--uri", uri, "--db", db.name, "--gzip", f"--archive={archive}"]
        self.stdout.write(f"Executing backup command for DB {db.name}:")
        self.stdout.write(self.style.SUCCESS(" ".join(cmd)))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(f"mongodump failed:\n{result.stderr or result.stdout}")
        self.stdout.write(self.style.SUCCESS(f"Backup complete: {archive}"))

    def _restore_run(self, db, archive, confirmed=False):
        if not confirmed:
            raise CommandError("restore requires --yes.")
        exe = shutil.which("mongorestore")
        if not exe:
            raise CommandError("mongorestore not found in PATH.")
        uri = self._safe_uri(db)
        cmd = [
            exe,
            "--uri",
            uri,
            "--nsInclude",
            f"{db.name}.*",
            "--drop",
            "--gzip",
            f"--archive={archive}",
        ]
        self.stdout.write(self.style.WARNING(f"Executing destructive restore command on {db.name}:"))
        self.stdout.write(self.style.WARNING(" ".join(cmd)))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CommandError(f"mongorestore failed:\n{result.stderr or result.stdout}")
        self.stdout.write(self.style.SUCCESS("Restore complete."))
