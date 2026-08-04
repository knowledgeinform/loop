import json
import os
import tempfile
from unittest.mock import patch

import numpy as np
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings

from catalog import xrd_store


class StoreRawAndResolveTests(SimpleTestCase):
    def test_store_writes_raw_with_extension_and_hash(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/"):
                upload = SimpleUploadedFile("scan.txt", b"9.0 100\n9.1 110\n")
                stored = xrd_store.store_raw_file("M:abc", "1", upload)
                self.assertTrue(stored.raw_path.endswith("xrd/M:abc/1/raw.txt"))
                self.assertEqual(stored.ext, ".txt")
                self.assertEqual(stored.media_url, "/media/xrd/M:abc/1/raw.txt")
                self.assertEqual(len(stored.sha256), 64)
                self.assertEqual(xrd_store.resolve_raw_path("M:abc", "1"), stored.raw_path)

    def test_resolve_falls_back_to_legacy_path(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/"):
                import os
                legacy_dir = os.path.join(media, "xrd_data", "M:legacy")
                os.makedirs(legacy_dir)
                legacy = os.path.join(legacy_dir, "7.csv")
                with open(legacy, "w") as fh:
                    fh.write("Angle,Intensity\n10,100\n")
                self.assertEqual(xrd_store.resolve_raw_path("M:legacy", "7"), legacy)

    def test_resolve_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as media:
            with override_settings(MEDIA_ROOT=media, MEDIA_URL="/media/"):
                self.assertIsNone(xrd_store.resolve_raw_path("M:none", "1"))


LOOP_CSV = (
    "Angle,Intensity\n"
    "20.0000,50.0\n20.0105,50.0\n20.0211,50.0\n20.0316,50.0\n20.0421,50.0\n"
    "20.0526,50.0\n20.0632,50.0\n20.0737,50.0\n20.0842,50.0\n20.0947,50.0\n"
    "20.1053,50.0\n20.1158,50.0\n20.1263,50.0\n20.1368,50.0\n20.1474,50.0\n"
    "20.1579,50.0\n20.1684,50.0\n20.1789,50.0\n20.1895,50.0\n20.2000,50.0\n"
    "20.2105,50.1\n20.2211,50.8\n20.2316,53.7\n20.2421,64.0\n20.2526,92.4\n"
    "20.2632,152.9\n20.2737,250.1\n20.2842,361.7\n20.2947,439.1\n20.3053,439.1\n"
    "20.3158,361.7\n20.3263,250.1\n20.3368,152.9\n20.3474,92.4\n20.3579,64.0\n"
    "20.3684,53.7\n20.3789,50.8\n20.3895,50.1\n20.4000,50.0\n20.4105,50.0\n"
    "20.4211,50.0\n20.4316,50.0\n20.4421,50.0\n20.4526,50.0\n20.4632,50.0\n"
    "20.4737,50.0\n20.4842,50.0\n20.4947,50.0\n20.5053,50.0\n20.5158,50.0\n"
    "20.5263,50.0\n20.5368,50.0\n20.5474,50.0\n20.5579,50.0\n20.5684,50.0\n"
    "20.5789,50.0\n20.5895,50.0\n20.6000,50.0\n"
)


def _loop_csv_from_arrays(theta, intensity):
    lines = ["Angle,Intensity"]
    lines.extend(f"{angle:.4f},{value:.4f}" for angle, value in zip(theta, intensity))
    return "\n".join(lines) + "\n"


_NOISY_THETA = np.linspace(18.0, 34.0, 121)
NOISY_LOOP_CSV = _loop_csv_from_arrays(
    _NOISY_THETA,
    62.0
    + 6.5 * np.sin(np.linspace(0.0, 7.5, 121))
    + 3.0 * np.cos(np.linspace(0.0, 15.0, 121))
    + 78.0 * np.exp(-0.5 * ((_NOISY_THETA - 23.2) / 0.24) ** 2)
    + 42.0 * np.exp(-0.5 * ((_NOISY_THETA - 29.1) / 0.36) ** 2),
)

PDF_CARD = (
    "2-Theta    d(?)   I(f)  ( h k l)\n"
    " 23.143  3.8400   85.0  ( 0 0 2)\n"
    " 23.643  3.7600  100.0  ( 0 2 0)\n"
)


class GetOrBuildTests(SimpleTestCase):
    def _media(self):
        return override_settings(MEDIA_ROOT=self._dir.name, MEDIA_URL="/media/")

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        p = patch("catalog.xrd_store._register_and_archive")
        p.start()
        self.addCleanup(p.stop)

    def _seed_raw(self, content, name="scan.csv", auid="M:abc", trial="1"):
        with self._media():
            upload = SimpleUploadedFile(name, content.encode())
            return xrd_store.store_raw_file(auid, trial, upload)

    def _assert_peak_contract(self, peaks):
        self.assertIsInstance(peaks, list)
        self.assertTrue(peaks)
        for peak in peaks:
            self.assertIsInstance(peak, dict)
            self.assertIn("two_theta", peak)
            self.assertIn("intensity", peak)
            self.assertIn("area", peak)

    def test_cold_build_writes_all_artifacts(self):
        stored = self._seed_raw(LOOP_CSV)
        with self._media():
            entry = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            self.assertFalse(entry.from_cache)
            self._assert_peak_contract(entry.peaks)
            self.assertTrue(entry.overlay_png_bytes.startswith(b"\x89PNG"))
            folder = os.path.join(self._dir.name, "xrd", "M:abc", "1")
            self.assertTrue(os.path.isfile(os.path.join(folder, "pattern.csv")))
            self.assertTrue(os.path.isfile(os.path.join(folder, "fast.png")))
            self.assertTrue(os.path.isfile(os.path.join(folder, "fast.peaks.json")))
            with open(os.path.join(folder, "fast.peaks.json")) as fh:
                self.assertEqual(json.load(fh), entry.peaks)
            with open(os.path.join(folder, "index.json")) as fh:
                index = json.load(fh)
            self.assertEqual(index["file_hash"], stored.sha256)
            self.assertIn("fast", index["variants"])
            self.assertEqual(index["variants"]["fast"]["n_peaks"], len(entry.peaks))
            self.assertEqual(index["n_points"], 58)

    def test_warm_hit_skips_render(self):
        stored = self._seed_raw(LOOP_CSV)
        with self._media():
            xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            with patch("catalog.xrd_store.peak_finder_fast") as spy:
                entry = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            spy.assert_not_called()
            self.assertTrue(entry.from_cache)
            self._assert_peak_contract(entry.peaks)

    def test_stale_hash_rebuilds(self):
        stored = self._seed_raw(LOOP_CSV)
        with self._media():
            xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            entry = xrd_store.get_or_build("M:abc", "1", "different-hash", variant="fast")
            self.assertFalse(entry.from_cache)

    def test_reflection_card_builds_stick_variant_without_peaks(self):
        stored = self._seed_raw(PDF_CARD, name="card.txt")
        with self._media():
            entry = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            self.assertEqual(entry.variant, "stick")
            self.assertEqual(entry.plot_style, "stick")
            self.assertEqual(entry.peaks, [])
            folder = os.path.join(self._dir.name, "xrd", "M:abc", "1")
            self.assertTrue(os.path.isfile(os.path.join(folder, "stick.png")))
            # Repeat view serves the stick cache rather than rebuilding as 'fast'.
            entry2 = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            self.assertTrue(entry2.from_cache)
            self.assertEqual(entry2.variant, "stick")

    def test_noisy_pattern_fast_path_preserves_peak_contract_and_artifacts(self):
        stored = self._seed_raw(NOISY_LOOP_CSV)
        with self._media():
            entry = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            self.assertEqual(entry.variant, "fast")
            self._assert_peak_contract(entry.peaks)
            self.assertGreaterEqual(len(entry.peaks), 1)
            self.assertAlmostEqual(entry.peaks[0]["two_theta"], 23.2, delta=0.35)

            folder = os.path.join(self._dir.name, "xrd", "M:abc", "1")
            with open(os.path.join(folder, "pattern.csv")) as fh:
                lines = fh.read().strip().splitlines()
            self.assertEqual(lines[0], "Angle,Intensity")
            self.assertEqual(len(lines), 122)

            with open(os.path.join(folder, "fast.peaks.json")) as fh:
                self.assertEqual(json.load(fh), entry.peaks)
            with open(os.path.join(folder, "index.json")) as fh:
                index = json.load(fh)
            self.assertEqual(index["variants"]["fast"]["n_peaks"], len(entry.peaks))

    def test_read_manifest_returns_current_artifact_contract(self):
        stored = self._seed_raw(LOOP_CSV)
        with self._media():
            entry = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            manifest = xrd_store.read_manifest("M:abc", "1", stored.sha256)

        self.assertEqual(manifest["trial_id"], "1")
        self.assertEqual(manifest["file_hash"], stored.sha256)
        self.assertEqual(manifest["n_points"], 58)
        self.assertEqual(manifest["peaks"], entry.peaks)
        self.assertIn("xrd/M:abc/1/raw.csv", manifest["raw_url"])
        self.assertIn("pattern.csv", manifest["pattern_url"])
        self.assertIn("fast", manifest["variants"])
        self.assertIn("fast.png", manifest["variants"]["fast"]["overlay_url"])
        self.assertIn("fast.peaks.json", manifest["variants"]["fast"]["peaks_url"])
        self.assertEqual(manifest["variants"]["fast"]["n_peaks"], len(entry.peaks))

    def test_gsas_variant_writes_current_artifacts_when_peak_finder_succeeds(self):
        stored = self._seed_raw(LOOP_CSV)
        fake_png = (
            "data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j2mQAAAAASUVORK5CYII="
        )
        fake_peaks = [
            {"two_theta": 20.2947, "intensity": 439.1, "area": 37.5},
            {"two_theta": 20.3158, "intensity": 361.7, "area": 28.4},
        ]

        with self._media():
            with patch("catalog.xrd_store.peak_finder", return_value=(fake_peaks, b"gpx", fake_png)) as mocked_peak_finder:
                entry = xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="gsas")

            mocked_peak_finder.assert_called_once()
            self.assertEqual(entry.variant, "gsas")
            self.assertEqual(entry.peaks, fake_peaks)
            self.assertTrue(entry.overlay_png_bytes.startswith(b"\x89PNG"))

            folder = os.path.join(self._dir.name, "xrd", "M:abc", "1")
            self.assertTrue(os.path.isfile(os.path.join(folder, "pattern.csv")))
            self.assertTrue(os.path.isfile(os.path.join(folder, "gsas.png")))
            self.assertTrue(os.path.isfile(os.path.join(folder, "gsas.peaks.json")))
            with open(os.path.join(folder, "gsas.peaks.json")) as fh:
                self.assertEqual(json.load(fh), fake_peaks)
            with open(os.path.join(folder, "index.json")) as fh:
                index = json.load(fh)
            self.assertEqual(index["variants"]["gsas"]["n_peaks"], len(fake_peaks))

    def test_rebuild_after_hash_change_drops_stale_variants(self):
        stored = self._seed_raw(LOOP_CSV)
        with self._media():
            xrd_store.get_or_build("M:abc", "1", stored.sha256, variant="fast")
            folder = os.path.join(self._dir.name, "xrd", "M:abc", "1")
            index_path = os.path.join(folder, "index.json")
            # Simulate a previously-built variant from this (old) hash.
            with open(index_path) as fh:
                data = json.load(fh)
            data["variants"]["gsas"] = {"n_peaks": 5}
            with open(index_path, "w") as fh:
                json.dump(data, fh)
            # Re-upload changes the hash -> rebuild must drop stale 'gsas'.
            xrd_store.get_or_build("M:abc", "1", "different-hash-xyz", variant="fast")
            with open(index_path) as fh:
                index = json.load(fh)
            self.assertEqual(index["file_hash"], "different-hash-xyz")
            self.assertEqual(list(index["variants"].keys()), ["fast"])


import unittest


from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class BuildSideEffectsTests(SimpleTestCase):
    databases = set()

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._arch = tempfile.TemporaryDirectory()
        self.addCleanup(self._arch.cleanup)
        from catalog.raw_db import RawFile
        self.RawFile = RawFile

    def test_build_registers_derived_and_backfills_archive(self):
        from catalog.raw_db import record_raw_file
        h = "feed" * 16  # 64 hex chars
        self.RawFile.objects(id=h).delete()
        self.addCleanup(lambda: self.RawFile.objects(id=h).delete())
        with override_settings(MEDIA_ROOT=self._dir.name, MEDIA_URL="/media/",
                               RAW_UPLOADS_ROOT=self._arch.name):
            from catalog.upload_archive import archive_upload
            from datetime import datetime
            folder = archive_upload(upload_type="trial", username="bob",
                                    timestamp=datetime(2026, 6, 25, 9, 0, 0, 1000),
                                    metadata={"trial_id": "1"})
            record_raw_file(file_hash=h, archive_folder=folder)

            upload = SimpleUploadedFile("scan.csv", LOOP_CSV.encode())
            xrd_store.store_raw_file("M:abc", "1", upload)
            xrd_store.get_or_build("M:abc", "1", h, variant="fast")

            row = self.RawFile.objects(id=h).first()
            kinds = sorted(d["kind"] for d in row.derived_files)
            self.assertEqual(kinds, ["overlay", "pattern", "peaks"])
            self.assertTrue(os.path.isfile(os.path.join(self._arch.name, folder, "pattern.csv")))
            self.assertTrue(os.path.isfile(os.path.join(self._arch.name, folder, "fast.png")))
