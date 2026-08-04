"""Integration tests for LOOP's versioned public API."""

import io
import json
import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from unittest.mock import patch

from catalog.documents import (
    DOIMapping,
    EmbeddedDFT,
    EmbeddedLiterature,
    EmbeddedTrial,
    ExpCondition,
    Material,
    Recipe,
    UserAffiliation,
    UserPrecursor,
    UserProtocol,
    upsert_user_affiliations,
)
from catalog.api.examples import (
    DOCUMENTED_COMPUTATIONAL_PAYLOAD,
    DOCUMENTED_EXPERIMENT_PAYLOAD,
    DOCUMENTED_LITERATURE_PAYLOAD,
    DOCUMENTED_PRECURSOR_PAYLOAD,
    DOCUMENTED_PROTOCOL_PAYLOAD,
    DOCUMENTED_VALIDATION_PAYLOAD,
)
from catalog.models import APIKey


# The throttles count requests in the default cache, which is file-backed and
# outlives the process. A class that walks pages issues hundreds of requests, so
# without a cache of its own it spends the next run's budget too and then fails
# with 429s that have nothing to do with the code under test.
# The keys every problem+json body carries, whichever layer produced it.
# "errors" is the one optional member.
PROBLEM_KEYS = frozenset({"type", "title", "status", "detail", "instance"})

ISOLATED_THROTTLE_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "api-paged-list-tests",
    }
}

TEST_STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}


class ApiFoundationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "foundation-user", "foundation@example.com", "pass", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_health_requires_account_and_is_versioned(self):
        self.assertEqual(Client().get("/api/v1/health/").status_code, 401)

        response = self.client.get("/api/v1/health/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "data": {
                    "status": "ok",
                    "api_version": "v1",
                }
            },
        )

    def test_openapi_and_interactive_docs_require_account(self):
        anonymous = Client()
        for path in (
            "/api/v1/openapi/",
            "/api/v1/openapi.json",
            "/api/v1/docs/",
            "/api/v1/redoc/",
        ):
            self.assertEqual(anonymous.get(path).status_code, 401)

        schema_response = self.client.get(
            "/api/v1/openapi/", HTTP_ACCEPT="application/json"
        )
        browser_schema_response = self.client.get("/api/v1/openapi.json")
        docs_response = self.client.get("/api/v1/docs/")

        self.assertEqual(schema_response.status_code, 200)
        self.assertIn("/api/v1/health/", schema_response.json()["paths"])
        self.assertEqual(browser_schema_response.status_code, 200)
        self.assertTrue(browser_schema_response["Content-Type"].startswith("application/json"))
        self.assertIn("/api/v1/health/", browser_schema_response.json()["paths"])
        self.assertEqual(docs_response.status_code, 200)
        self.assertContains(docs_response, "LOOP API")

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_llm_and_documentation_discovery_are_public(self):
        llms = Client().get("/llms.txt", HTTP_HOST="docs.example.com")
        docs_url = Client().get("/docs.url", HTTP_HOST="docs.example.com")
        markdown = Client().get("/developers/api.md")

        self.assertEqual(llms.status_code, 200)
        self.assertTrue(llms["Content-Type"].startswith("text/plain"))
        self.assertContains(llms, "# LOOP")
        self.assertContains(llms, "http://docs.example.com/developers/")
        self.assertContains(llms, "http://docs.example.com/developers/agent.md")
        self.assertContains(llms, "http://docs.example.com/developers/")
        self.assertContains(llms, "/api/v1/openapi.json")
        self.assertEqual(docs_url.status_code, 200)
        self.assertEqual(docs_url["Content-Type"], "text/uri-list; charset=utf-8")
        self.assertEqual(
            docs_url.content.decode().strip(),
            "http://docs.example.com/developers/",
        )
        self.assertEqual(markdown.status_code, 200)
        self.assertContains(markdown, "# LOOP API v1")

    @override_settings(ALLOWED_HOSTS=["*"])
    def test_public_agent_client_guide_is_safe_and_deployment_aware(self):
        response = Client().get("/developers/agent.md", HTTP_HOST="docs.example.com")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/markdown"))
        self.assertIn("public", response["Cache-Control"])
        self.assertContains(response, "# LOOP Agent Client Guide")
        self.assertContains(response, "http://docs.example.com/api/v1/")
        self.assertContains(response, "LOOP_API_BASE_URL")
        self.assertContains(response, "openapi.json")
        self.assertContains(response, "Never ask a user to paste an API key")
        self.assertNotContains(response, "MongoDB")
        self.assertNotContains(response, "SQLite")
        self.assertNotContains(response, "deployment runbook")


class ApiNotFoundTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "not-found-api", "not-found-api@example.com", "pass", is_staff=True
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_unrouted_api_paths_answer_problem_json_not_html(self):
        for path in ("/api/v1/", "/api/v1/stats/", "/api/v1/experiments/count/"):
            with self.subTest(path=path):
                response = self.client.get(path)

                self.assertEqual(response.status_code, 404)
                self.assertTrue(
                    response["Content-Type"].startswith("application/problem+json"),
                    msg=response["Content-Type"],
                )
                body = response.json()
                self.assertEqual(body["title"], "Not Found")
                self.assertEqual(body["status"], 404)
                self.assertEqual(body["instance"], path)
                self.assertIn(path, body["detail"])
                self.assertEqual(body["errors"]["code"], "unknown_endpoint")

    def test_unrouted_api_path_is_json_for_an_unauthenticated_caller_too(self):
        response = Client().get("/api/v1/stats/")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            response["Content-Type"].startswith("application/problem+json"),
            msg=response["Content-Type"],
        )

    # DEFAULT_RENDERER_CLASSES still includes the browsable API, so a caller
    # sending a browser Accept negotiates HTML. Asserting only on Content-Type
    # cannot catch that: the header is exactly what goes wrong -- DRF rendered
    # an HTML page underneath a problem+json header, so a client branching on
    # the header before parsing still threw. These assert the BODY parses.
    BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

    def test_errors_are_json_even_when_the_caller_asks_for_html(self):
        cases = (
            ("resolver 404", self.client, "get", "/api/v1/stats/", 404),
            ("drf 404", self.client, "get", "/api/v1/materials/count/", 404),
            ("401", Client(), "get", "/api/v1/materials/", 401),
            ("400", self.client, "get", "/api/v1/materials/?limit=abc", 400),
            ("405", self.client, "delete", "/api/v1/materials/", 405),
        )
        for label, client, method, path, expected_status in cases:
            with self.subTest(case=label):
                response = getattr(client, method)(path, HTTP_ACCEPT=self.BROWSER_ACCEPT)

                self.assertEqual(response.status_code, expected_status)
                self.assertTrue(
                    response["Content-Type"].startswith("application/problem+json"),
                    msg=response["Content-Type"],
                )
                self.assertFalse(
                    response.content.lstrip()[:9].lower().startswith(b"<!doctype"),
                    msg=f"HTML body served under a problem+json header: {response.content[:120]!r}",
                )
                body = json.loads(response.content)      # not .json(): that trusts the header
                self.assertEqual(body["status"], expected_status)

    def test_unhandled_exception_under_the_api_is_json_not_an_html_error_page(self):
        # DRF's exception hook only converts APIException subclasses, so an
        # ordinary bug in a view escaped to Django and rendered HTML -- the one
        # status where a client can least afford a parse error on top of the
        # failure it is already handling. handler500 makes it problem+json.
        client = Client(raise_request_exception=False)
        client.force_login(self.user)

        with patch("catalog.api.views._pagination_or_problem", side_effect=RuntimeError("boom")):
            response = client.get("/api/v1/materials/", HTTP_ACCEPT=self.BROWSER_ACCEPT)

        self.assertEqual(response.status_code, 500)
        self.assertTrue(
            response["Content-Type"].startswith("application/problem+json"),
            msg=response["Content-Type"],
        )
        body = json.loads(response.content)
        self.assertEqual(body["status"], 500)
        self.assertEqual(body["errors"]["code"], "internal_error")
        # The exception message must not reach the caller.
        self.assertNotIn("boom", response.content.decode())

    def test_html_site_still_gets_an_html_500(self):
        # The handler is called directly: urls.py holds a reference to the view
        # function itself, so patching a view to raise never reaches the
        # resolver. What matters here is the branch, not how the 500 arose.
        from django.test import RequestFactory

        from catalog.views import custom_server_error

        response = custom_server_error(RequestFactory().get("/browse/"))

        self.assertEqual(response.status_code, 500)
        self.assertTrue(
            response["Content-Type"].startswith("text/html"),
            msg=response["Content-Type"],
        )

    def test_format_api_does_not_get_an_html_error_body_either(self):
        # ?format=api overrides Accept entirely, so it is a separate route to
        # the browsable renderer.
        response = self.client.get("/api/v1/materials/count/?format=api")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            response.content.lstrip()[:9].lower().startswith(b"<!doctype"),
            msg=response.content[:120],
        )
        self.assertEqual(json.loads(response.content)["status"], 404)

    def test_generic_unknown_endpoint_points_at_the_schema(self):
        body = self.client.get("/api/v1/stats/").json()

        self.assertIn("/api/v1/openapi.json", body["detail"])
        self.assertNotIn("endpoint", body["errors"])

    def test_trials_404_names_experiments_as_the_endpoint_that_serves_trials(self):
        response = self.client.get("/api/v1/trials/")
        experiments = self.client.get("/api/v1/experiments/")

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["errors"]["endpoint"], "/api/v1/experiments/")
        self.assertIn("/api/v1/experiments/", body["detail"])
        # The endpoint the error hands back has to be one the caller can follow.
        self.assertEqual(experiments.status_code, 200, msg=experiments.content)

    def test_recipes_404_names_the_paths_that_do_reach_recipes(self):
        response = self.client.get("/api/v1/recipes/")

        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertIn("/api/v1/materials/{material_auid}/recipes/", body["detail"])
        self.assertIn("/api/v1/recipes/{recipe_auid}/", body["detail"])
        self.assertNotIn("endpoint", body["errors"])

    def test_unknown_path_under_a_known_prefix_keeps_its_own_problem_detail(self):
        response = self.client.get("/api/v1/materials/count/")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            response["Content-Type"].startswith("application/problem+json"),
            msg=response["Content-Type"],
        )
        self.assertEqual(response.json()["detail"], "Material was not found.")

    def test_api_path_without_its_trailing_slash_still_redirects(self):
        response = self.client.get("/api/v1/materials")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(response["Location"], "/api/v1/materials/")

    def test_api_root_without_its_trailing_slash_is_json_too(self):
        # Nothing routes /api/v1/ either, so APPEND_SLASH has no redirect to
        # offer here and the bare prefix would otherwise leave through the HTML
        # 404 page.
        response = self.client.get("/api/v1")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            response["Content-Type"].startswith("application/problem+json"),
            msg=response["Content-Type"],
        )
        self.assertEqual(response.json()["errors"]["code"], "unknown_endpoint")

    @override_settings(FORCE_SCRIPT_NAME="/loop")
    def test_unrouted_api_path_is_json_under_a_deployment_subpath(self):
        # Production sets DJANGO_SUBPATH, which puts the prefix in request.path
        # but not in path_info; matching on path would silently stop working
        # in the only deployment that matters.
        response = self.client.get("/api/v1/trials/")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            response["Content-Type"].startswith("application/problem+json"),
            msg=response["Content-Type"],
        )
        body = response.json()
        self.assertEqual(body["errors"]["endpoint"], "/api/v1/experiments/")
        # The body still reports the path the caller used, prefix included.
        self.assertEqual(body["instance"], "/loop/api/v1/trials/")

    def test_anonymous_caller_gets_the_same_problem_shape_at_the_bare_prefix(self):
        # The auth middleware and the 404 handler have to agree on where the
        # versioned API starts, or this path answers in a third error shape.
        response = Client().get("/api/v1")

        self.assertTrue(
            response["Content-Type"].startswith("application/problem+json"),
            msg=response["Content-Type"],
        )
        self.assertEqual(sorted(response.json()), sorted(PROBLEM_KEYS | {"errors"}))

    def test_api_errors_are_json_even_for_a_caller_asking_for_html(self):
        # The browsable renderer would otherwise answer a browser Accept with an
        # HTML page under the problem+json content type, so a client that checks
        # the header before parsing breaks on the mismatch.
        browser_accept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        cases = (
            ("/api/v1/materials/count/", 404),
            ("/api/v1/stats/", 404),
            ("/api/v1/materials/?bogus=1", 400),
            ("/api/v1/materials/?limit=abc", 400),
            ("/api/v1/materials/?limit=0", 400),
            ("/api/v1/materials/?offset=-1", 400),
            ("/api/v1/trials/", 404),
            ("/api/v1", 404),
            ("/api/v1/api-keys/?bogus=1", 400),
        )
        for path, status in cases:
            with self.subTest(path=path):
                response = self.client.get(path, HTTP_ACCEPT=browser_accept)

                self.assertEqual(response.status_code, status, msg=response.content)
                self.assertTrue(
                    response["Content-Type"].startswith("application/problem+json"),
                    msg=response["Content-Type"],
                )
                body = response.json()
                self.assertNotIn(b"<!DOCTYPE html", response.content[:200])
                self.assertEqual(body["status"], status)
                # One envelope, whichever layer produced the error.
                self.assertLessEqual(PROBLEM_KEYS, set(body))
                self.assertLessEqual(set(body), PROBLEM_KEYS | {"errors"})

    def test_unauthenticated_error_is_json_for_a_browser_accept_too(self):
        response = Client().get(
            "/api/v1/materials/", HTTP_ACCEPT="text/html,*/*;q=0.8"
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["status"], 401)

    def test_browsable_api_still_renders_for_a_successful_request(self):
        # Only errors are pinned to JSON; the browsable renderer is untouched
        # for the responses a human actually browses. CI collects static files
        # before the suite, so this also exercises the production-like manifest
        # storage selected when DEBUG=False.
        response = self.client.get("/api/v1/health/?format=api")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/html"))

    # Template-rendering tests bring their own storage rather than making the
    # whole API suite depend on collectstatic having been run.
    @override_settings(STORAGES=TEST_STORAGES)
    def test_html_site_keeps_its_html_404(self):
        response = self.client.get("/no-such-page-on-the-html-site/")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            response["Content-Type"].startswith("text/html"),
            msg=response["Content-Type"],
        )

    def test_bare_api_v1_answers_json_not_an_html_login_redirect(self):
        # ``/api/v1`` with no trailing slash. The approval gate's exempt list
        # matches ``^api/v1/``, which this does not satisfy, so an anonymous
        # request used to be redirected to the HTML login page while every
        # other API path answered problem+json. The gate now shares the
        # ``is_api_v1_path`` predicate with the token middleware and the 404
        # handler, so all three agree on where the versioned API begins.
        response = self.client.get("/api/v1")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            response["Content-Type"].startswith("application/problem+json"),
            msg=response["Content-Type"],
        )

    @override_settings(FORCE_SCRIPT_NAME="/loop")
    def test_bare_api_v1_answers_json_under_the_production_subpath(self):
        # Production serves the site at ``/loop`` behind a proxy that strips the
        # prefix, so Django sees a bare PATH_INFO with SCRIPT_NAME set. The
        # predicate reads ``path_info``, so mounting under a subpath must not
        # change the answer.
        response = self.client.get("/api/v1", SCRIPT_NAME="/loop")

        self.assertEqual(response.status_code, 404)
        self.assertTrue(
            response["Content-Type"].startswith("application/problem+json"),
            msg=response["Content-Type"],
        )


@override_settings(
    CACHES={
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "api-throttle-tests",
        }
    },
)
class ApiRateLimitTests(TestCase):
    @override_settings(LOOP_API_SESSION_USER_RATE="2/min")
    def test_authenticated_requests_are_limited_by_user(self):
        user = get_user_model().objects.create_user(
            "session-rate-owner", "session-rate-owner@example.com", "pass", is_staff=True
        )
        client = Client(REMOTE_ADDR="203.0.113.8")
        client.force_login(user)

        for _ in range(2):
            self.assertEqual(client.get("/api/v1/version/").status_code, 200)
        response = client.get("/api/v1/version/")

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["status"], 429)
        self.assertIn("Retry-After", response)

    @override_settings(LOOP_API_KEY_RATE="2/min")
    def test_api_keys_are_limited_independently(self):
        user = get_user_model().objects.create_user(
            "rate-owner", "rate-owner@example.com", "pass", is_staff=True
        )
        _, first_key = APIKey.issue(user=user, name="First", scopes=["data:read"])
        _, second_key = APIKey.issue(user=user, name="Second", scopes=["data:read"])

        first = Client(REMOTE_ADDR="203.0.113.10", HTTP_X_API_KEY=first_key)
        self.assertEqual(first.get("/api/v1/me/").status_code, 200)
        self.assertEqual(first.get("/api/v1/me/").status_code, 200)
        limited = first.get("/api/v1/me/")
        self.assertEqual(limited.status_code, 429)

        second = Client(REMOTE_ADDR="203.0.113.10", HTTP_X_API_KEY=second_key)
        self.assertEqual(second.get("/api/v1/me/").status_code, 200)


class ApiKeyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "api-owner", "api-owner@example.com", "pass", is_staff=True
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)

    def test_user_can_create_key_and_use_it_for_me(self):
        created = self.client.post(
            "/api/v1/api-keys/",
            json.dumps({"name": "Research notebook", "scopes": ["data:read", "data:write"]}),
            content_type="application/json",
        )

        self.assertEqual(created.status_code, 201)
        raw_key = created.json()["data"]["key"]
        self.assertTrue(raw_key.startswith("loop_"))

        response = Client().get("/api/v1/me/", HTTP_X_API_KEY=raw_key)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["username"], "api-owner")

    def test_disabled_account_key_cannot_authenticate(self):
        _, raw_key = APIKey.issue(
            user=self.user, name="Disabled owner", scopes=["data:read"]
        )
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        response = Client().get("/api/v1/me/", HTTP_X_API_KEY=raw_key)

        self.assertEqual(response.status_code, 401)

    def test_user_can_bootstrap_key_with_https_basic_auth(self):
        import base64

        authorization = base64.b64encode(b"api-owner:pass").decode("ascii")
        response = Client().post(
            "/api/v1/api-keys/",
            json.dumps({"name": "CLI", "scopes": ["data:read"]}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Basic {authorization}",
        )

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["data"]["key"].startswith("loop_"))

    def test_read_only_key_cannot_write_catalog_records(self):
        created = self.client.post(
            "/api/v1/api-keys/",
            json.dumps({"name": "Read only", "scopes": ["data:read"]}),
            content_type="application/json",
        )
        raw_key = created.json()["data"]["key"]

        response = Client().post(
            "/api/v1/experiments/",
            json.dumps(
                {
                    "elements": {"La": 1, "O": 1},
                    "structure_family": "other",
                    "phase_status": "not_confirmed",
                }
            ),
            content_type="application/json",
            HTTP_X_API_KEY=raw_key,
        )

        self.assertEqual(response.status_code, 403)

    def test_user_can_revoke_key_and_it_stops_authenticating(self):
        created = self.client.post(
            "/api/v1/api-keys/",
            json.dumps({"name": "Temporary", "scopes": ["data:read"]}),
            content_type="application/json",
        )
        identity = created.json()["data"]

        revoked = self.client.delete(f"/api/v1/api-keys/{identity['id']}/")

        self.assertEqual(revoked.status_code, 204)
        denied = Client().get("/api/v1/me/", HTTP_X_API_KEY=identity["key"])
        self.assertEqual(denied.status_code, 401)

    def test_expired_key_is_deleted_when_it_is_used(self):
        credential, raw_key = APIKey.issue(
            user=self.user,
            name="Expired notebook",
            scopes=["data:read"],
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        denied = Client().get("/api/v1/me/", HTTP_X_API_KEY=raw_key)

        self.assertEqual(denied.status_code, 401)
        self.assertFalse(APIKey.objects.filter(pk=credential.pk).exists())

    def test_expired_keys_can_be_purged_without_being_used(self):
        expired, _ = APIKey.issue(
            user=self.user,
            name="Old integration",
            scopes=["data:read"],
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        current, _ = APIKey.issue(
            user=self.user,
            name="Current integration",
            scopes=["data:read"],
            expires_at=timezone.now() + timedelta(days=1),
        )

        call_command("purge_expired_api_keys")

        self.assertFalse(APIKey.objects.filter(pk=expired.pk).exists())
        self.assertTrue(APIKey.objects.filter(pk=current.pk).exists())

    def test_file_upload_requires_files_write_scope(self):
        created = self.client.post(
            "/api/v1/api-keys/",
            json.dumps({"name": "JSON only", "scopes": ["data:write"]}),
            content_type="application/json",
        )
        raw_key = created.json()["data"]["key"]
        record = {
            "elements": {"Sc": 2, "O": 3},
            "structure_family": "other",
            "phase_status": "single_phase",
        }
        upload = SimpleUploadedFile("pattern.csv", b"2theta,intensity\n10,100\n")
        media_dir = tempfile.mkdtemp(prefix="loop_scope_media_")
        try:
            with self.settings(ALLOWED_HOSTS=["*"], MEDIA_ROOT=media_dir):
                response = Client().post(
                    "/api/v1/experiments/",
                    {"record": json.dumps(record), "csv_file": upload},
                    HTTP_X_API_KEY=raw_key,
                    SERVER_NAME="www.example.com",
                )
        finally:
            shutil.rmtree(media_dir, ignore_errors=True)
            Recipe.objects(__raw__={"elements.Sc": {"$exists": True}}).delete()
            Material.objects(__raw__={"elements.Sc": {"$exists": True}}).delete()

        self.assertEqual(response.status_code, 403)


class ApiApprovalTests(TestCase):
    def test_unapproved_non_staff_user_cannot_use_protected_api(self):
        user = get_user_model().objects.create_user(
            "pending-api", "pending-api@example.com", "pass"
        )
        client = Client(enforce_csrf_checks=False)
        client.force_login(user)

        response = client.get("/api/v1/materials/")

        self.assertEqual(response.status_code, 403)

    def test_unapproved_account_cannot_use_platform_or_schema_routes(self):
        user = get_user_model().objects.create_user(
            "pending-platform", "pending-platform@example.com", "pass"
        )
        client = Client(enforce_csrf_checks=False)
        client.force_login(user)

        for path in (
            "/api/v1/health/",
            "/api/v1/version/",
            "/api/v1/openapi/",
            "/api/v1/openapi.json",
            "/api/v1/docs/",
            "/api/v1/redoc/",
        ):
            self.assertEqual(client.get(path).status_code, 403)


class ApiRecordValidationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "validator", "validator@example.com", "pass", is_staff=True
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)

    def test_detailed_experiment_can_be_validated_without_writing(self):
        payload = DOCUMENTED_VALIDATION_PAYLOAD

        response = self.client.post(
            "/api/v1/records/validate/",
            json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["accepted"])
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["normalized"]["synthesis_steps"][0]["milling_rpm"], 250.0)


class ApiAddDataHelperTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "helper-api", "helper-api@example.com", "pass", is_staff=True
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)

    def tearDown(self):
        UserPrecursor.objects(user_id=self.user.id).delete()
        UserProtocol.objects(user_id=self.user.id).delete()
        DOIMapping.objects(doi="10.5555/loop.api.lookup").delete()

    def test_composition_normalization_and_doi_lookup(self):
        normalized = self.client.post(
            "/api/v1/compositions/normalize/",
            json.dumps(
                {
                    "elements": {"Zr": 1, "O": 2},
                    "structure_family": "fluorite",
                }
            ),
            content_type="application/json",
        )
        DOIMapping(
            doi="10.5555/loop.api.lookup",
            title="Known paper",
            material_auids=["M:known"],
        ).save()
        doi = self.client.get(
            "/api/v1/doi/", {"doi": "10.5555/loop.api.lookup"}
        )

        self.assertEqual(normalized.status_code, 200)
        self.assertTrue(normalized.json()["data"]["material_auid"].startswith("M:"))
        self.assertTrue(doi.json()["data"]["found"])

    def test_precursor_and_protocol_lifecycle(self):
        precursor = self.client.post(
            "/api/v1/precursors/",
            json.dumps(DOCUMENTED_PRECURSOR_PAYLOAD),
            content_type="application/json",
        )
        self.assertEqual(precursor.status_code, 201, msg=precursor.content)
        precursor_id = precursor.json()["data"]["id"]
        patched = self.client.patch(
            f"/api/v1/precursors/{precursor_id}/",
            json.dumps({"purity": "99.99%"}),
            content_type="application/json",
        )
        self.assertEqual(patched.json()["data"]["purity"], "99.99%")

        protocol = self.client.post(
            "/api/v1/protocols/",
            json.dumps(DOCUMENTED_PROTOCOL_PAYLOAD),
            content_type="application/json",
        )
        self.assertEqual(protocol.status_code, 201, msg=protocol.content)
        listed = self.client.get("/api/v1/protocols/")
        saved_protocol = listed.json()["data"][0]
        self.assertEqual(saved_protocol["name"], DOCUMENTED_PROTOCOL_PAYLOAD["name"])
        self.assertEqual(saved_protocol["steps"][0]["step_type"], "weighing")
        self.assertEqual(
            saved_protocol["steps"][0]["precursors_list"][0]["cas_number"],
            DOCUMENTED_PRECURSOR_PAYLOAD["cas_number"],
        )

        self.assertEqual(
            self.client.delete(f"/api/v1/precursors/{precursor_id}/").status_code,
            204,
        )


