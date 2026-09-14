"""Smoke tests for catalog URLs (superuser bypasses approval gate)."""

from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.template.loader import get_template
from django.test import Client, TestCase, override_settings
from django.urls import reverse

# Manifest static storage breaks tests that render templates with {% static %}.
_TEST_STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": settings.MEDIA_ROOT},
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


class CatalogURLSmokeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            "smokeuser",
            "smoke@example.com",
            "pass",
            is_staff=True,
            is_superuser=True,
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)

    @override_settings(STORAGES=_TEST_STORAGES)
    def test_index(self):
        self.assertEqual(self.client.get("/").status_code, 200)

    @override_settings(STORAGES=_TEST_STORAGES)
    def test_browse_data(self):
        self.assertEqual(self.client.get(reverse("browse_data")).status_code, 200)

    @override_settings(STORAGES=_TEST_STORAGES)
    def test_add_data_renders_with_modal(self):
        # Renders base_generic (Add Data modal) and the individual chooser.
        self.assertEqual(self.client.get(reverse("add_data")).status_code, 200)

    @override_settings(STORAGES=_TEST_STORAGES)
    def test_batch_landing(self):
        self.assertEqual(self.client.get(reverse("batch_landing")).status_code, 200)

    @override_settings(STORAGES=_TEST_STORAGES)
    def test_batch_upload_exp_data(self):
        self.assertEqual(self.client.get(reverse("batch_upload_exp_data")).status_code, 200)

    @override_settings(STORAGES=_TEST_STORAGES)
    def test_batch_upload_lit_data(self):
        self.assertEqual(self.client.get(reverse("batch_upload_lit_data")).status_code, 200)

    @override_settings(STORAGES=_TEST_STORAGES)
    @patch("catalog.views.screen_3d_transition_metal_oxides")
    def test_prediction_workspace_is_fixed_screen_with_no_prompt(self, screen):
        screen.return_value = {
            "rows": [],
            "eligible_count": 0,
            "displayed_count": 0,
            "model_status": {
                "active": None,
                "latest_job": None,
                "reward_count": 0,
                "flag_count": 0,
            },
            "criteria": "Five 3d transition metals plus oxygen.",
        }
        response = self.client.get(reverse("predict"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ranked 3d transition-metal oxides")
        self.assertNotContains(response, 'name="query"')
        self.assertNotContains(response, 'name="datafile"')
        self.assertNotContains(response, 'name="csv_data"')

    @override_settings(STORAGES=_TEST_STORAGES)
    @patch("catalog.views.screen_3d_transition_metal_oxides")
    def test_prediction_workspace_renders_complete_ranking(self, screen):
        first = {
            "rank": 1,
            "material_auid": "M:abc123",
            "composition_display": "(Co0.2Cu0.2Fe0.2Mn0.2Zn0.2)O",
            "metals": ["Co", "Cu", "Fe", "Mn", "Zn"],
            "structure": "Rocksalt",
            "concern_score": 5,
            "efa": 36.5138,
            "efa_source": "DFT",
            "deed": 16.1972,
            "deed_source": "DFT",
            "d2h": 0.1392,
            "d2h_source": "Derived from EFA/DEED",
            "synthesis_route": "Ball Milling → Annealing",
            "synthesis_route_source": "ChemScreen literature prior",
            "synthesis_route_source_url": "",
            "synthesis_temperature": "900.0–1100.0 °C",
            "synthesis_temperature_source": "Published HEO route",
            "synthesis_temperature_source_url": "",
            "dft_status": "DFT",
            "dft_detail": "Observed EFA/DEED/d2h",
            "exp_status": "74% single-phase likelihood",
            "exp_detail": "5-neighbor estimate · unvalidated",
            "exp_source": "",
            "concerns": ["Zn: volatility risk during high-temperature synthesis"],
        }
        second = {
            **first,
            "rank": 2,
            "material_auid": "M:def456",
            "composition_display": "(Co0.2Cu0.2Mn0.2Ni0.2Zn0.2)O",
            "metals": ["Co", "Cu", "Mn", "Ni", "Zn"],
            "concern_score": 6,
            "efa": 35.2378,
        }
        screen.return_value = {
            "rows": [first, second],
            "eligible_count": 2,
            "displayed_count": 2,
            "model_status": {
                "active": None,
                "latest_job": None,
                "reward_count": 0,
                "flag_count": 0,
            },
            "criteria": "Five 3d transition metals plus oxygen.",
        }
        response = self.client.get(reverse("predict"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "(Co0.2Cu0.2Fe0.2Mn0.2Zn0.2)O")
        self.assertContains(response, "(Co0.2Cu0.2Mn0.2Ni0.2Zn0.2)O")
        self.assertContains(response, "Top-ranked candidates")
        self.assertContains(response, "EFA + DEED feedback loop")
        self.assertContains(response, "Top 2 of 2")
        self.assertContains(response, "36.5138")
        self.assertContains(response, "35.2378")
        self.assertContains(response, "Ball Milling")
        self.assertContains(response, "900.0–1100.0 °C")
        self.assertContains(response, "74% single-phase likelihood")
        self.assertContains(response, "DFT / model")
        self.assertContains(response, "EXP outlook")
        self.assertNotContains(response, "Lead candidate")


@override_settings(
    S4E_SHELL_URL="",
    CACHES={"default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "shell-login-tests",
    }},
)
class CatalogLoginPageTests(TestCase):
    """Login route is exempt from the approval gate (anonymous OK)."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    @override_settings(STORAGES=_TEST_STORAGES)
    def test_login_get(self):
        client = Client(enforce_csrf_checks=False)
        response = client.get(reverse("login"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Shared site shell, rendered")

    @override_settings(STORAGES=_TEST_STORAGES)
    def test_login_has_collapsed_contributors(self):
        client = Client(enforce_csrf_checks=False)
        response = client.get(reverse("login"))
        html = response.content.decode()

        self.assertIn('<details class="footer-contributors">', html)
        self.assertNotIn('<details class="footer-contributors" open', html)

        # Names, years and ordering belong to the site. Assert that LOOP uses
        # the entire bundled footer exactly once instead of duplicating them.
        footer = get_template("s4e/partials/footer-loop.html").render({})
        self.assertContains(response, footer, count=1)
        self.assertContains(response, 'aria-label="LOOP contributors"', count=1)
        self.assertGreater(html.count('class="footer-contributor"'), 0)

    @override_settings(STORAGES=_TEST_STORAGES, S4E_SHELL_URL="https://s4e.ai/")
    def test_login_uses_site_managed_contributors(self):
        from catalog.templatetags.s4e_shell import _fallback

        footer = (
            '<footer><details class="footer-contributors"><summary>Contributors</summary>'
            '<ul aria-label="LOOP contributors"><li class="footer-contributor">'
            'Site-managed contributor · 2027</li></ul></details></footer>'
        )
        with patch(
            "catalog.templatetags.s4e_shell._fetch",
            side_effect=lambda name: footer if name == "footer-loop" else _fallback(name),
        ):
            response = self.client.get(reverse("login"))
        self.assertContains(response, footer, count=1)
        self.assertContains(response, 'class="footer-contributor"', count=1)


class CatalogBrowseApprovedGroupTests(TestCase):
    """Non-staff users in the Approved group pass ``ApprovedGateMiddleware``."""

    @override_settings(STORAGES=_TEST_STORAGES)
    def test_browse_data(self):
        User = get_user_model()
        user = User.objects.create_user(
            "approved_browser",
            "approved_browser@example.com",
            "pass",
            is_staff=False,
            is_superuser=False,
        )
        group, _ = Group.objects.get_or_create(name="Approved")
        user.groups.add(group)
        client = Client(enforce_csrf_checks=False)
        client.force_login(user)
        self.assertEqual(client.get(reverse("browse_data")).status_code, 200)
