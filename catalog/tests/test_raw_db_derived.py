import os
import unittest

from django.test import SimpleTestCase

from catalog.raw_db import RawFile, record_derived_file, record_raw_file


from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class RecordDerivedFileTests(SimpleTestCase):
    databases = set()

    def setUp(self):
        self.h = "deadbeef" * 8
        RawFile.objects(id=self.h).delete()

    def tearDown(self):
        RawFile.objects(id=self.h).delete()

    def test_records_derived_and_dedups_by_kind_variant_and_analysis_id(self):
        record_raw_file(file_hash=self.h, archive_folder="trial-2026_06_25-00:00:00.000-bob")
        record_derived_file(
            file_hash=self.h, kind="overlay", variant="fast",
            stored_path="xrd/M:x/1/fast.png", url="/media/xrd/M:x/1/fast.png",
            size_bytes=10, sha256="a", generated_at="2026-06-25T00:00:00Z",
            analysis_id="analysis-1",
        )
        # Re-record same (kind, variant, analysis_id) -> replaces, not appends.
        record_derived_file(
            file_hash=self.h, kind="overlay", variant="fast",
            stored_path="xrd/M:x/1/fast.png", url="/media/xrd/M:x/1/fast.png",
            size_bytes=20, sha256="b", generated_at="2026-06-25T01:00:00Z",
            analysis_id="analysis-1",
        )
        # A different analysis_id must coexist rather than clobber the first one.
        record_derived_file(
            file_hash=self.h, kind="overlay", variant="fast",
            stored_path="xrd/M:x/1/analyses/analysis-2/fast.png",
            url="/media/xrd/M:x/1/analyses/analysis-2/fast.png",
            size_bytes=30, sha256="d", generated_at="2026-06-25T02:00:00Z",
            analysis_id="analysis-2",
        )
        record_derived_file(
            file_hash=self.h, kind="pattern", variant=None,
            stored_path="xrd/M:x/1/pattern.csv", url="/media/xrd/M:x/1/pattern.csv",
            size_bytes=5, sha256="c", generated_at="2026-06-25T00:00:00Z",
        )
        row = RawFile.objects(id=self.h).first()
        self.assertEqual(row.archive_folder, "trial-2026_06_25-00:00:00.000-bob")
        self.assertEqual(len(row.derived_files), 3)
        overlays = sorted(
            [d for d in row.derived_files if d["kind"] == "overlay"],
            key=lambda item: item.get("analysis_id") or "",
        )
        self.assertEqual(overlays[0]["analysis_id"], "analysis-1")
        self.assertEqual(overlays[0]["size_bytes"], 20)
        self.assertEqual(overlays[0]["sha256"], "b")
        self.assertEqual(overlays[1]["analysis_id"], "analysis-2")
        self.assertEqual(overlays[1]["size_bytes"], 30)
        self.assertEqual(overlays[1]["sha256"], "d")