@override_settings(EMBEDDINGS_ON_WRITE=False, RAW_UPLOADS_ROOT="")
class ApiImportTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "import-api", "import-api@example.com", "pass", is_staff=True
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)

    def tearDown(self):
        Recipe.objects(__raw__={"elements.Tb": {"$exists": True}}).delete()
        Material.objects(__raw__={"elements.Tb": {"$exists": True}}).delete()

    def test_imports_multiple_records_and_reports_each_result(self):
        payload = {
            "record_type": "experiment",
            "records": [
                {
                    "elements": {"Tb": 2, "Ti": 2, "O": 7},
                    "structure_family": "pyrochlore",
                    "phase_status": "single_phase",
                    "synthesis_steps": [
                        {"step_type": "heat_treatment", "max_temp_c": 1425}
                    ],
                },
                {
                    "elements": {"Tb": 1, "O": 1},
                    "structure_family": "other",
                },
            ],
        }

        response = self.client.post(
            "/api/v1/imports/", json.dumps(payload), content_type="application/json"
        )

        self.assertEqual(response.status_code, 207)
        body = response.json()
        self.assertEqual(body["meta"]["created"], 1)
        self.assertEqual(body["meta"]["failed"], 1)
        self.assertEqual(body["data"][0]["status"], "created")
        self.assertEqual(body["data"][1]["status"], "rejected")

    def test_import_accepts_jsonl_file_upload(self):
        record = {
            "elements": {"Tb": 2, "Zr": 2, "O": 7},
            "structure_family": "pyrochlore",
            "phase_status": "single_phase",
        }
        upload = SimpleUploadedFile(
            "experiments.jsonl",
            (json.dumps(record) + "\n").encode("utf-8"),
            content_type="application/x-ndjson",
        )

        response = self.client.post(
            "/api/v1/imports/", {"record_type": "experiment", "file": upload}
        )

        self.assertEqual(response.status_code, 201, msg=response.content)
        self.assertEqual(response.json()["meta"]["created"], 1)


@override_settings(EMBEDDINGS_ON_WRITE=False, RAW_UPLOADS_ROOT="")
class ApiExperimentTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "experiment-api", "experiment-api@example.com", "pass", is_staff=True
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)

    def tearDown(self):
        Recipe.objects(__raw__={"elements.Ho": {"$exists": True}}).delete()
        Material.objects(__raw__={"elements.Ho": {"$exists": True}}).delete()

    def test_create_then_read_experiment(self):
        payload = DOCUMENTED_EXPERIMENT_PAYLOAD

        created = self.client.post(
            "/api/v1/experiments/", json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(created.status_code, 201)
        identity = created.json()["data"]

        fetched = self.client.get(
            f"/api/v1/recipes/{identity['recipe_auid']}/trials/{identity['trial_id']}/"
        )
        self.assertEqual(fetched.status_code, 200)
        data = fetched.json()["data"]
        self.assertEqual(data["phase_status"], "single_phase")
        self.assertEqual(data["synthesis_steps"][0]["milling_rpm"], 250.0)

        experiments = self.client.get(
            f"/api/v1/experiments/?material_auid={identity['material_auid']}"
        )
        self.assertEqual(experiments.status_code, 200)
        self.assertEqual(experiments.json()["data"][0]["trial_id"], identity["trial_id"])

        materials = self.client.get(
            "/api/v1/materials/?elements=Ho&structure_family=pyrochlore"
        )
        self.assertEqual(materials.status_code, 200)
        self.assertEqual(materials.json()["data"][0]["material_auid"], identity["material_auid"])

        recipes = self.client.get(
            f"/api/v1/materials/{identity['material_auid']}/recipes/"
        )
        self.assertEqual(recipes.status_code, 200)
        self.assertEqual(recipes.json()["data"][0]["recipe_auid"], identity["recipe_auid"])

        trial_list = self.client.get(
            f"/api/v1/recipes/{identity['recipe_auid']}/trials/"
        )
        self.assertEqual(trial_list.status_code, 200)
        self.assertEqual(trial_list.json()["data"][0]["trial_id"], identity["trial_id"])

        for url in (
            f"/api/v1/materials/{identity['material_auid']}/download/",
            f"/api/v1/recipes/{identity['recipe_auid']}/download/",
            f"/api/v1/recipes/{identity['recipe_auid']}/trials/{identity['trial_id']}/download/",
        ):
            exported = self.client.get(url)
            self.assertEqual(exported.status_code, 200, msg=exported.content)
            self.assertIn("attachment", exported["Content-Disposition"])

    def test_multipart_experiment_accepts_record_json_and_xrd_csv(self):
        media_dir = tempfile.mkdtemp(prefix="loop_api_media_")
        record = {
            "elements": {"Ho": 2, "Zr": 2, "O": 7},
            "structure_family": "pyrochlore",
            "phase_status": "single_phase",
            "raw_data_type": "xrd",
        }
        csv_file = io.BytesIO(b"Angle,Intensity\n10.0,100\n20.0,200\n")
        csv_file.name = "pattern.csv"
        try:
            with self.settings(MEDIA_ROOT=media_dir, ALLOWED_HOSTS=["*"]):
                response = self.client.post(
                    "/api/v1/experiments/",
                    {"record": json.dumps(record), "csv_file": csv_file},
                    SERVER_NAME="www.example.com",
                )
                identity = response.json()["data"]
                download = self.client.get(
                    f"/api/v1/recipes/{identity['recipe_auid']}/trials/{identity['trial_id']}/xrd/"
                )
                metadata = self.client.get(
                    f"/api/v1/recipes/{identity['recipe_auid']}/trials/{identity['trial_id']}/xrd/metadata/"
                )
                with patch("catalog.api.views.render_xrd_plot", return_value="data:image/png;base64,test"):
                    preview = self.client.get(
                        f"/api/v1/recipes/{identity['recipe_auid']}/trials/{identity['trial_id']}/xrd/preview/"
                    )
        finally:
            shutil.rmtree(media_dir, ignore_errors=True)

        self.assertEqual(response.status_code, 201, msg=response.content)
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download["Content-Disposition"])
        self.assertEqual(metadata.status_code, 200, msg=metadata.content)
        self.assertIn("metadata", metadata.json()["data"])
        self.assertEqual(preview.status_code, 200, msg=preview.content)
        self.assertEqual(preview.json()["data"]["image_data_uri"], "data:image/png;base64,test")

    def test_owner_can_delete_experiment(self):
        created = self.client.post(
            "/api/v1/experiments/",
            json.dumps(
                {
                    "elements": {"Ho": 2, "Sn": 2, "O": 7},
                    "structure_family": "pyrochlore",
                    "phase_status": "not_confirmed",
                }
            ),
            content_type="application/json",
        ).json()["data"]
        url = (
            f"/api/v1/recipes/{created['recipe_auid']}/"
            f"trials/{created['trial_id']}/"
        )

        deleted = self.client.delete(url)

        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_owner_can_patch_experiment_outcome(self):
        created = self.client.post(
            "/api/v1/experiments/",
            json.dumps(
                {
                    "elements": {"Ho": 2, "Hf": 2, "O": 7},
                    "structure_family": "pyrochlore",
                    "phase_status": "not_confirmed",
                }
            ),
            content_type="application/json",
        ).json()["data"]
        url = (
            f"/api/v1/recipes/{created['recipe_auid']}/"
            f"trials/{created['trial_id']}/"
        )

        patched = self.client.patch(
            url,
            json.dumps({"phase_status": "single_phase", "comments": "Validated."}),
            content_type="application/json",
        )

        self.assertEqual(patched.status_code, 200, msg=patched.content)
        fetched = self.client.get(url).json()["data"]
        self.assertEqual(fetched["phase_status"], "single_phase")
        self.assertEqual(fetched["notes"], "Validated.")


