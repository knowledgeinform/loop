"""Uploaded media is served only to signed-in users in the right affiliation.

Every trial's ``raw_data_link`` points at ``/loop/media/...``. Django does not
serve MEDIA_URL with DEBUG=False, so those links 404'd for everyone. The fix
must not become an open door: this data is restricted to the collaboration, and
the AUIDs in the path are content-derived, so even confirming a path exists is
a disclosure. These tests pin that.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from catalog.media_access import _owning_trial, _resolve_within_media_root


class MediaPathResolutionTests(SimpleTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_ordinary_path_resolves(self) -> None:
        with override_settings(MEDIA_ROOT=str(self.root)):
            target = self.root / "xrd" / "M:abc" / "R:def" / "1" / "raw.txt"
            target.parent.mkdir(parents=True)
            target.write_text("data")
            self.assertEqual(
                _resolve_within_media_root("xrd/M:abc/R:def/1/raw.txt"), target.resolve()
            )

    def test_dotdot_traversal_is_refused(self) -> None:
        """../ must not reach /etc/passwd or anything else outside MEDIA_ROOT."""
        with override_settings(MEDIA_ROOT=str(self.root)):
            for attack in (
                "../../../../etc/passwd",
                "xrd/../../etc/passwd",
                "xrd/M:abc/../../../../etc/shadow",
            ):
                self.assertIsNone(
                    _resolve_within_media_root(attack), f"{attack} was not refused"
                )

    def test_symlink_out_of_tree_is_refused(self) -> None:
        """String checks alone would miss this, so resolution happens first."""
        outside = Path(self._tmp.name).parent / "outside-target.txt"
        outside.write_text("secret")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        with override_settings(MEDIA_ROOT=str(self.root)):
            link = self.root / "escape.txt"
            link.symlink_to(outside)
            self.assertIsNone(_resolve_within_media_root("escape.txt"))


class OwningTrialTests(SimpleTestCase):
    def test_unified_layout_yields_recipe_and_trial(self) -> None:
        self.assertEqual(
            _owning_trial("xrd/M:15bebe1be6cd/R:ab99a8065b37/1/raw.txt"),
            ("M:15bebe1be6cd:R:ab99a8065b37", "1"),
        )

    def test_analysis_subdirectory_still_maps_to_its_trial(self) -> None:
        self.assertEqual(
            _owning_trial("xrd/M:abc/R:def/2/analyses/deadbeef/result.json"),
            ("M:abc:R:def", "2"),
        )

    def test_legacy_layout_yields_material_and_trial(self) -> None:
        self.assertEqual(_owning_trial("xrd_data/M:abc/3/raw.txt"), ("M:abc", "3"))

    def test_unrecognised_layouts_are_refused_not_guessed(self) -> None:
        for path in (
            "xrd",
            "xrd/M:abc",
            "xrd/M:abc/R:def",
            "batch_upload_manifests/whatever.json",
            "",
        ):
            self.assertIsNone(_owning_trial(path), f"{path!r} should be refused")


@override_settings(ROOT_URLCONF="loop.urls")
class ProtectedMediaViewTests(SimpleTestCase):
    """The view itself: anonymous users get nothing, and refusals look alike."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        target = self.root / "xrd" / "M:abc" / "R:def" / "1" / "raw.txt"
        target.parent.mkdir(parents=True)
        target.write_text("two-theta intensity\n10.0 42\n")
        self.target = target

    def test_anonymous_request_is_not_served(self) -> None:
        with override_settings(MEDIA_ROOT=str(self.root)):
            resp = self.client.get("/media/xrd/M:abc/R:def/1/raw.txt")
        self.assertIn(resp.status_code, (302, 401, 403, 404))
        self.assertNotIn(b"two-theta", resp.content if resp.status_code == 200 else b"")

    def test_missing_and_forbidden_are_indistinguishable(self) -> None:
        """A 403 would confirm a content-derived AUID exists. Both must be 404."""
        from catalog import media_access

        user = mock.Mock(is_authenticated=True, is_superuser=False)
        with override_settings(MEDIA_ROOT=str(self.root)):
            with mock.patch.object(media_access, "_is_visible", return_value=False):
                request = mock.Mock(user=user)
                from django.http import Http404

                with self.assertRaises(Http404):
                    media_access.protected_media.__wrapped__(
                        request, "xrd/M:abc/R:def/1/raw.txt"
                    )
                with self.assertRaises(Http404):
                    media_access.protected_media.__wrapped__(
                        request, "xrd/M:abc/R:def/1/does-not-exist.txt"
                    )

    def test_visible_trial_is_served_with_contents(self) -> None:
        from catalog import media_access

        user = mock.Mock(is_authenticated=True, is_superuser=False)
        with override_settings(MEDIA_ROOT=str(self.root)):
            with mock.patch.object(media_access, "_is_visible", return_value=True):
                request = mock.Mock(user=user)
                resp = media_access.protected_media.__wrapped__(
                    request, "xrd/M:abc/R:def/1/raw.txt"
                )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"two-theta", b"".join(resp.streaming_content))

    def test_non_servable_subdirectory_is_refused(self) -> None:
        """Upload manifests have no per-record visibility, so they are not served."""
        from catalog import media_access
        from django.http import Http404

        manifest = self.root / "batch_upload_manifests" / "m.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("{}")
        user = mock.Mock(is_authenticated=True, is_superuser=True)
        with override_settings(MEDIA_ROOT=str(self.root)):
            with self.assertRaises(Http404):
                media_access.protected_media.__wrapped__(
                    mock.Mock(user=user), "batch_upload_manifests/m.json"
                )
