"""DEV_LOCKDOWN removes /admin/ and the signup flow from the URL conf.

The dev deployment runs against a clone of production, real superuser password
hashes included, so those routes must be absent there rather than merely
permission-gated -- a 404 has no form to attack and no username oracle.

loop/urls.py reads the flag at import time, so each test reloads that module
under the setting it wants and puts it back afterwards; override_settings alone
cannot change a URL conf that was already built.
"""

import importlib
from contextlib import contextmanager

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import NoReverseMatch, clear_url_caches, reverse

import loop.urls

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


@contextmanager
def urlconf_with_lockdown(enabled):
    """Rebuild loop.urls with DEV_LOCKDOWN set, then restore the real one."""
    try:
        with override_settings(DEV_LOCKDOWN=enabled, STORAGES=_TEST_STORAGES):
            importlib.reload(loop.urls)
            clear_url_caches()
            yield
    finally:
        # Outside the override, so this rebuilds from the process's real setting.
        importlib.reload(loop.urls)
        clear_url_caches()


class DevLockdownOnTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_admin_index_is_absent(self):
        with urlconf_with_lockdown(True):
            self.assertEqual(self.client.get("/admin/").status_code, 404)

    def test_admin_login_form_is_absent(self):
        # The index alone would 302 to here even when gated, so this is the one
        # that proves there is no password field left standing.
        with urlconf_with_lockdown(True):
            self.assertEqual(self.client.get("/admin/login/").status_code, 404)

    def test_admin_is_absent_for_a_logged_in_superuser(self):
        User = get_user_model()
        User.objects.create_superuser("lockdown-admin", "a@example.com", "pass")
        self.client.login(username="lockdown-admin", password="pass")
        with urlconf_with_lockdown(True):
            self.assertEqual(self.client.get("/admin/").status_code, 404)

    def test_signup_is_absent(self):
        with urlconf_with_lockdown(True):
            self.assertEqual(self.client.get("/accounts/signup/").status_code, 404)

    def test_rest_of_signup_flow_is_absent(self):
        with urlconf_with_lockdown(True):
            for url in (
                "/accounts/signup/pending/",
                "/accounts/signup/complete/",
                "/accounts/activate/MQ/abc-123/",
            ):
                with self.subTest(url=url):
                    self.assertEqual(self.client.get(url).status_code, 404)

    def test_route_names_do_not_resolve(self):
        with urlconf_with_lockdown(True):
            for name in ("signup", "admin:index", "admin:auth_user_change"):
                with self.subTest(name=name):
                    with self.assertRaises(NoReverseMatch):
                        reverse(name)

    def test_login_page_still_renders_without_the_signup_link(self):
        # login.html links to signup; if that link is not guarded, taking the
        # route away turns the whole login page into a 500.
        with urlconf_with_lockdown(True):
            response = self.client.get("/accounts/login/")
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "/accounts/signup/")

    def test_ordinary_pages_are_untouched(self):
        with urlconf_with_lockdown(True):
            self.assertEqual(self.client.get("/llms.txt").status_code, 200)
            self.assertEqual(self.client.get("/accounts/password_reset/").status_code, 200)


class DevLockdownOffTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_admin_login_form_is_served(self):
        with urlconf_with_lockdown(False):
            self.assertEqual(self.client.get("/admin/login/").status_code, 200)

    def test_admin_index_redirects_anonymous_to_the_login_form(self):
        with urlconf_with_lockdown(False):
            response = self.client.get("/admin/")
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login/", response["Location"])

    def test_signup_form_is_served(self):
        with urlconf_with_lockdown(False):
            self.assertEqual(self.client.get("/accounts/signup/").status_code, 200)

    def test_signup_flow_pages_are_served(self):
        with urlconf_with_lockdown(False):
            self.assertEqual(self.client.get("/accounts/signup/pending/").status_code, 200)
            self.assertEqual(self.client.get("/accounts/signup/complete/").status_code, 200)

    def test_login_page_still_offers_registration(self):
        with urlconf_with_lockdown(False):
            self.assertContains(self.client.get("/accounts/login/"), "/accounts/signup/")


class DevLockdownDefaultTests(TestCase):
    def test_flag_defaults_off(self):
        # Production and plain local development must be unaffected by all this.
        self.assertFalse(settings.DEV_LOCKDOWN)