@override_settings(EMBEDDINGS_ON_WRITE=False, RAW_UPLOADS_ROOT="")
class ApiLiteratureTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "literature-api", "literature-api@example.com", "pass", is_staff=True
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)

    def tearDown(self):
        Recipe.objects(__raw__={"elements.Er": {"$exists": True}}).delete()
        Material.objects(__raw__={"elements.Er": {"$exists": True}}).delete()

    def test_create_then_read_literature(self):
        payload = DOCUMENTED_LITERATURE_PAYLOAD

        created = self.client.post(
            "/api/v1/literature/", json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(created.status_code, 201)
        identity = created.json()["data"]

        fetched = self.client.get(
            f"/api/v1/recipes/{identity['recipe_auid']}/literature/{identity['lit_id']}/"
        )
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["data"]["doi"], payload["doi"])

        records = self.client.get(
            f"/api/v1/literature/?material_auid={identity['material_auid']}"
        )
        self.assertEqual(records.status_code, 200)
        self.assertEqual(records.json()["data"][0]["lit_id"], identity["lit_id"])

    def test_owner_can_patch_literature_metadata(self):
        payload = {
            "doi": "10.5555/loop.api.patch",
            "synthesis_successful": True,
            "elements": {"Er": 2, "Hf": 2, "O": 7},
            "structure_family": "pyrochlore",
            "title": "Before correction",
        }
        identity = self.client.post(
            "/api/v1/literature/", json.dumps(payload), content_type="application/json"
        ).json()["data"]
        url = (
            f"/api/v1/recipes/{identity['recipe_auid']}/"
            f"literature/{identity['lit_id']}/"
        )

        patched = self.client.patch(
            url,
            json.dumps({"title": "Corrected title", "synthesis_successful": False}),
            content_type="application/json",
        )

        self.assertEqual(patched.status_code, 200, msg=patched.content)
        fetched = self.client.get(url).json()["data"]
        self.assertEqual(fetched["title"], "Corrected title")
        self.assertFalse(fetched["synthesis_successful"])

    def test_duplicate_literature_returns_conflict_problem(self):
        payload = {
            "doi": "10.5555/loop.api.duplicate",
            "synthesis_successful": True,
            "elements": {"Er": 2, "Sn": 2, "O": 7},
            "structure_family": "pyrochlore",
        }
        first = self.client.post(
            "/api/v1/literature/", json.dumps(payload), content_type="application/json"
        )
        second = self.client.post(
            "/api/v1/literature/", json.dumps(payload), content_type="application/json"
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["status"], 409)


@override_settings(EMBEDDINGS_ON_WRITE=False, RAW_UPLOADS_ROOT="")
class ApiComputationalTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "computational-api", "computational-api@example.com", "pass", is_staff=True
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)

    def tearDown(self):
        Material.objects(__raw__={"elements.Dy": {"$exists": True}}).delete()

    def test_create_then_read_computational_record(self):
        payload = DOCUMENTED_COMPUTATIONAL_PAYLOAD

        created = self.client.post(
            "/api/v1/computational/", json.dumps(payload), content_type="application/json"
        )
        self.assertEqual(created.status_code, 201)
        identity = created.json()["data"]

        fetched = self.client.get(
            f"/api/v1/materials/{identity['material_auid']}/computations/{identity['comp_auid']}/"
        )
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["data"]["dft_bandgap_ev"], 1.7)

        records = self.client.get(
            f"/api/v1/computational/?material_auid={identity['material_auid']}"
        )
        self.assertEqual(records.status_code, 200)
        self.assertEqual(records.json()["data"][0]["comp_auid"], identity["comp_auid"])

    def test_owner_can_patch_computational_properties(self):
        payload = {
            "elements": {"Dy": 2, "Hf": 2, "O": 7},
            "structure_family": "pyrochlore",
            "dft_source": "S4E",
            "calculation_method": "DFT",
            "functional": "PBE",
            "bandgap_ev": 1.2,
        }
        identity = self.client.post(
            "/api/v1/computational/", json.dumps(payload), content_type="application/json"
        ).json()["data"]
        url = (
            f"/api/v1/materials/{identity['material_auid']}/"
            f"computations/{identity['comp_auid']}/"
        )

        patched = self.client.patch(
            url, json.dumps({"bandgap_ev": 1.8}), content_type="application/json"
        )

        self.assertEqual(patched.status_code, 200, msg=patched.content)
        fetched = self.client.get(url).json()["data"]
        self.assertEqual(fetched["dft_bandgap_ev"], 1.8)

    def test_duplicate_computational_identity_returns_conflict(self):
        payload = {
            "elements": {"Dy": 2, "Sn": 2, "O": 7},
            "structure_family": "pyrochlore",
            "dft_source": "S4E",
            "calculation_method": "DFT",
            "functional": "PBE",
        }
        first = self.client.post(
            "/api/v1/computational/", json.dumps(payload), content_type="application/json"
        )
        second = self.client.post(
            "/api/v1/computational/", json.dumps(payload), content_type="application/json"
        )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)


@override_settings(
    EMBEDDINGS_ON_WRITE=False,
    RAW_UPLOADS_ROOT="",
    CACHES=ISOLATED_THROTTLE_CACHE,
)
class ApiListQueryParamTests(TestCase):
    LIST_ENDPOINTS = (
        "/api/v1/materials/",
        "/api/v1/experiments/",
        "/api/v1/literature/",
        "/api/v1/computational/",
        "/api/v1/precursors/",
        "/api/v1/protocols/",
    )

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "list-params-api", "list-params-api@example.com", "pass", is_staff=True
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)

    def tearDown(self):
        for symbol in ("Ho", "Er", "Dy"):
            Recipe.objects(__raw__={f"elements.{symbol}": {"$exists": True}}).delete()
            Material.objects(__raw__={f"elements.{symbol}": {"$exists": True}}).delete()

    def test_unknown_query_param_is_rejected_by_every_list_endpoint(self):
        for path in self.LIST_ENDPOINTS:
            with self.subTest(path=path):
                response = self.client.get(f"{path}?totally_made_up_param=xyz")

                self.assertEqual(response.status_code, 400, msg=response.content)
                self.assertTrue(
                    response["Content-Type"].startswith("application/problem+json"),
                    msg=response["Content-Type"],
                )
                body = response.json()
                self.assertEqual(body["title"], "Bad Request")
                self.assertEqual(body["status"], 400)
                self.assertEqual(body["instance"], path)
                self.assertIn("totally_made_up_param", body["detail"])
                self.assertEqual(
                    body["errors"]["code"], "unsupported_query_parameters"
                )
                self.assertEqual(
                    body["errors"]["unsupported"], ["totally_made_up_param"]
                )

    def test_response_body_field_name_is_not_silently_accepted_as_a_filter(self):
        response = self.client.get("/api/v1/materials/?element_symbols=Mg")

        self.assertEqual(response.status_code, 400)
        errors = response.json()["errors"]
        self.assertEqual(errors["unsupported"], ["element_symbols"])
        self.assertEqual(
            errors["supported"],
            ["elements", "format", "limit", "offset", "structure_family"],
        )

    def test_error_names_only_the_params_that_were_not_recognised(self):
        response = self.client.get(
            "/api/v1/materials/?structure_family=pyrochlore&search=Mg&num_elements=5"
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["errors"]["unsupported"], ["num_elements", "search"]
        )

    def test_content_negotiation_and_limit_stay_accepted(self):
        response = self.client.get("/api/v1/materials/?format=json&limit=5")

        self.assertEqual(response.status_code, 200, msg=response.content)
        self.assertEqual(response.json()["meta"]["limit"], 5)

    def test_unfiltered_endpoints_honor_paging_but_still_reject_filters(self):
        for path in ("/api/v1/precursors/", "/api/v1/protocols/"):
            with self.subTest(path=path):
                paged = self.client.get(f"{path}?limit=10&offset=0")

                self.assertEqual(paged.status_code, 200, msg=paged.content)
                self.assertEqual(paged.json()["meta"]["limit"], 10)
                self.assertEqual(paged.json()["meta"]["filters_applied"], {})

                rejected = self.client.get(f"{path}?name=anything")

                self.assertEqual(rejected.status_code, 400, msg=rejected.content)
                self.assertEqual(rejected.json()["errors"]["unsupported"], ["name"])

    def test_filters_applied_reports_the_filters_that_took_effect(self):
        created = self.client.post(
            "/api/v1/experiments/",
            json.dumps(DOCUMENTED_EXPERIMENT_PAYLOAD),
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201, msg=created.content)
        identity = created.json()["data"]

        materials = self.client.get(
            "/api/v1/materials/?elements=Ho&structure_family=pyrochlore"
        )
        experiments = self.client.get(
            f"/api/v1/experiments/?material_auid={identity['material_auid']}"
            f"&recipe_auid={identity['recipe_auid']}"
        )
        unfiltered = self.client.get("/api/v1/materials/")

        self.assertEqual(
            materials.json()["meta"]["filters_applied"],
            {"elements": ["Ho"], "structure_family": "pyrochlore"},
        )
        self.assertEqual(
            experiments.json()["meta"]["filters_applied"],
            {
                "material_auid": identity["material_auid"],
                "recipe_auid": identity["recipe_auid"],
            },
        )
        self.assertEqual(unfiltered.json()["meta"]["filters_applied"], {})

    def test_supported_param_with_an_empty_value_is_not_reported_as_applied(self):
        response = self.client.get("/api/v1/materials/?structure_family=&elements=")

        self.assertEqual(response.status_code, 200, msg=response.content)
        self.assertEqual(response.json()["meta"]["filters_applied"], {})

    def test_literature_and_computational_report_their_own_filters(self):
        literature_identity = self.client.post(
            "/api/v1/literature/",
            json.dumps(DOCUMENTED_LITERATURE_PAYLOAD),
            content_type="application/json",
        ).json()["data"]
        computational_identity = self.client.post(
            "/api/v1/computational/",
            json.dumps(DOCUMENTED_COMPUTATIONAL_PAYLOAD),
            content_type="application/json",
        ).json()["data"]

        literature = self.client.get(
            f"/api/v1/literature/?doi={DOCUMENTED_LITERATURE_PAYLOAD['doi']}"
        )
        computational = self.client.get(
            f"/api/v1/computational/?material_auid={computational_identity['material_auid']}"
        )

        self.assertEqual(
            literature.json()["meta"]["filters_applied"],
            {"doi": DOCUMENTED_LITERATURE_PAYLOAD["doi"]},
        )
        self.assertEqual(
            literature.json()["data"][0]["lit_id"], literature_identity["lit_id"]
        )
        self.assertEqual(
            computational.json()["meta"]["filters_applied"],
            {"material_auid": computational_identity["material_auid"]},
        )

    def test_unknown_param_is_rejected_before_any_filtering_happens(self):
        self.client.post(
            "/api/v1/experiments/",
            json.dumps(DOCUMENTED_EXPERIMENT_PAYLOAD),
            content_type="application/json",
        )

        response = self.client.get("/api/v1/experiments/?trial_id=made-up")

        self.assertEqual(response.status_code, 400, msg=response.content)
        self.assertNotIn("data", response.json())

    def test_every_supported_param_name_is_actually_accepted(self):
        # The "supported" list in the rejection is the only machine-readable
        # record of what an endpoint takes, so it has to be true.
        for path in self.LIST_ENDPOINTS:
            supported = self.client.get(f"{path}?zzz=1").json()["errors"]["supported"]
            self.assertLessEqual({"limit", "offset"}, set(supported), path)
            for name in supported:
                with self.subTest(path=path, param=name):
                    response = self.client.get(f"{path}?{name}=1")

                    if response.status_code == 400:
                        self.assertNotEqual(
                            response.json()["errors"]["code"],
                            "unsupported_query_parameters",
                        )

    def test_paged_lists_all_report_the_same_meta_keys(self):
        shapes = {
            path: sorted(self.client.get(path).json()["meta"])
            for path in self.LIST_ENDPOINTS
        }
        expected = [
            "filters_applied",
            "has_more",
            "limit",
            "offset",
            "returned",
            "total",
        ]
        self.assertEqual(shapes, {path: expected for path in self.LIST_ENDPOINTS})

    def test_schema_declares_the_paging_knobs_and_the_400_every_list_can_return(self):
        # A generated client is otherwise told these endpoints take no
        # parameters and never fail, while the server rejects anything it was
        # not told about.
        schema = self.client.get("/api/v1/openapi.json").json()
        for path in self.LIST_ENDPOINTS:
            with self.subTest(path=path):
                get = schema["paths"][path]["get"]
                names = {param["name"] for param in get.get("parameters", [])}

                self.assertLessEqual({"limit", "offset"}, names)
                self.assertIn("400", get["responses"])


