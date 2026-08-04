import tempfile
from datetime import datetime
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from catalog.upload_archive import add_files, archive_upload


class ArchiveUploadTests(SimpleTestCase):
    def test_returns_folder_and_add_files_backfills(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(RAW_UPLOADS_ROOT=root):
                folder = archive_upload(
                    upload_type="trial",
                    username="bob",
                    timestamp=datetime(2026, 6, 25, 9, 30, 0, 123000),
                    metadata={"trial_id": "1"},
                )
                self.assertIsInstance(folder, str)
                self.assertTrue((Path(root) / folder / "metadata.jsonl").is_file())

                src = Path(root) / "overlay.png"
                src.write_bytes(b"PNGDATA")
                add_files(folder, [str(src)])
                self.assertTrue((Path(root) / folder / "overlay.png").is_file())

    def test_archive_disabled_returns_none(self):
        with override_settings(RAW_UPLOADS_ROOT=""):
            self.assertIsNone(
                archive_upload(
                    upload_type="trial", username="bob",
                    timestamp=datetime(2026, 6, 25, 9, 30, 0), metadata={},
                )
            )

    def test_add_files_preserves_relative_structure_when_relative_to_is_given(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(RAW_UPLOADS_ROOT=root):
                folder = archive_upload(
                    upload_type="trial",
                    username="bob",
                    timestamp=datetime(2026, 6, 25, 9, 30, 0, 123000),
                    metadata={"trial_id": "1"},
                )
                trial_root = Path(root) / "xrd" / "M:test" / "R:test" / "T1"
                analysis_dir = trial_root / "analyses" / "abc123"
                analysis_dir.mkdir(parents=True)
                result_path = analysis_dir / "result.json"
                result_path.write_text("{}", encoding="utf-8")

                add_files(folder, [str(result_path)], relative_to=str(trial_root))

                self.assertTrue(
                    (Path(root) / folder / "analyses" / "abc123" / "result.json").is_file()
                )
