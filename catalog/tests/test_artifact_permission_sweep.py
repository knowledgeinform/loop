"""A finished analysis tree must be readable by the offsite rsync account.

Chmodding at each write site only covers the paths we know about. Twice now an
artifact reached disk as 0600 through a path that was not covered, and the
nightly rsync of /mnt/data/common/LOOP to bellatrix failed outright -- rsync
exits non-zero on a single unreadable file, so one artifact stops the whole
backup. These tests pin the tree sweep that backstops the per-site chmods.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from catalog.canonical import (
    apply_artifact_mode_tree,
    artifact_dir_mode,
    artifact_file_mode,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class ArtifactTreeSweepTests(SimpleTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "analyses" / "abc123"
        (self.root / "nested").mkdir(parents=True)

    def _write(self, relative: str, mode: int) -> Path:
        p = self.root / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"{}")
        os.chmod(p, mode)
        return p

    def test_restrictive_artifacts_become_world_readable(self) -> None:
        files = [self._write(n, 0o600) for n in
                 ("result.json", "candidates.json", "nested/pattern_observed.csv")]
        apply_artifact_mode_tree(self.root)
        for p in files:
            self.assertTrue(_mode(p) & stat.S_IROTH, f"{p} still {_mode(p):04o}")

    def test_unsearchable_directories_are_fixed(self) -> None:
        """A readable file inside an unsearchable directory is still unreachable."""
        self._write("nested/result.json", 0o644)
        os.chmod(self.root / "nested", 0o700)
        apply_artifact_mode_tree(self.root)
        self.assertTrue(_mode(self.root / "nested") & stat.S_IXOTH)

    def test_reports_how_many_paths_changed(self) -> None:
        self._write("a.json", 0o600)
        self._write("b.json", 0o600)
        self.assertGreaterEqual(apply_artifact_mode_tree(self.root), 2)
        # already correct: nothing left to change
        self.assertEqual(apply_artifact_mode_tree(self.root), 0)

    def test_file_contents_are_untouched(self) -> None:
        p = self._write("result.json", 0o600)
        p.write_bytes(b'{"kept": true}')
        os.chmod(p, 0o600)
        apply_artifact_mode_tree(self.root)
        self.assertEqual(p.read_bytes(), b'{"kept": true}')

    def test_missing_root_is_not_an_error(self) -> None:
        """A permissions problem must never fail an analysis that succeeded."""
        self.assertEqual(apply_artifact_mode_tree(self.root / "does-not-exist"), 0)

    def test_single_file_root_is_accepted(self) -> None:
        p = self._write("result.json", 0o600)
        apply_artifact_mode_tree(p)
        self.assertTrue(_mode(p) & stat.S_IROTH)

    @override_settings(FILE_UPLOAD_PERMISSIONS=0o640,
                       FILE_UPLOAD_DIRECTORY_PERMISSIONS=0o750)
    def test_modes_track_project_settings(self) -> None:
        """Sweep and Django storage must agree on what published files look like."""
        self.assertEqual(artifact_file_mode(), 0o640)
        self.assertEqual(artifact_dir_mode(), 0o750)
        p = self._write("result.json", 0o600)
        apply_artifact_mode_tree(self.root)
        self.assertEqual(_mode(p), 0o640)
        self.assertEqual(_mode(self.root / "nested"), 0o750)
