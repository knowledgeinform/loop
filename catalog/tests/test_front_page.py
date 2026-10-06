"""The LOOP front page on s4e.ai: signed-out visitors to LOOP's landing page
are sent there, and /api/v1/stats/ gives it the catalog's totals.

No MongoDB needed: the totals are replaced by fixed numbers.
"""

from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from catalog.views import FRONT_PAGE_COOKIE

FRONT = "https://s4e.ai/loop"
TOTALS = {
    "composition_count": 12,
    "material_count": 12,
    "recipe_count": 34,
    "literature_count": 5,
    "experiment_count": 67,
    "computational_count": 89,
}
EXTRA = {"materials_with_recipes": 7, "calculations_from_chaos": 80}
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": settings.MEDIA_ROOT},
    },
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=STORAGES, S4E_SHELL_URL="", PROFILE_REQUIRED=False)
@patch("catalog.views.aggregation_mod.catalog_landing_page_totals", return_value=TOTALS)
class FrontPageRedirectTests(TestCase):
    @override_settings(LOOP_FRONT_PAGE_URL=FRONT)
    def test_signed_out_visitor_goes_to_the_front_page(self, _totals):
        response = Client().get(reverse("index"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], FRONT)
        cookie = response.cookies[FRONT_PAGE_COOKIE]
        self.assertEqual(cookie["max-age"], 60)
        self.assertEqual(cookie["path"], reverse("index"))
        # No session is started for a visitor who is sent on.
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)

    @override_settings(LOOP_FRONT_PAGE_URL=FRONT)
    def test_back_within_a_minute_sees_the_landing_page(self, _totals):
        client = Client()
        client.get(reverse("index"))
        response = client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)

    @override_settings(LOOP_FRONT_PAGE_URL=FRONT)
    def test_signed_in_account_stays(self, _totals):
        user = get_user_model().objects.create_user("frontuser", "front@example.com", "pass", is_superuser=True)
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get(reverse("index")).status_code, 200)

    @override_settings(LOOP_FRONT_PAGE_URL="")
    def test_no_setting_no_redirect(self, _totals):
        self.assertEqual(Client().get(reverse("index")).status_code, 200)


@override_settings(PROFILE_REQUIRED=True)
@patch("catalog.views.aggregation_mod.catalog_front_page_counts", return_value=EXTRA)
@patch("catalog.views.aggregation_mod.catalog_landing_page_totals", return_value=TOTALS)
class CatalogCountsTests(TestCase):
    def test_counts_for_anyone(self, _totals, _extra):
        response = Client().get("/api/v1/stats/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            {k: data[k] for k in ("materials", "recipes", "experiments", "literature", "calculations")},
            {"materials": 12, "recipes": 34, "experiments": 67, "literature": 5, "calculations": 89},
        )
        self.assertEqual(data["materials_with_recipes"], 7)
        self.assertEqual(data["calculations_from_chaos"], 80)
        self.assertRegex(data["as_of"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(response["Cache-Control"], "public, max-age=300")

    def test_account_without_profile_gets_the_counts(self, _totals, _extra):
        user = get_user_model().objects.create_user("noprofile", "np@example.com", "pass")
        client = Client()
        client.force_login(user)
        self.assertEqual(client.get("/api/v1/stats/").status_code, 200)

    def test_only_reads(self, _totals, _extra):
        self.assertEqual(Client().post("/api/v1/stats/").status_code, 405)
        self.assertEqual(Client().head("/api/v1/stats/").status_code, 200)
