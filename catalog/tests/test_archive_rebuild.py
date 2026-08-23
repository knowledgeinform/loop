"""The acceptance test: can MongoDB actually be rebuilt from the archive?

An archive nobody has restored from is a backup nobody has tested. These tests
destroy the database and bring it back, twice — once from the current-state
tree and once from the journal alone — and assert the result is identical to
what was there before, down to the timestamps.

They run against a scratch database name so a mistake here can never touch a
real catalog. CI's MongoDB is a shared instance with no per-test isolation.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from catalog.archive import rebuild as rebuild_mod
from catalog.archive import registry, writer
from catalog.canonical import archive_json_bytes
from catalog.documents import (
    DOIMapping,
    EmbeddedDFT,
    EmbeddedLiterature,
    EmbeddedTrial,
    ExpCondition,
    Material,
    Recipe,
    UserPrecursor,
    UserProtocol,
)
from catalog.raw_db import RawFile, record_raw_file
from .mongo_guard import mongo_reachable


def _archive_settings(root: str):
    return override_settings(
        ARCHIVE_ROOT=root,
        ARCHIVE_ENABLED=True,
        ARCHIVE_REQUIRED=True,
        ARCHIVE_FSYNC=False,
        EMBEDDINGS_ON_WRITE=False,
    )


@unittest.skipUnless(mongo_reachable(), "MongoDB not reachable")
class ArchiveRoundTripTests(TestCase):
    # Not Mongo-only any more: `export_auth` archives the Django-side accounts
    # from SQLite, so these tests touch both databases. TestCase (rather than
    # SimpleTestCase) also rolls back the SQL side between tests; the Mongo
    # side has no transactions and is cleaned explicitly in tearDown.
    databases = {"default"}

    def setUp(self):
        from catalog import signals

        signals.connect_document_signals()

        self.suffix = uuid.uuid4().hex[:10]
        self.material_auid = f"M:tstRbd{self.suffix}"
        self.recipe_a = f"{self.material_auid}:R:a{self.suffix}"
        self.recipe_b = f"{self.material_auid}:R:b{self.suffix}"
        self.file_hash = uuid.uuid4().hex + uuid.uuid4().hex
        self.user_id = 4242

        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._cleanup()

    def tearDown(self):
        self._cleanup()
        self._tmp.cleanup()

    def _cleanup(self):
        Recipe.objects(material_auid=self.material_auid).delete()
        Material.objects(id=self.material_auid).delete()
        RawFile.objects(id=self.file_hash).delete()
        UserPrecursor.objects(user_id=self.user_id).delete()
        UserProtocol.objects(user_id=self.user_id).delete()

    # -- fixtures --------------------------------------------------------

    def _build_fixtures(self):
        """A catalog slice covering every structural feature the archive has."""
        Material(
            id=self.material_auid,
            elements={"Zr": 1, "Ti": 1, "O": 4},
            structure_family="fluorite",
            display_name="Test fluorite",
            notes="round-trip fixture",
            default_visibility_affiliations=["S4E"],
            dft_calculations=[
                EmbeddedDFT(
                    comp_auid=f"{self.material_auid}:C:c{self.suffix}",
                    dft_bandgap_ev=2.25,
                    dft_formation_energy_ev=-3.5,
                    # A datetime inside an untyped DictField. Without the
                    # tagged encoding this comes back as a string after a
                    # rebuild, and the snapshot comparison below catches it.
                    dft_metadata={
                        "functional": "PBE",
                        "measured_at": datetime(2026, 2, 14, 8, 5, tzinfo=timezone.utc),
                    },
                    visibility_affiliations=["S4E"],
                ),
            ],
        ).save()

        Recipe(
            id=self.recipe_a,
            material_auid=self.material_auid,
            elements={"Zr": 1, "Ti": 1, "O": 4},
            structure_family="fluorite",
            synthesis_steps=[
                {"step_type": "ball_milling", "milling_time_hours": 12},
                {"step_type": "heat_treatment", "max_temp_c": 1400},
            ],
            trials=[
                EmbeddedTrial(
                    trial_id="1",
                    trial_date=datetime(2026, 3, 1, 9, 30, 0, 123456, tzinfo=timezone.utc),
                    phase_status="single_phase",
                    success=True,
                    exp_condition=ExpCondition(
                        milling_time_hours=12.0,
                        additional_params={
                            "file_hash": self.file_hash,
                            "synthesis_steps": [],
                            # The deepest untyped data in the catalog:
                            # instrument output inside a DictField inside an
                            # embedded document inside a recipe. Recipe children
                            # are assembled separately during rebuild/replay, so
                            # this exercises a decode path the material-level
                            # fixture does not reach.
                            "xrd_metadata": {
                                "scanned_at": datetime(2026, 4, 9, 7, 0, tzinfo=timezone.utc)
                            },
                        },
                    ),
                    file_hash=self.file_hash,
                    experimenter="tester",
                    notes="first",
                    visibility_affiliations=["S4E"],
                ),
                EmbeddedTrial(
                    trial_id="2",
                    trial_date=datetime(2026, 3, 2, tzinfo=timezone.utc),
                    phase_status="multi_phase",
                    exp_condition=ExpCondition(additional_params={}),
                    visibility_affiliations=["APL"],
                ),
            ],
            literature=[
                EmbeddedLiterature(
                    lit_id=f"L:lit{self.suffix}",
                    doi=f"10.1000/test.{self.suffix}",
                    title="A paper",
                    authors=["Ada L.", "Grace H."],
                    year=2025,
                    synthesis_successful=True,
                    visibility_affiliations=["S4E"],
                ),
            ],
            visibility_affiliations=["S4E"],
        ).save()

        # A second recipe with no children, to catch empty-list handling.
        Recipe(
            id=self.recipe_b,
            material_auid=self.material_auid,
            elements={"Zr": 1, "Ti": 1, "O": 4},
            structure_family="fluorite",
            synthesis_steps=[{"step_type": "arc_melting"}],
            visibility_affiliations=["S4E"],
        ).save()

        record_raw_file(
            file_hash=self.file_hash,
            material_auid=self.material_auid,
            recipe_auid=self.recipe_a,
            trial_id="1",
            original_filename="pattern.csv",
            size_bytes=2048,
            uploaded_by="tester",
        )

        UserPrecursor(
            user_id=self.user_id,
            uploaded_by_username="tester",
            name="Zirconia",
            formula="ZrO2",
            cas_number="1314-23-4",
        ).save()
        UserProtocol(
            user_id=self.user_id,
            uploaded_by_username="tester",
            name="Standard mill",
            steps=[{"step_type": "ball_milling", "milling_rpm": 300}],
        ).save()

    def _snapshot(self):
        """Everything the rebuild is supposed to restore, as comparable dicts."""
        return {
            "material": [
                registry.document_payload(m, drop=registry.MATERIAL.drop_fields)
                for m in Material.objects(id=self.material_auid)
            ],
            "recipe": [
                registry.document_payload(r)
                for r in Recipe.objects(material_auid=self.material_auid).order_by("id")
            ],
            "raw_file": [
                registry.document_payload(f) for f in RawFile.objects(id=self.file_hash)
            ],
            "precursor": [
                registry.document_payload(p)
                for p in UserPrecursor.objects(user_id=self.user_id)
            ],
            "protocol": [
                registry.document_payload(p)
                for p in UserProtocol.objects(user_id=self.user_id)
            ],
        }

    def _simulate_database_loss(self):
        """Wipe the fixtures from Mongo the way a lost database would.

        Deliberately raw pymongo, not ``Recipe.objects(...).delete()``. A
        MongoEngine delete is a *user action* — it fires ``pre_delete``, and the
        archive correctly records a tombstone and removes the record, because
        the user meant to delete it. That is the opposite of what we want to
        simulate here: a database that vanished while the archive stayed intact.
        """
        Material._get_collection().delete_many({"_id": self.material_auid})
        Recipe._get_collection().delete_many({"material_auid": self.material_auid})
        RawFile._get_collection().delete_many({"_id": self.file_hash})
        UserPrecursor._get_collection().delete_many({"user_id": self.user_id})
        UserProtocol._get_collection().delete_many({"user_id": self.user_id})

    def _archive_tree(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file() and "journal" not in path.parts
        }

    # -- the round trip --------------------------------------------------

    def test_rebuild_from_records_restores_everything_including_timestamps(self):
        with _archive_settings(str(self.root)):
            self._build_fixtures()
            before = self._snapshot()

            # Sanity: the fixtures actually exercise the structures we care about.
            self.assertEqual(len(before["recipe"]), 2)
            self.assertEqual(len(before["recipe"][0]["trials"]), 2)
            self.assertEqual(len(before["recipe"][0]["literature"]), 1)

            self._simulate_database_loss()
            self.assertIsNone(Material.objects(id=self.material_auid).first())

            # Bring it back from disk. drop=False so this test only restores its
            # own fixtures and leaves any other data in the shared test DB alone.
            rebuild_mod.rebuild(drop=False, regenerate_derived=False)

            after = self._snapshot()

        self.assertEqual(before["material"], after["material"])
        self.assertEqual(before["recipe"], after["recipe"])
        self.assertEqual(before["raw_file"], after["raw_file"])
        self.assertEqual(before["precursor"], after["precursor"])
        self.assertEqual(before["protocol"], after["protocol"])

        self._assert_dict_field_datetimes_restored()
        stored = Material._get_collection().find_one({"_id": self.material_auid})
        self.assertEqual(
            stored["dft_calculations"][0]["dft_metadata"]["measured_at"].replace(
                tzinfo=timezone.utc
            ),
            datetime(2026, 2, 14, 8, 5, tzinfo=timezone.utc),
        )

    def test_export_is_idempotent_and_byte_identical_after_a_rebuild(self):
        """export -> rebuild -> export must converge, or the archive is lossy."""
        with _archive_settings(str(self.root)):
            self._build_fixtures()
            rebuild_mod.export()
            first = self._archive_tree()

            rebuild_mod.rebuild(drop=False, regenerate_derived=False)
            rebuild_mod.export()
            second = self._archive_tree()

        self.assertEqual(sorted(first), sorted(second))
        for name in first:
            self.assertEqual(first[name], second[name], f"{name} changed across rebuild")

    def test_replay_from_the_journal_alone_reproduces_the_same_state(self):
        """If this passes, `records/` is an optimization, not a dependency."""
        with _archive_settings(str(self.root)):
            self._build_fixtures()
            before = self._snapshot()

            self._simulate_database_loss()

            # Deliberately remove the current-state tree so replay cannot
            # accidentally read from it.
            import shutil

            shutil.rmtree(self.root / "records")

            rebuild_mod.replay(drop=False)
            after = self._snapshot()

        self.assertEqual(before["material"], after["material"])
        self.assertEqual(before["recipe"], after["recipe"])
        self.assertEqual(before["raw_file"], after["raw_file"])
        self.assertEqual(before["precursor"], after["precursor"])
        self._assert_dict_field_datetimes_restored()

    def _assert_dict_field_datetimes_restored(self):
        """Datetimes inside untyped DictFields must survive as datetimes.

        Deliberately reads the raw BSON rather than comparing payloads: both
        sides of a payload comparison run through the same encoder, so a real
        datetime and a literal ``{"$date": ...}`` dict compare equal. Only the
        stored type tells them apart.
        """
        stored = Material._get_collection().find_one({"_id": self.material_auid})
        measured_at = stored["dft_calculations"][0]["dft_metadata"]["measured_at"]
        self.assertIsInstance(
            measured_at,
            datetime,
            f"datetime in a DictField came back as {type(measured_at).__name__}",
        )

        recipe = Recipe._get_collection().find_one({"_id": self.recipe_a})
        trial = next(t for t in recipe["trials"] if t["trial_id"] == "1")
        scanned_at = trial["exp_condition"]["additional_params"]["xrd_metadata"]["scanned_at"]
        self.assertIsInstance(
            scanned_at,
            datetime,
            f"datetime in a trial's DictField came back as {type(scanned_at).__name__}",
        )

    def test_replay_honors_deletions(self):
        with _archive_settings(str(self.root)):
            self._build_fixtures()
            recipe = Recipe.objects(id=self.recipe_a).first()
            recipe.trials = [t for t in recipe.trials if t.trial_id != "2"]
            recipe.save()
            # A real user deleting a recipe. This *should* be journaled as a
            # deletion and honored by replay.
            Recipe.objects(id=self.recipe_b).delete()

            self._simulate_database_loss()

            import shutil

            shutil.rmtree(self.root / "records")
            rebuild_mod.replay(drop=False)

            recipes = list(Recipe.objects(material_auid=self.material_auid))

        self.assertEqual([r.id for r in recipes], [self.recipe_a])
        self.assertEqual([t.trial_id for t in recipes[0].trials], ["1"])

    # -- verify ----------------------------------------------------------

    def _mine(self, entries: list[str]) -> list[str]:
        """Filter a verify report down to this test's own fixtures.

        ``verify`` is deliberately global — it compares the whole database
        against the whole archive, which is what an operator wants. But the
        test database is shared with every other module in the suite (LOOP has
        no per-test Mongo isolation), so those fixtures show up as divergence
        that has nothing to do with this test. Asserting on the global report
        would make this test fail depending on what ran before it.
        """
        return [entry for entry in entries if self.suffix in entry]

    def test_verify_is_clean_after_export_and_detects_tampering(self):
        material_path = (
            self.root / "records"
            / registry.material_dir(self.material_auid)
            / "material.json"
        )
        with _archive_settings(str(self.root)):
            self._build_fixtures()
            rebuild_mod.export()

            report = rebuild_mod.verify(["material", "recipe", "trial"])
            self.assertEqual(self._mine(report.missing_on_disk), [])
            self.assertEqual(self._mine(report.missing_in_db), [])
            self.assertEqual(self._mine(report.content_differs), [])

            # Corrupt one record on disk.
            payload = json.loads(material_path.read_text(encoding="utf-8"))
            payload["notes"] = "tampered"
            material_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            report = rebuild_mod.verify(["material"])

        self.assertFalse(report.ok)
        self.assertEqual(
            self._mine(report.content_differs),
            [f"material:{registry.material_dir(self.material_auid)}/material.json"],
        )

    def test_verify_reports_a_document_that_never_reached_the_archive(self):
        with _archive_settings(str(self.root)):
            self._build_fixtures()
            rebuild_mod.export()
            # Simulate a write that bypassed the archive entirely.
            path = (
                self.root / "records"
                / registry.material_dir(self.material_auid)
                / "material.json"
            )
            path.unlink()
            report = rebuild_mod.verify(["material"])

        self.assertEqual(
            self._mine(report.missing_on_disk),
            [f"material:{registry.material_dir(self.material_auid)}/material.json"],
        )

    # -- derived state ---------------------------------------------------

    def test_rebuild_regenerates_doi_mappings_and_latest_trial_date(self):
        doi = f"10.1000/test.{self.suffix}"
        try:
            with _archive_settings(str(self.root)):
                self._build_fixtures()
                DOIMapping.objects(doi=doi).delete()
                Material._get_collection().update_one(
                    {"_id": self.material_auid}, {"$unset": {"latest_trial_date": ""}}
                )

                rebuild_mod.regenerate_derived_state()

                mapping = DOIMapping.objects(doi=doi).first()
                material = Material.objects(id=self.material_auid).first()

            self.assertIsNotNone(mapping)
            self.assertEqual(mapping.material_auids, [self.material_auid])
            self.assertIsNotNone(material.latest_trial_date)
            # The later of the two trial dates.
            self.assertEqual(material.latest_trial_date.day, 2)
        finally:
            DOIMapping.objects(doi=doi).delete()

    # -- command safety --------------------------------------------------

    def test_rebuild_command_requires_confirming_every_database_it_touches(self):
        """LOOP has two MongoDB connections and the guard must cover both.

        `RawFile` lives on the ``raw`` alias. Overriding only ``MONGODB_URI`` to
        point at a scratch database leaves ``MONGODB_RAW_URI`` aimed at
        production, so a rehearsal that looked safe would have dropped and
        repopulated the live ``loop_raw``. Validating just the default
        connection was not enough.
        """
        from mongoengine.connection import get_db

        live = get_db().name
        with _archive_settings(str(self.root)):
            # Naming only the default database is refused, because the run
            # would also modify the raw connection.
            with self.assertRaises(CommandError) as ctx:
                call_command("loop_archive", "rebuild", "--yes", "--target-db", live)
            self.assertIn("raw", str(ctx.exception))

            # A wrong name for the raw database is refused.
            with self.assertRaises(CommandError):
                call_command(
                    "loop_archive", "rebuild", "--yes",
                    "--target-db", live, "--target-raw-db", "definitely_not_this_db",
                )

            # Scoping away from the raw alias needs no raw confirmation.
            call_command(
                "loop_archive", "rebuild", "--yes", "--target-db", live,
                "--kind", "material", "--no-derived", "--no-raw-files", verbosity=0,
            )

    def test_rebuild_command_refuses_without_yes_and_target_db(self):
        with _archive_settings(str(self.root)):
            with self.assertRaises(CommandError):
                call_command("loop_archive", "rebuild")
            with self.assertRaises(CommandError):
                call_command("loop_archive", "rebuild", "--yes")
            # A target that doesn't match the connected database is refused —
            # this is the guard that keeps a mistyped name off production.
            with self.assertRaises(CommandError):
                call_command(
                    "loop_archive", "rebuild", "--yes", "--target-db", "definitely_not_this_db"
                )

    def test_export_blobs_links_raw_files_that_predate_the_archive(self):
        """Blobs are normally written at upload time, so historical trials have
        none. Without this backfill an archive can hold complete metadata for
        years of experiments and not one diffraction pattern to re-analyze."""
        from catalog import xrd_store

        with _archive_settings(str(self.root)):
            self._build_fixtures()

            # Put a raw file where xrd_store expects it, as an upload would
            # have before the archive existed.
            media = Path(tempfile.mkdtemp(dir=self._tmp.name))
            with override_settings(MEDIA_ROOT=str(media)):
                folder = xrd_store.trial_dir(self.recipe_a, "1")
                (folder / "raw.csv").write_bytes(b"Angle,Intensity\n10,100\n20,250\n")

                counts = rebuild_mod.export_blobs()
                blob = self.root / registry.blob_path(self.file_hash, ".csv")
                self.assertTrue(blob.is_file(), f"expected blob at {blob}")
                self.assertEqual(blob.read_bytes(), b"Angle,Intensity\n10,100\n20,250\n")
                self.assertEqual(counts.written, 1)

                # Idempotent: a second run links nothing new.
                again = rebuild_mod.export_blobs()
                self.assertEqual(again.written, 0)
                self.assertEqual(again.unchanged, 1)

    def test_raw_files_survive_a_full_wipe_of_mongo_and_media(self):
        """The half of recovery that is easy to forget.

        Restoring documents gives every trial a file_hash and a raw_data_link,
        but with no bytes under MEDIA_ROOT the detail page has no pattern to
        plot and no analysis can be re-run — the metadata looks perfect and the
        science is gone. This wipes both the database and the media tree and
        asserts the actual diffraction data comes back byte-for-byte.
        """
        import hashlib

        from catalog import xrd_store

        pattern = b"Angle,Intensity\n10.0,100\n20.0,250\n30.0,880\n"
        media = Path(tempfile.mkdtemp(dir=self._tmp.name))

        with _archive_settings(str(self.root)), override_settings(MEDIA_ROOT=str(media)):
            # The trial's file_hash must be the real digest of the bytes: that
            # is the contract `store_raw_file` establishes in production, and
            # `restore_blobs` verifies it before handing a pattern back.
            self.file_hash = hashlib.sha256(pattern).hexdigest()
            self._build_fixtures()
            folder = xrd_store.trial_dir(self.recipe_a, "1")
            (folder / "raw.csv").write_bytes(pattern)

            rebuild_mod.export()
            self.assertTrue(
                (self.root / registry.blob_path(self.file_hash, ".csv")).is_file()
            )

            # Lose everything: the database *and* the media tree.
            self._simulate_database_loss()
            shutil.rmtree(media)
            media.mkdir()
            self.assertIsNone(xrd_store.resolve_raw_path(self.recipe_a, "1"))

            rebuild_mod.rebuild(drop=False, regenerate_derived=False)

            restored = xrd_store.resolve_raw_path(self.recipe_a, "1")
            self.assertIsNotNone(restored, "raw pattern was not restored to MEDIA_ROOT")
            self.assertEqual(Path(restored).read_bytes(), pattern)

    def test_restore_blobs_refuses_a_corrupted_blob(self):
        """The blob's name is its checksum, so corruption is detectable."""
        import hashlib

        from catalog import xrd_store

        pattern = b"Angle,Intensity\n10,100\n"
        media = Path(tempfile.mkdtemp(dir=self._tmp.name))
        with _archive_settings(str(self.root)), override_settings(MEDIA_ROOT=str(media)):
            self.file_hash = hashlib.sha256(pattern).hexdigest()
            self._build_fixtures()
            folder = xrd_store.trial_dir(self.recipe_a, "1")
            (folder / "raw.csv").write_bytes(pattern)
            rebuild_mod.export_blobs()

            blob = self.root / registry.blob_path(self.file_hash, ".csv")
            blob.write_bytes(b"corrupted on the way out")
            shutil.rmtree(media)
            media.mkdir()

            counts = rebuild_mod.restore_blobs()

            self.assertEqual(counts.failed, 1)
            self.assertEqual(counts.written, 0)
            # Better to restore nothing than to hand back a silently wrong pattern.
            self.assertIsNone(xrd_store.resolve_raw_path(self.recipe_a, "1"))

    def test_documents_without_stored_timestamps_project_identically_every_time(self):
        """The archive must record what is stored, not what MongoEngine invents.

        Documents written by raw ``bulk_write`` (``import_chemscreen``,
        ``import_chaos_data``) and by ``upsert_user_affiliations`` have no
        ``created_at``/``updated_at`` key at all. Projecting them through
        ``to_mongo()`` materializes the field's ``default=_utc_now``, producing
        a brand-new timestamp on every call — so the same untouched document
        archived differently each run and the archive could never converge.

        In production this churned 1,453 materials and all 18 affiliations on
        every single export, and `verify` could never come back clean.
        """
        from catalog.documents import UserAffiliation

        auid = f"M:noTs{self.suffix}"
        try:
            Material._get_collection().insert_one({
                "_id": auid,
                "elements": {"Zr": 1},
                "element_symbols": ["Zr"],
                "num_elements": 1,
                "structure_family": "fluorite",
            })
            UserAffiliation._get_collection().insert_one({
                "user_id": self.user_id,
                "username": f"nots{self.suffix}",
                "affiliations": ["S4E"],
            })

            with _archive_settings(str(self.root)):
                for kind in ("material", "user_affiliation"):
                    runs = [
                        {registry.kind(kind).path_of(p): archive_json_bytes(p)
                         for p in rebuild_mod.iter_kind_payloads(kind)}
                        for _ in range(2)
                    ]
                    self.assertEqual(runs[0], runs[1], f"{kind} projection is unstable")

                first = rebuild_mod.export(["material", "user_affiliation"])
                second = rebuild_mod.export(["material", "user_affiliation"])

            # The second export must find nothing to do.
            self.assertEqual(second["material"].written, 0)
            self.assertEqual(second["user_affiliation"].written, 0)
            self.assertGreater(first["material"].written + first["material"].unchanged, 0)

            # And the archived record must not contain keys Mongo does not hold.
            payload = json.loads(
                (self.root / "records" / registry.material_dir(auid)
                 / "material.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("created_at", payload)
            self.assertNotIn("dft_calculations", payload)
        finally:
            Material._get_collection().delete_many({"_id": auid})
            UserAffiliation._get_collection().delete_many({"user_id": self.user_id})

    def test_export_documents_archives_only_the_named_records(self):
        """The repair path for bulk writes that fire no signals.

        `import_chemscreen` bulk-updates ~1.5k materials with raw pymongo on
        every container start. A full export would also fix the drift, but
        walking every material after each boot is not a cost anyone should pay.
        """
        with _archive_settings(str(self.root)):
            self._build_fixtures()
            rebuild_mod.export(["material"])

            # Simulate the bulk write: change Mongo without firing a signal.
            Material._get_collection().update_one(
                {"_id": self.material_auid}, {"$set": {"notes": "bulk-updated"}}
            )
            counts = rebuild_mod.export_documents("material", [self.material_auid])

        self.assertEqual(counts.written, 1)
        payload = json.loads(
            (self.root / "records" / registry.material_dir(self.material_auid)
             / "material.json").read_text(encoding="utf-8")
        )
        self.assertEqual(payload["notes"], "bulk-updated")

    def test_export_reports_two_records_colliding_on_one_file(self):
        """Two documents mapping to one path means one is silently lost.

        User-scoped kinds are keyed by username; a blank or duplicated username
        collapses several records onto a single file, and only the last one
        written survives a rebuild. Silent data loss unless it is reported.
        """
        shared = f"dupe{self.suffix}"
        try:
            with _archive_settings(str(self.root)):
                for index in (1, 2):
                    UserPrecursor(
                        user_id=self.user_id + index,
                        uploaded_by_username=shared,
                        name=f"Reagent {index}",
                    ).save()
                # Force the collision: same username *and* same object id path
                # segment is impossible, so collide on the affiliations file,
                # which is keyed by username alone.
                from catalog.documents import UserAffiliation

                for index in (1, 2):
                    UserAffiliation(
                        user_id=self.user_id + index,
                        username=shared,
                        affiliations=["S4E"],
                    ).save()
                results = rebuild_mod.export(["user_affiliation"])

            self.assertGreaterEqual(results["user_affiliation"].collisions, 1)
        finally:
            from catalog.documents import UserAffiliation

            UserPrecursor.objects(uploaded_by_username=shared).delete()
            UserAffiliation.objects(username=shared).delete()

    def test_export_auth_writes_a_loaddata_fixture_at_0600(self):
        """A restore that returns the catalog but no logins is not a restore."""
        import stat

        from django.contrib.auth.models import User

        User.objects.create_user(username=f"arch{self.suffix}", password="x")
        with _archive_settings(str(self.root)):
            counts = rebuild_mod.export_auth()

        target = self.root / "records" / "auth" / "django-auth.json"
        self.assertTrue(target.is_file())
        self.assertGreaterEqual(counts.written, 1)
        # Contains password hashes, so it must not be world-readable.
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        # Django fixture shape, so recovery is `manage.py loaddata`.
        fixture = json.loads(target.read_text(encoding="utf-8"))
        self.assertTrue(any(row["model"] == "auth.user" for row in fixture))

    def test_status_and_export_commands_run(self):
        with _archive_settings(str(self.root)):
            self._build_fixtures()
            call_command("loop_archive", "export", verbosity=0)
            call_command("loop_archive", "status", verbosity=0)
            report = rebuild_mod.status()

        self.assertTrue(report["enabled"])
        self.assertGreaterEqual(report["records"]["material"], 1)
        self.assertGreaterEqual(report["records"]["trial"], 2)
        self.assertIsNotNone(report["last_event"])
