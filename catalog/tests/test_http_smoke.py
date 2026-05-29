"""Smoke tests for catalog URLs (superuser bypasses approval gate)."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
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


class CatalogLoginPageTests(TestCase):
    """Login route is exempt from the approval gate (anonymous OK)."""

    @override_settings(STORAGES=_TEST_STORAGES)
    def test_login_get(self):
        client = Client(enforce_csrf_checks=False)
        self.assertEqual(client.get(reverse("login")).status_code, 200)


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