class PagedListFixtureMixin:
    """Five materials and three recipes, one of whose children alternate
    between two affiliations so a second viewer sees only half of them."""

    LIST_ENDPOINTS = ApiListQueryParamTests.LIST_ENDPOINTS
    AUID = "M:paginationfixture"
    MATERIAL_AUID = f"{AUID}:material0"
    ELEMENTS = {"Ho": 1, "Er": 1, "Dy": 1, "Yb": 1, "Lu": 1, "O": 7}
    # "unknown" plus five lanthanides keeps the materials listing scoped to this
    # fixture even against a populated catalog.
    MATERIAL_FILTER = "elements=Ho,Er,Dy,Yb,Lu&structure_family=unknown"

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "pagination-api", "pagination-api@example.com", "pass", is_staff=True
        )
        self.client = Client(enforce_csrf_checks=False)
        self.client.force_login(self.user)

        self.apl_user = get_user_model().objects.create_user(
            "pagination-apl", "pagination-apl@example.com", "pass", is_staff=True
        )
        upsert_user_affiliations(self.apl_user, ["APL"])
        self.apl_client = Client(enforce_csrf_checks=False)
        self.apl_client.force_login(self.apl_user)

        self.now = timezone.now()
        self._build_fixtures()

    def tearDown(self):
        Material.objects(id__startswith=self.AUID).delete()
        Recipe.objects(id__startswith=self.AUID).delete()
        UserPrecursor.objects(name__startswith="pagination-").delete()
        UserProtocol.objects(name__startswith="pagination-").delete()
        UserAffiliation.objects(user_id=self.apl_user.id).delete()

    # -- fixtures ---------------------------------------------------------

    def _trial(self, index, visibility=("S4E",)):
        return EmbeddedTrial(
            trial_id=f"T:page{index}",
            trial_date=self.now,
            exp_condition=ExpCondition(),
            visibility_affiliations=list(visibility),
        )

    def _literature(self, index, visibility=("S4E",)):
        return EmbeddedLiterature(
            lit_id=f"L:page{index}",
            doi=f"10.5555/loop.page.{index}",
            visibility_affiliations=list(visibility),
        )

    def _recipe(self, suffix, *, age_seconds, trials=(), literature=()):
        return Recipe(
            id=f"{self.AUID}:material0:R:{suffix}",
            material_auid=self.MATERIAL_AUID,
            elements=dict(self.ELEMENTS),
            element_symbols=sorted(self.ELEMENTS),
            structure_family="unknown",
            visibility_affiliations=["S4E"],
            trials=list(trials),
            literature=list(literature),
            created_at=self.now - timedelta(seconds=age_seconds),
        ).save()

    def _build_fixtures(self):
        self.material_auids = [f"{self.AUID}:material{index}" for index in range(5)]
        for index, auid in enumerate(self.material_auids):
            Material(
                id=auid,
                elements=dict(self.ELEMENTS),
                element_symbols=sorted(self.ELEMENTS),
                num_elements=len(self.ELEMENTS),
                structure_family="unknown",
                default_visibility_affiliations=["S4E"],
                created_at=self.now - timedelta(seconds=index),
                # Five calculations on the first material exercise the
                # computational view's embedded-document paging.
                dft_calculations=[
                    EmbeddedDFT(comp_auid=f"C:page{position}", dft_source="S4E")
                    for position in range(5)
                ]
                if index == 0
                else [],
            ).save()

        # Recipe "a" holds a whole page's worth of trials on its own; "b" exists
        # so a page can straddle the boundary between two parents.
        self._recipe("a", age_seconds=0, trials=[self._trial(i) for i in range(5)])
        self._recipe("b", age_seconds=1, trials=[self._trial(i) for i in range(5, 8)])
        # Alternating visibility: an APL viewer sees only the even positions.
        self._recipe(
            "scoped",
            age_seconds=2,
            trials=[
                self._trial(index, visibility=["APL" if index % 2 == 0 else "Oak Ridge"])
                for index in range(6)
            ],
            literature=[
                self._literature(
                    index, visibility=["APL" if index % 2 == 0 else "Oak Ridge"]
                )
                for index in range(6)
            ],
        )

    # -- helpers ----------------------------------------------------------

    def _body(self, url, client=None):
        response = (client or self.client).get(url)
        self.assertEqual(response.status_code, 200, msg=response.content)
        return response.json()

    def _walk(self, path, page_size, client=None):
        """Collect every row by advancing offset while meta.has_more is true."""
        rows = []
        offset = 0
        for _ in range(50):
            body = self._body(f"{path}&limit={page_size}&offset={offset}", client)
            meta = body["meta"]
            self.assertEqual(meta["offset"], offset)
            self.assertEqual(meta["returned"], len(body["data"]))
            rows.extend(body["data"])
            if not meta["has_more"]:
                return rows
            offset += page_size
        self.fail(f"paging {path} did not terminate")


