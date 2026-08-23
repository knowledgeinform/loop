"""Tests for the archive writer: what lands on disk, and what happens when it can't.

These exercise the writer through real MongoEngine saves so the signal wiring
is covered too — the point of hooking the document layer is that callers don't
have to know the archive exists, and a test that called the writer directly
would not prove that.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from catalog.archive import registry, writer
from catalog.archive.context import archive_context
from catalog.documents import (
    EmbeddedDFT,
    EmbeddedLiterature,
    EmbeddedTrial,
    ExpCondition,
    Material,
    Recipe,
    UserPrecursor,
)
from catalog.raw_db import RawFile, record_raw_file
from .mongo_guard import mongo_reachable


def _archive_settings(root: str):
    """Archive on, fsync off (tests don't need durability), embeddings off."""
    return override_settings(
        ARCHIVE_ROOT=root,
        ARCHIVE_ENABLED=True,
        ARCHIVE_REQUIRED=True,
        ARCHIVE_FSYNC=False,
        EMBEDDINGS_ON_WRITE=False,
    )


@unittest.skipUnless(mongo_reachable(), "MongoDB not reachable")
class ArchiveWriterTests(SimpleTestCase):
    # Mongo only — keep Django's test runner from wrapping these in a SQL
    # transaction it would then try to roll back.
    databases: set = set()

    def setUp(self):
        # Connect the archive hooks the same way the app does at boot.
        from catalog import signals

        signals.connect_document_signals()

        suffix = uuid.uuid4().hex[:10]
        self.material_auid = f"M:tstArc{suffix}"
        self.recipe_auid = f"{self.material_auid}:R:r{suffix}"
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.records = self.root / "records"

    def tearDown(self):
        Recipe.objects(id=self.recipe_auid).delete()
        Material.objects(id=self.material_auid).delete()
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def _make_material(self):
        return Material(
            id=self.material_auid,
            elements={"Zr": 1, "Ti": 1},
            structure_family="rocksalt",
            default_visibility_affiliations=["S4E"],
        )

    def _make_trial(self, trial_id="1", file_hash=None):
        return EmbeddedTrial(
            trial_id=trial_id,
            trial_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
            phase_status="single_phase",
            exp_condition=ExpCondition(additional_params={"synthesis_steps": []}),
            file_hash=file_hash,
            visibility_affiliations=["S4E"],
        )

    def _make_recipe(self, trials=None, literature=None):
        return Recipe(
            id=self.recipe_auid,
            material_auid=self.material_auid,
            elements={"Zr": 1, "Ti": 1},
            structure_family="rocksalt",
            synthesis_steps=[{"step_type": "ball_milling"}],
            trials=trials or [],
            literature=literature or [],
            visibility_affiliations=["S4E"],
        )

    def _read(self, relative: str) -> dict:
        return json.loads((self.records / relative).read_text(encoding="utf-8"))

    def _journal_events(self) -> list[dict]:
        events = []
        for path in sorted((self.root / "journal").glob("*/*/*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    events.append(json.loads(line))
        return events

    # -- record writing --------------------------------------------------

    def test_material_save_writes_json_before_mongo(self):
        with _archive_settings(str(self.root)):
            self._make_material().save()

        path = f"materials/{registry.sanitize(self.material_auid)}/material.json"
        payload = self._read(path)
        self.assertEqual(payload["_id"], self.material_auid)
        self.assertEqual(payload["structure_family"], "rocksalt")
        # Derived field, deliberately excluded so it can't drift.
        self.assertNotIn("latest_trial_date", payload)

    def test_recipe_explodes_trials_and_literature_into_files(self):
        trials = [self._make_trial("1"), self._make_trial("2")]
        literature = [EmbeddedLiterature(lit_id="L:abc123", doi="10.1/xyz")]
        with _archive_settings(str(self.root)):
            self._make_material().save()
            self._make_recipe(trials, literature).save()

        base = f"materials/{registry.sanitize(self.material_auid)}/recipes/R-r{self.recipe_auid.split(':R:r')[1]}"
        recipe = self._read(f"{base}/recipe.json")
        # The bodies live in their own files; recipe.json keeps only the index.
        self.assertEqual(recipe["trial_ids"], ["1", "2"])
        self.assertEqual(recipe["literature_ids"], ["L:abc123"])
        self.assertNotIn("trials", recipe)
        self.assertNotIn("literature", recipe)

        trial = self._read(f"{base}/trials/1/trial.json")
        self.assertEqual(trial["trial_id"], "1")
        # Back-references make each file independently meaningful.
        self.assertEqual(trial["recipe_auid"], self.recipe_auid)
        self.assertEqual(trial["material_auid"], self.material_auid)

        lit = self._read(f"{base}/literature/L-abc123.json")
        self.assertEqual(lit["doi"], "10.1/xyz")

    def test_datetime_inside_an_untyped_dict_field_is_tagged(self):
        """A DictField has no declared inner types, so a bare ISO string there
        would be indistinguishable from a string that merely looks like a date.

        Tagging is confined to untyped containers: declared DateTimeFields stay
        plain readable strings, which is the whole point of not using Extended
        JSON everywhere.
        """
        material = self._make_material()
        material.dft_calculations = [
            EmbeddedDFT(
                comp_auid=f"{self.material_auid}:C:c1",
                dft_metadata={
                    "measured_at": datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc),
                    "looks_like_a_date": "2026-01-01T00:00:00",
                    "count": 3,
                },
            )
        ]
        with _archive_settings(str(self.root)):
            material.save()

        payload = self._read(
            f"materials/{registry.sanitize(self.material_auid)}/material.json"
        )
        meta = payload["dft_calculations"][0]["dft_metadata"]
        self.assertEqual(meta["measured_at"], {"$date": "2026-01-01T12:30:00+00:00"})
        # A genuine string is left alone — the encoding is driven by the
        # declared type, never by sniffing the value's shape.
        self.assertEqual(meta["looks_like_a_date"], "2026-01-01T00:00:00")
        self.assertEqual(meta["count"], 3)
        # Declared fields stay legible.
        self.assertIsInstance(payload["created_at"], str)

    def test_tagged_datetime_decodes_back_to_a_datetime(self):
        material = self._make_material()
        material.dft_calculations = [
            EmbeddedDFT(
                comp_auid=f"{self.material_auid}:C:c1",
                dft_metadata={"measured_at": datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc)},
            )
        ]
        with _archive_settings(str(self.root)):
            material.save()
            payload = self._read(
                f"materials/{registry.sanitize(self.material_auid)}/material.json"
            )

        decoded = registry.decode_tagged(payload)
        self.assertEqual(
            decoded["dft_calculations"][0]["dft_metadata"]["measured_at"],
            datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc),
        )

    def test_embedded_dft_stays_inline_in_material(self):
        material = self._make_material()
        material.dft_calculations = [
            EmbeddedDFT(comp_auid=f"{self.material_auid}:C:c1", dft_bandgap_ev=1.5)
        ]
        with _archive_settings(str(self.root)):
            material.save()

        payload = self._read(
            f"materials/{registry.sanitize(self.material_auid)}/material.json"
        )
        self.assertEqual(len(payload["dft_calculations"]), 1)
        self.assertEqual(payload["dft_calculations"][0]["dft_bandgap_ev"], 1.5)

    # -- the deletion path that fires no delete signal --------------------

    def test_removing_a_trial_from_a_recipe_deletes_its_archive_file(self):
        """`views.delete_trial` filters the list and re-saves the *recipe*.

        No document is deleted, so no pre_delete fires. The recipe writer has to
        notice the trial is gone by reconciling the directory.
        """
        with _archive_settings(str(self.root)):
            self._make_material().save()
            recipe = self._make_recipe([self._make_trial("1"), self._make_trial("2")])
            recipe.save()

            short = self.recipe_auid.split(":R:")[1]
            base = f"materials/{registry.sanitize(self.material_auid)}/recipes/R-{short}"
            self.assertTrue((self.records / base / "trials" / "2" / "trial.json").exists())

            recipe.trials = [t for t in recipe.trials if t.trial_id != "2"]
            recipe.save()

        self.assertTrue((self.records / base / "trials" / "1" / "trial.json").exists())
        self.assertFalse((self.records / base / "trials" / "2").exists())

        deletes = [e for e in self._journal_events() if e["op"] == "delete"]
        self.assertEqual([e["kind"] for e in deletes], ["trial"])
        # The tombstone carries the final body, so the journal alone can
        # reconstruct what was removed.
        self.assertEqual(deletes[0]["body"]["trial_id"], "2")

    def test_deleting_a_recipe_removes_the_whole_subtree(self):
        with _archive_settings(str(self.root)):
            self._make_material().save()
            recipe = self._make_recipe([self._make_trial("1")])
            recipe.save()
            short = self.recipe_auid.split(":R:")[1]
            base = self.records / f"materials/{registry.sanitize(self.material_auid)}/recipes/R-{short}"
            self.assertTrue(base.exists())

            recipe.delete()

        self.assertFalse(base.exists())
        kinds = {e["kind"] for e in self._journal_events() if e["op"] == "delete"}
        self.assertEqual(kinds, {"trial", "recipe"})

    def test_queryset_delete_still_tombstones_each_recipe(self):
        """`views.delete_material` cascades via `Recipe.objects(...).delete()`.

        MongoEngine only loops documents individually when a delete signal has
        receivers; if the hook were attached differently this would silently
        skip the archive.
        """
        with _archive_settings(str(self.root)):
            self._make_material().save()
            self._make_recipe([self._make_trial("1")]).save()

            Recipe.objects(material_auid=self.material_auid).delete()

        deletes = [e for e in self._journal_events() if e["op"] == "delete"]
        self.assertIn("recipe", {e["kind"] for e in deletes})

    # -- journal ---------------------------------------------------------

    def test_journal_records_actor_and_source(self):
        with _archive_settings(str(self.root)):
            with archive_context("pboctor", "web"):
                self._make_material().save()

        events = self._journal_events()
        self.assertTrue(events)
        self.assertEqual(events[0]["actor"], "pboctor")
        self.assertEqual(events[0]["source"], "web")
        self.assertEqual(events[0]["op"], "upsert")

    def test_unchanged_save_writes_nothing_and_journals_nothing(self):
        """`_upsert_material` re-saves an unchanged Material on every upload.

        Without the skip, the journal would fill with no-op entries and stop
        being a useful record of what actually changed.
        """
        with _archive_settings(str(self.root)):
            material = self._make_material()
            material.save()
            first = len(self._journal_events())
            # Re-save the same document. Material.save() restamps updated_at,
            # so re-read from Mongo to get a genuinely unchanged document.
            reloaded = Material.objects(id=self.material_auid).first()
            writer.write_payload(
                "material",
                registry.document_payload(reloaded, drop=registry.MATERIAL.drop_fields),
            )

        self.assertEqual(len(self._journal_events()), first)

    # -- failure mode ----------------------------------------------------

    def test_archive_failure_aborts_the_mongo_write(self):
        """The whole point: if the archive can't record it, it doesn't happen."""
        from catalog.archive import writer as writer_mod

        original = writer_mod._write_bytes

        def explode(path, payload):
            raise OSError("simulated disk failure")

        with _archive_settings(str(self.root)):
            writer_mod._write_bytes = explode
            try:
                with self.assertRaises(writer_mod.ArchiveWriteError):
                    self._make_material().save()
            finally:
                writer_mod._write_bytes = original

        self.assertIsNone(Material.objects(id=self.material_auid).first())

    def test_archive_not_required_lets_the_write_through_and_records_drift(self):
        from catalog.archive import writer as writer_mod

        original = writer_mod._write_bytes
        calls = {"n": 0}

        def explode_once(path, payload):
            # Let the drift marker itself be written, or there'd be no record.
            if "drift.json" in str(path):
                return original(path, payload)
            calls["n"] += 1
            raise OSError("simulated disk failure")

        with override_settings(
            ARCHIVE_ROOT=str(self.root),
            ARCHIVE_ENABLED=True,
            ARCHIVE_REQUIRED=False,
            ARCHIVE_FSYNC=False,
            EMBEDDINGS_ON_WRITE=False,
        ):
            writer_mod._write_bytes = explode_once
            try:
                self._make_material().save()
            finally:
                writer_mod._write_bytes = original

        self.assertIsNotNone(Material.objects(id=self.material_auid).first())
        marker = json.loads((self.root / ".state" / "drift.json").read_text())
        self.assertTrue(marker["events"])

    # -- bypass paths ----------------------------------------------------

    def test_record_raw_file_archives_despite_using_update_one(self):
        """`record_raw_file` upserts via the queryset, so no signal fires."""
        digest = uuid.uuid4().hex + uuid.uuid4().hex
        try:
            with _archive_settings(str(self.root)):
                record_raw_file(
                    file_hash=digest,
                    material_auid=self.material_auid,
                    original_filename="pattern.csv",
                )
            payload = self._read(f"raw-files/{digest[:2]}/{digest[2:4]}/{digest}.json")
            self.assertEqual(payload["original_filename"], "pattern.csv")
        finally:
            RawFile.objects(id=digest).delete()

    def test_objectid_documents_get_a_stable_key_before_insert(self):
        """Auto-id documents have no pk at hook time; the hook assigns one."""
        precursor = UserPrecursor(
            user_id=987654,
            uploaded_by_username="archivetester",
            name="Zirconia",
        )
        try:
            with _archive_settings(str(self.root)):
                precursor.save()
            self.assertIsNotNone(precursor.pk)
            path = self.records / "users" / "archivetester" / "precursors" / f"{precursor.pk}.json"
            self.assertTrue(path.exists(), f"expected {path}")
            # The id the archive recorded must be the id Mongo stored.
            self.assertIsNotNone(UserPrecursor.objects(pk=precursor.pk).first())
        finally:
            UserPrecursor.objects(user_id=987654).delete()

    # -- blobs -----------------------------------------------------------

    def test_write_blob_is_content_addressed_and_idempotent(self):
        with _archive_settings(str(self.root)):
            source = self.root / "upload.csv"
            source.write_bytes(b"Angle,Intensity\n10,100\n")
            digest = "ab" + "0" * 62
            first = writer.write_blob(digest, source, ext=".csv")
            second = writer.write_blob(digest, source, ext=".csv")

        self.assertEqual(first, second)
        self.assertEqual(first, f"blobs/sha256/ab/00/{digest}.csv")
        self.assertTrue((self.root / first).is_file())

    # -- suspension ------------------------------------------------------

    def test_suspended_blocks_writes(self):
        with _archive_settings(str(self.root)):
            with writer.suspended():
                self._make_material().save()
        self.assertFalse((self.records).exists())