@override_settings(
    EMBEDDINGS_ON_WRITE=False,
    RAW_UPLOADS_ROOT="",
    CACHES=ISOLATED_THROTTLE_CACHE,
)
class ApiPaginationTests(PagedListFixtureMixin, TestCase):
    """Offset paging, including the views that flatten embedded documents."""

    def test_materials_offset_reaches_records_beyond_the_first_page(self):
        base = f"/api/v1/materials/?{self.MATERIAL_FILTER}"
        full = self._body(f"{base}&limit=100")["data"]
        self.assertEqual(
            [row["material_auid"] for row in full[:5]], self.material_auids
        )

        first = self._body(f"{base}&limit=2")
        second = self._body(f"{base}&limit=2&offset=2")
        third = self._body(f"{base}&limit=2&offset=4")

        self.assertEqual(
            [row["material_auid"] for row in first["data"]], self.material_auids[:2]
        )
        self.assertEqual(
            [row["material_auid"] for row in second["data"]], self.material_auids[2:4]
        )
        self.assertEqual(
            [row["material_auid"] for row in third["data"][:1]],
            self.material_auids[4:],
        )
        self.assertEqual(first["meta"]["offset"], 0)
        self.assertEqual(second["meta"]["offset"], 2)
        self.assertEqual(second["meta"]["returned"], 2)
        self.assertTrue(first["meta"]["has_more"])
        self.assertEqual(
            second["meta"]["filters_applied"],
            {"elements": ["Ho", "Er", "Dy", "Yb", "Lu"], "structure_family": "unknown"},
        )

    def test_experiments_offset_skips_inside_one_recipes_trials(self):
        base = f"/api/v1/experiments/?recipe_auid={self.AUID}:material0:R:a"

        page = self._body(f"{base}&limit=2&offset=3")

        self.assertEqual(
            [row["trial_id"] for row in page["data"]], ["T:page3", "T:page4"]
        )
        self.assertEqual(
            page["meta"],
            {
                "limit": 2,
                "offset": 3,
                "returned": 2,
                "total": 5,
                "has_more": False,
                "filters_applied": {"recipe_auid": f"{self.AUID}:material0:R:a"},
            },
        )

    def test_experiments_offset_crosses_a_parent_document_boundary(self):
        base = f"/api/v1/experiments/?material_auid={self.MATERIAL_AUID}"

        page = self._body(f"{base}&limit=2&offset=4")

        # Recipe "a" supplies trials 0-4 and recipe "b" supplies 5-7, so this
        # page starts in one parent document and finishes in the next.
        self.assertEqual(
            [row["trial_id"] for row in page["data"]], ["T:page4", "T:page5"]
        )
        self.assertEqual(page["data"][0]["recipe_auid"], f"{self.AUID}:material0:R:a")
        self.assertEqual(page["data"][1]["recipe_auid"], f"{self.AUID}:material0:R:b")
        self.assertTrue(page["meta"]["has_more"])

    def test_experiments_offset_counts_only_trials_the_viewer_can_see(self):
        base = f"/api/v1/experiments/?recipe_auid={self.AUID}:material0:R:scoped"

        visible = self._body(f"{base}&limit=100", self.apl_client)["data"]
        page = self._body(f"{base}&limit=1&offset=1", self.apl_client)

        # Positions 1, 3 and 5 belong to Oak Ridge; skipping in the database
        # would have landed on one of them instead of the second APL trial.
        self.assertEqual(
            [row["trial_id"] for row in visible],
            ["T:page0", "T:page2", "T:page4"],
        )
        self.assertEqual([row["trial_id"] for row in page["data"]], ["T:page2"])
        self.assertTrue(page["meta"]["has_more"])

    def test_literature_offset_applies_after_visibility_and_doi_filtering(self):
        base = f"/api/v1/literature/?recipe_auid={self.AUID}:material0:R:scoped"

        page = self._body(f"{base}&limit=1&offset=2", self.apl_client)
        filtered = self._body(
            f"{base}&doi=10.5555/loop.page.2&limit=10&offset=1", self.apl_client
        )

        self.assertEqual([row["lit_id"] for row in page["data"]], ["L:page4"])
        self.assertFalse(page["meta"]["has_more"])
        self.assertEqual(filtered["data"], [])
        self.assertEqual(filtered["meta"]["returned"], 0)
        self.assertFalse(filtered["meta"]["has_more"])

    def test_computational_offset_skips_embedded_calculations(self):
        base = f"/api/v1/computational/?material_auid={self.MATERIAL_AUID}"

        page = self._body(f"{base}&limit=2&offset=2")

        self.assertEqual(
            [row["comp_auid"] for row in page["data"]], ["C:page2", "C:page3"]
        )
        self.assertEqual(page["meta"]["offset"], 2)
        self.assertTrue(page["meta"]["has_more"])

    def test_walking_offset_reproduces_a_single_large_page(self):
        base = f"/api/v1/experiments/?material_auid={self.MATERIAL_AUID}"

        walked = self._walk(base, 3)

        self.assertEqual(walked, self._body(f"{base}&limit=100")["data"])
        self.assertEqual(
            [row["trial_id"] for row in walked][:3],
            ["T:page0", "T:page1", "T:page2"],
        )

    def test_precursors_and_protocols_page_with_offset(self):
        for index in range(4):
            UserPrecursor(
                user_id=self.user.id,
                name=f"pagination-precursor-{index}",
                visibility_affiliations=["S4E"],
            ).save()
            UserProtocol(
                user_id=self.user.id,
                name=f"pagination-protocol-{index}",
                steps=[],
                visibility_affiliations=["S4E"],
            ).save()

        for path, prefix in (
            ("/api/v1/precursors/", "pagination-precursor-"),
            ("/api/v1/protocols/", "pagination-protocol-"),
        ):
            with self.subTest(path=path):
                walked = self._walk(f"{path}?format=json", 2)
                names = [row["name"] for row in walked if row["name"].startswith(prefix)]

                self.assertEqual(names, [f"{prefix}{index}" for index in range(4)])
                self.assertEqual(walked, self._body(f"{path}?limit=100")["data"])

    def test_offset_past_the_end_returns_an_empty_page(self):
        response = self._body(
            f"/api/v1/experiments/?recipe_auid={self.AUID}:material0:R:a&offset=99"
        )

        self.assertEqual(response["data"], [])
        self.assertEqual(response["meta"]["offset"], 99)
        self.assertEqual(response["meta"]["returned"], 0)
        self.assertFalse(response["meta"]["has_more"])

    def test_offset_zero_matches_an_unpaged_request(self):
        base = f"/api/v1/experiments/?material_auid={self.MATERIAL_AUID}"

        self.assertEqual(
            self._body(f"{base}&offset=0"), self._body(f"{base}&limit=50")
        )

    def test_invalid_offset_is_rejected_by_every_list_endpoint(self):
        for path in self.LIST_ENDPOINTS:
            for value in ("abc", "-1", "", "1.5"):
                with self.subTest(path=path, offset=value):
                    response = self.client.get(f"{path}?offset={value}")

                    self.assertEqual(response.status_code, 400, msg=response.content)
                    self.assertTrue(
                        response["Content-Type"].startswith(
                            "application/problem+json"
                        ),
                        msg=response["Content-Type"],
                    )
                    body = response.json()
                    self.assertEqual(body["errors"]["code"], "invalid_offset")
                    self.assertEqual(body["errors"]["received"], value)
                    self.assertEqual(body["instance"], path)
                    self.assertNotIn("data", body)

    def test_unparseable_limit_is_rejected_instead_of_silently_defaulting(self):
        response = self.client.get("/api/v1/materials/?limit=all")

        self.assertEqual(response.status_code, 400, msg=response.content)
        self.assertEqual(response.json()["errors"]["code"], "invalid_limit")
        self.assertEqual(response.json()["errors"]["received"], "all")

    def test_limit_below_one_is_rejected_the_same_way_a_negative_offset_is(self):
        # A caller computing "limit = remaining - fetched" reaches zero and
        # below; clamping that to a one-row page would answer a request nobody
        # made, and offset already refuses the same input.
        for raw in ("0", "-1", "-100"):
            with self.subTest(limit=raw):
                response = self.client.get(f"/api/v1/materials/?limit={raw}")

                self.assertEqual(response.status_code, 400, msg=response.content)
                errors = response.json()["errors"]
                self.assertEqual(errors["code"], "invalid_limit")
                self.assertEqual(errors["received"], raw)

    def test_limit_is_still_capped_at_one_hundred_per_page(self):
        # The ceiling is a server policy rather than a caller mistake, and
        # meta.limit reports the size actually served, so it clamps.
        response = self._body(f"/api/v1/materials/?{self.MATERIAL_FILTER}&limit=1000")

        self.assertEqual(response["meta"]["limit"], 100)

    def test_paging_aliases_are_rejected_rather_than_silently_ignored(self):
        for alias in ("page", "cursor", "skip", "after"):
            with self.subTest(alias=alias):
                response = self.client.get(f"/api/v1/materials/?{alias}=2")

                self.assertEqual(response.status_code, 400, msg=response.content)
                self.assertEqual(
                    response.json()["errors"]["code"], "unsupported_query_parameters"
                )
                self.assertEqual(response.json()["errors"]["unsupported"], [alias])


@override_settings(
    EMBEDDINGS_ON_WRITE=False,
    RAW_UPLOADS_ROOT="",
    CACHES=ISOLATED_THROTTLE_CACHE,
)
class ApiTotalCountTests(PagedListFixtureMixin, TestCase):
    """``meta.total``: the filtered, visibility-scoped population behind a page."""

    def test_every_list_endpoint_reports_a_total_at_least_as_big_as_the_page(self):
        for path in self.LIST_ENDPOINTS:
            with self.subTest(path=path):
                meta = self._body(f"{path}?limit=1")["meta"]

                self.assertIsInstance(meta["total"], int)
                self.assertGreaterEqual(meta["total"], meta["returned"])
                self.assertEqual(meta["has_more"], meta["total"] > meta["returned"])

    def test_total_says_how_much_was_truncated_not_merely_that_it_was(self):
        base = f"/api/v1/materials/?{self.MATERIAL_FILTER}"

        truncated = self._body(f"{base}&limit=2")["meta"]
        whole = self._body(f"{base}&limit=100")["meta"]

        # has_more says another page exists; total is what lets a caller size
        # the gap without paging to the end to find out.
        self.assertTrue(truncated["has_more"])
        self.assertEqual(truncated["returned"], 2)
        self.assertEqual(truncated["total"] - truncated["returned"], 3)
        self.assertFalse(whole["has_more"])
        self.assertEqual((whole["returned"], whole["total"]), (5, 5))

    def test_total_matches_the_rows_a_caller_gets_by_walking_every_page(self):
        for path in (
            f"/api/v1/materials/?{self.MATERIAL_FILTER}",
            f"/api/v1/experiments/?material_auid={self.MATERIAL_AUID}",
            f"/api/v1/literature/?recipe_auid={self.AUID}:material0:R:scoped",
            f"/api/v1/computational/?material_auid={self.MATERIAL_AUID}",
        ):
            with self.subTest(path=path):
                total = self._body(f"{path}&limit=1")["meta"]["total"]

                self.assertEqual(len(self._walk(path, 2)), total)

    def test_total_is_unchanged_by_where_the_caller_is_in_the_result_set(self):
        base = f"/api/v1/experiments/?material_auid={self.MATERIAL_AUID}"

        totals = [
            self._body(f"{base}&limit=2&offset={offset}")["meta"]["total"]
            for offset in (0, 2, 4, 99)
        ]

        # Recipes "a", "b" and "scoped" contribute 5 + 3 + 6 trials.
        self.assertEqual(totals, [14, 14, 14, 14])

    def test_total_counts_only_what_this_viewer_may_see(self):
        base = f"/api/v1/experiments/?recipe_auid={self.AUID}:material0:R:scoped"

        s4e = self._body(f"{base}&limit=1")["meta"]
        apl = self._body(f"{base}&limit=1", self.apl_client)["meta"]

        # Three of the six trials are Oak Ridge only. A total of 6 for the APL
        # viewer would advertise records they are never served.
        self.assertEqual(s4e["total"], 6)
        self.assertEqual(apl["total"], 3)
        self.assertEqual(
            len(self._walk(base, 1, self.apl_client)), apl["total"]
        )

    def test_total_hides_a_population_the_viewer_may_see_none_of(self):
        self._recipe(
            "hidden",
            age_seconds=3,
            trials=[
                self._trial(index, visibility=["Oak Ridge"]) for index in range(4)
            ],
        )
        base = f"/api/v1/experiments/?recipe_auid={self.AUID}:material0:R:hidden"

        apl = self._body(f"{base}&limit=10", self.apl_client)["meta"]

        self.assertEqual(apl["total"], 0)
        self.assertEqual(apl["returned"], 0)
        self.assertFalse(apl["has_more"])
        self.assertEqual(self._body(f"{base}&limit=10")["meta"]["total"], 4)

    def test_materials_total_follows_the_filters_that_were_applied(self):
        unfiltered = self._body("/api/v1/materials/?limit=1")["meta"]["total"]
        filtered = self._body(
            f"/api/v1/materials/?{self.MATERIAL_FILTER}&limit=1"
        )["meta"]["total"]
        narrowed = self._body(
            "/api/v1/materials/?elements=Ho,Er,Dy,Yb,Lu"
            "&structure_family=perovskite&limit=1"
        )["meta"]["total"]

        self.assertEqual(filtered, 5)
        self.assertGreaterEqual(unfiltered, filtered)
        self.assertEqual(narrowed, 0)

    def test_literature_total_applies_the_doi_filter_the_page_applies(self):
        base = f"/api/v1/literature/?recipe_auid={self.AUID}:material0:R:scoped"

        matched = self._body(f"{base}&doi=10.5555/loop.page.2&limit=10")
        # L:page3 is Oak Ridge, so an APL viewer must not learn that it exists.
        hidden = self._body(
            f"{base}&doi=10.5555/loop.page.3&limit=10", self.apl_client
        )

        self.assertEqual(matched["meta"]["total"], 1)
        self.assertEqual([row["lit_id"] for row in matched["data"]], ["L:page2"])
        self.assertEqual(hidden["meta"]["total"], 0)
        self.assertEqual(hidden["data"], [])

    def test_precursor_and_protocol_totals_count_the_owner_scoped_library(self):
        # Deltas rather than absolutes: these collections are not rolled back
        # between tests the way the relational fixtures are.
        before = {
            path: (
                self._body(f"{path}?limit=1")["meta"]["total"],
                self._body(f"{path}?limit=1", self.apl_client)["meta"]["total"],
            )
            for path in ("/api/v1/precursors/", "/api/v1/protocols/")
        }
        for index in range(4):
            UserPrecursor(
                user_id=self.user.id,
                name=f"pagination-precursor-{index}",
                visibility_affiliations=["Oak Ridge"],
            ).save()
            UserProtocol(
                user_id=self.user.id,
                name=f"pagination-protocol-{index}",
                steps=[],
                visibility_affiliations=["Oak Ridge"],
            ).save()

        for path in ("/api/v1/precursors/", "/api/v1/protocols/"):
            with self.subTest(path=path):
                owner = self._body(f"{path}?limit=2")["meta"]
                other = self._body(f"{path}?limit=2", self.apl_client)["meta"]

                self.assertEqual(owner["total"], before[path][0] + 4)
                self.assertTrue(owner["has_more"])
                self.assertEqual(
                    len(self._walk(f"{path}?format=json", 3)), owner["total"]
                )
                # Uploaded by someone else and shared with Oak Ridge only.
                self.assertEqual(other["total"], before[path][1])

    def test_unpaged_subresource_lists_report_a_total_too(self):
        recipe_auid = f"{self.AUID}:material0:R:a"

        trials = self._body(f"/api/v1/recipes/{recipe_auid}/trials/")["meta"]
        recipes = self._body(f"/api/v1/materials/{self.MATERIAL_AUID}/recipes/")["meta"]

        self.assertEqual(
            trials,
            {"returned": 5, "total": 5, "has_more": False, "filters_applied": {}},
        )
        self.assertEqual(recipes["total"], recipes["returned"])
        self.assertFalse(recipes["has_more"])

    def test_every_list_response_carries_filters_applied(self):
        # Uniform enough that reading meta["filters_applied"] never raises,
        # whether or not the endpoint pages.
        recipe_auid = f"{self.AUID}:material0:R:a"
        paths = list(self.LIST_ENDPOINTS) + [
            f"/api/v1/recipes/{recipe_auid}/trials/",
            f"/api/v1/materials/{self.MATERIAL_AUID}/recipes/",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self._body(path)["meta"]["filters_applied"], {})

    def test_unpaged_lists_reject_paging_params_rather_than_ignoring_them(self):
        # These return their whole visible set. Accepting "offset" and answering
        # with row zero anyway is the same silent-truncation hazard the paged
        # endpoints were fixed for.
        recipe_auid = f"{self.AUID}:material0:R:a"
        paths = (
            f"/api/v1/recipes/{recipe_auid}/trials/",
            f"/api/v1/materials/{self.MATERIAL_AUID}/recipes/",
            "/api/v1/api-keys/",
        )
        for path in paths:
            for param in ("offset=2", "limit=2", "page=2", "totally_made_up=xyz"):
                with self.subTest(path=path, param=param):
                    response = self.client.get(f"{path}?{param}")

                    self.assertEqual(response.status_code, 400, msg=response.content)
                    self.assertEqual(
                        response.json()["errors"]["code"],
                        "unsupported_query_parameters",
                    )
                    self.assertEqual(
                        response.json()["errors"]["supported"], ["format"]
                    )
