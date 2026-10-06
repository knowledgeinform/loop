"""Tests for site access: the CHAOS curtain beside LOOP's approval.

Run in the dev stack: python manage.py test access
None of these tests needs MongoDB.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from access import policy
from access.models import AccessRequest, TermsAcceptance, VerifiedEmail
from catalog.models import APIKey

TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "access-tests",
    }
}
COMMON = dict(
    STORAGES=TEST_STORAGES,
    CACHES=LOCMEM_CACHE,
    S4E_SHELL_URL="",
    APPROVED_GROUP_NAME="Approved",
    CHAOS_GROUP_NAME="CHAOS",
    CHAOS_TERMS_VERSION="test-1",
    CHAOS_AUTO_EMAIL_SUFFIXES=[".edu"],
    ACCESS_ADMIN_EMAILS=["admin@example.org"],
    # The profile requirement has its own tests (test_profile.py); these
    # accounts have no profile.
    PROFILE_REQUIRED=False,
    USAGE_DIR="",
)

# LOOP approval is the Approved group plus an affiliation, which lives in
# MongoDB (access.policy.loop_affiliations). These tests need no MongoDB, so
# every account here has the S4E affiliation and the group alone decides;
# test_affiliation_gate.py covers the affiliation half.
_affiliations = mock.patch("access.policy.loop_affiliations", return_value=["S4E"])


def setUpModule():
    _affiliations.start()


def tearDownModule():
    _affiliations.stop()


REQUEST_FORM = {
    "affiliation": "Example University, graduate student",
    "purpose": "Screening oxide compositions for a class project.",
    "accept_terms": "on",
}

User = get_user_model()


def make_user(username, email, *, verified=False, loop_approved=False, **extra):
    user = User.objects.create_user(username, email, "Access-test-pw-1", **extra)
    if verified:
        policy.record_verified_email(user)
    if loop_approved:
        user.groups.add(Group.objects.get_or_create(name="Approved")[0])
    return user


def logged_in(user):
    client = Client()
    client.force_login(user)
    return client


class EmailRuleTests(SimpleTestCase):
    def test_domains_ending_in_edu_match(self):
        for email in ("a@jhu.edu", "b@cs.cmu.edu", "C@JHU.EDU", "d@jhu.edu."):
            self.assertTrue(policy.email_qualifies(email, [".edu"]), email)

    def test_other_domains_do_not_match(self):
        for email in ("a@edu.example.com", "b@example.com", "c@edu", "d@jhu.edu.evil.com",
                      "", "no-at-sign", "x@y@jhu.edu"):
            self.assertFalse(policy.email_qualifies(email, [".edu"]), email)

    def test_suffix_list_is_configurable(self):
        self.assertTrue(policy.email_qualifies("a@ox.ac.uk", [".edu", "ac.uk"]))
        self.assertFalse(policy.email_qualifies("a@ox.ac.uk", [".edu"]))


@override_settings(**COMMON)
class ActivationTests(TestCase):
    def test_activation_link_records_the_verified_address(self):
        user = make_user("newbie", "Newbie@JHU.edu")
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        response = Client().get(reverse("activate", args=[uid, token]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(VerifiedEmail.objects.filter(user=user, email="newbie@jhu.edu").exists())
        self.assertEqual(policy.verified_email(user), "newbie@jhu.edu")

    def test_changed_address_is_not_verified(self):
        user = make_user("mover", "mover@example.com", verified=True)
        user.email = "mover@jhu.edu"
        user.save()

        self.assertIsNone(policy.verified_email(user))
        self.assertIsNone(policy.automatic_route(user))


@override_settings(**COMMON)
class RequestFlowTests(TestCase):
    def post_request(self, user, data=None):
        client = logged_in(user)
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(reverse("chaos_access"), data or REQUEST_FORM)

    def test_verified_edu_address_is_granted_at_once(self):
        user = make_user("student", "student@jhu.edu", verified=True)

        response = self.post_request(user)

        self.assertRedirects(response, reverse("chaos_access"), fetch_redirect_response=False)
        request = AccessRequest.objects.get(user=user)
        self.assertEqual(request.status, AccessRequest.APPROVED)
        self.assertEqual(request.decided_via, AccessRequest.VIA_VERIFIED_EMAIL)
        self.assertEqual(request.terms_version, "test-1")
        self.assertTrue(user.groups.filter(name="CHAOS").exists())
        self.assertTrue(
            TermsAcceptance.objects.filter(user=user, document="chaos", version="test-1").exists()
        )
        self.assertTrue(policy.has_chaos_access(user))
        self.assertFalse(policy.has_loop_access(user))
        self.assertEqual([m.to for m in mail.outbox], [["student@jhu.edu"]])

    def test_unverified_edu_address_waits_for_an_admin(self):
        user = make_user("unverified", "unverified@jhu.edu")

        self.post_request(user)

        request = AccessRequest.objects.get(user=user)
        self.assertEqual(request.status, AccessRequest.PENDING)
        self.assertFalse(policy.has_chaos_access(user))
        self.assertEqual([m.to for m in mail.outbox], [["admin@example.org"]])

    def test_other_address_waits_then_admin_approves(self):
        user = make_user("industry", "someone@example.com", verified=True)
        admin = make_user("boss", "boss@example.org", is_staff=True, is_superuser=True)
        self.post_request(user)
        request = AccessRequest.objects.get(user=user)
        self.assertEqual(request.status, AccessRequest.PENDING)

        policy.approve(request, via=AccessRequest.VIA_ADMIN, by=admin)

        request.refresh_from_db()
        self.assertEqual(request.decided_by, admin)
        self.assertTrue(policy.has_chaos_access(user))

    def test_admin_action_approves_pending_requests(self):
        user = make_user("viaadmin", "viaadmin@example.com")
        admin = make_user("boss", "boss@example.org", is_staff=True, is_superuser=True)
        self.post_request(user)
        request = AccessRequest.objects.get(user=user)

        logged_in(admin).post(
            reverse("admin:access_accessrequest_changelist"),
            {"action": "approve_selected", "_selected_action": [request.pk]},
        )

        request.refresh_from_db()
        self.assertEqual(request.status, AccessRequest.APPROVED)
        self.assertEqual(request.decided_via, AccessRequest.VIA_ADMIN)
        self.assertTrue(policy.has_chaos_access(user))

    def test_loop_approved_account_is_granted_at_once(self):
        user = make_user("member", "member@example.com", loop_approved=True)

        self.post_request(user)

        request = AccessRequest.objects.get(user=user)
        self.assertEqual(request.status, AccessRequest.APPROVED)
        self.assertEqual(request.decided_via, AccessRequest.VIA_LOOP_APPROVED)

    def test_terms_must_be_accepted(self):
        user = make_user("hasty", "hasty@jhu.edu", verified=True)
        data = dict(REQUEST_FORM)
        del data["accept_terms"]

        response = self.post_request(user, data)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AccessRequest.objects.filter(user=user).exists())
        self.assertFalse(TermsAcceptance.objects.filter(user=user).exists())

    def test_a_second_request_does_not_open_another(self):
        user = make_user("twice", "twice@example.com")
        self.post_request(user)
        policy.request_chaos_access(user, affiliation="x", purpose="y")

        self.assertEqual(AccessRequest.objects.filter(user=user).count(), 1)

    def test_pending_request_is_granted_once_the_account_qualifies(self):
        user = make_user("later", "later@example.com")
        self.post_request(user)
        user.groups.add(Group.objects.get_or_create(name="Approved")[0])

        logged_in(user).get(reverse("chaos_access"))

        request = AccessRequest.objects.get(user=user)
        self.assertEqual(request.status, AccessRequest.APPROVED)
        self.assertEqual(request.decided_via, AccessRequest.VIA_LOOP_APPROVED)

    def test_new_terms_version_asks_again(self):
        user = make_user("returning", "returning@jhu.edu", verified=True)
        self.post_request(user)

        with self.settings(CHAOS_TERMS_VERSION="test-2"):
            self.assertFalse(policy.has_chaos_access(user))
            client = logged_in(user)
            self.assertEqual(client.get(reverse("chaos_access")).context["state"], "terms_needed")
            client.post(reverse("chaos_access"), {"accept_terms": "on"})
            self.assertTrue(policy.has_chaos_access(user))

    def test_revoked_access_is_not_granted_again_automatically(self):
        user = make_user("revoked", "revoked@jhu.edu", verified=True)
        self.post_request(user)
        request = AccessRequest.objects.get(user=user)

        policy.revoke(request)

        self.assertFalse(user.groups.filter(name="CHAOS").exists())
        self.assertFalse(policy.has_chaos_access(user))
        page = logged_in(user).get(reverse("chaos_access"))
        self.assertEqual(page.context["state"], "request")
        self.assertTrue(page.context["prior_refusal"])
        self.assertIsNone(page.context["automatic_route"])

        self.post_request(user)

        self.assertTrue(
            AccessRequest.objects.filter(user=user, status=AccessRequest.PENDING).exists()
        )
        self.assertFalse(policy.has_chaos_access(user))

    def test_denied_account_waits_for_an_admin_even_when_it_later_qualifies(self):
        user = make_user("denied", "denied@example.com")
        self.post_request(user)
        policy.deny(AccessRequest.objects.get(user=user))
        user.groups.add(Group.objects.get_or_create(name="Approved")[0])

        self.post_request(user)

        self.assertTrue(
            AccessRequest.objects.filter(user=user, status=AccessRequest.PENDING).exists()
        )
        self.assertFalse(policy.has_chaos_access(user))

    def test_group_removed_outside_the_requests_is_recorded_as_revoked(self):
        user = make_user("ungrouped", "ungrouped@jhu.edu", verified=True)
        self.post_request(user)
        user.groups.remove(Group.objects.get(name="CHAOS"))

        page = logged_in(user).get(reverse("chaos_access"))

        self.assertEqual(AccessRequest.objects.get(user=user).status, AccessRequest.REVOKED)
        self.assertEqual(page.context["state"], "request")
        self.assertTrue(page.context["prior_refusal"])

    def test_requests_cannot_be_deleted_in_the_admin(self):
        user = make_user("kept", "kept@example.com")
        admin = make_user("boss", "boss@example.org", is_staff=True, is_superuser=True)
        self.post_request(user)
        request = AccessRequest.objects.get(user=user)

        response = logged_in(admin).get(
            reverse("admin:access_accessrequest_delete", args=[request.pk])
        )

        self.assertEqual(response.status_code, 403)

    def test_deleting_a_user_still_removes_their_access_records(self):
        user = make_user("leaving", "leaving@jhu.edu", verified=True)
        admin = make_user("boss", "boss@example.org", is_staff=True, is_superuser=True)
        self.post_request(user)

        logged_in(admin).post(reverse("admin:auth_user_delete", args=[user.pk]), {"post": "yes"})

        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(AccessRequest.objects.filter(user_id=user.pk).exists())


@override_settings(**COMMON)
class GateTests(TestCase):
    def test_access_pages_are_open_to_accounts_loop_has_not_approved(self):
        user = make_user("outsider", "outsider@jhu.edu", verified=True)
        client = logged_in(user)

        self.assertEqual(client.get(reverse("chaos_access")).status_code, 200)
        # LOOP itself still sends the same account to the approval page.
        response = client.get(reverse("browse_data"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("awaiting_approval"), response["Location"])

    def test_request_page_needs_a_login_and_terms_page_does_not(self):
        response = Client().get(reverse("chaos_access"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])
        self.assertEqual(Client().get(reverse("chaos_terms")).status_code, 200)


@override_settings(**COMMON)
class AccessApiTests(TestCase):
    url = "/api/v1/access/"

    def grant_chaos(self, user):
        policy.request_chaos_access(user, affiliation="Example University", purpose="Research")
        self.assertTrue(policy.has_chaos_access(user))

    def test_anonymous_caller(self):
        response = Client().get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["data"], {"authenticated": False, "loop": False, "chaos": False}
        )
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_session_with_chaos_access_only(self):
        user = make_user("chaosonly", "chaosonly@jhu.edu", verified=True)
        self.grant_chaos(user)

        data = logged_in(user).get(self.url).json()["data"]

        self.assertTrue(data["authenticated"])
        self.assertEqual(data["via"], "session")
        self.assertTrue(data["chaos"])
        self.assertFalse(data["loop"])
        self.assertEqual(data["user"]["username"], "chaosonly")

    def test_loop_member_without_a_chaos_request(self):
        user = make_user("looponly", "looponly@example.com", loop_approved=True)

        data = logged_in(user).get(self.url).json()["data"]

        self.assertTrue(data["loop"])
        self.assertFalse(data["chaos"])

    def test_api_key_needs_the_chaos_scope(self):
        user = make_user("keyholder", "keyholder@jhu.edu", verified=True)
        self.grant_chaos(user)
        _, with_scope = APIKey.issue(user=user, name="chaos", scopes=["chaos:read"])
        _, without_scope = APIKey.issue(user=user, name="other", scopes=["data:read"])

        yes = Client().get(self.url, HTTP_AUTHORIZATION=f"Bearer {with_scope}").json()["data"]
        no = Client().get(self.url, HTTP_AUTHORIZATION=f"Bearer {without_scope}").json()["data"]

        self.assertEqual(yes["via"], "api_key")
        self.assertTrue(yes["chaos"])
        self.assertEqual(yes["scopes"], ["chaos:read"])
        self.assertFalse(no["chaos"])

    def test_key_stops_working_when_terms_change(self):
        user = make_user("stale", "stale@jhu.edu", verified=True)
        self.grant_chaos(user)
        _, raw = APIKey.issue(user=user, name="chaos", scopes=["chaos:read"])

        with self.settings(CHAOS_TERMS_VERSION="test-2"):
            data = Client().get(self.url, HTTP_AUTHORIZATION=f"Bearer {raw}").json()["data"]

        self.assertFalse(data["chaos"])
        self.assertFalse(data["chaos_terms_current"])

    def test_invalid_key_is_refused(self):
        response = Client().get(self.url, HTTP_AUTHORIZATION="Bearer loop_nope_nope")
        self.assertEqual(response.status_code, 401)


@override_settings(**COMMON)
class DeveloperKeysTests(TestCase):
    def test_chaos_only_account_gets_the_chaos_scope_only(self):
        user = make_user("chaoskeys", "chaoskeys@jhu.edu", verified=True)
        policy.request_chaos_access(user, affiliation="Example University", purpose="Research")
        client = logged_in(user)

        page = client.get(reverse("developer_keys"))
        self.assertEqual(tuple(page.context["api_key_scopes"]), ("chaos:read",))

        client.post(
            reverse("developer_keys"),
            {"action": "create_key", "name": "blocked", "scopes": ["data:read"]},
        )
        self.assertFalse(APIKey.objects.filter(user=user).exists())

        client.post(
            reverse("developer_keys"),
            {"action": "create_key", "name": "chaos", "scopes": ["chaos:read"]},
        )
        self.assertEqual(list(APIKey.objects.get(user=user).scopes), ["chaos:read"])

    def test_loop_member_without_chaos_access_is_not_offered_the_chaos_scope(self):
        user = make_user("loopkeys", "loopkeys@example.com", loop_approved=True)

        page = logged_in(user).get(reverse("developer_keys"))

        self.assertNotIn("chaos:read", page.context["api_key_scopes"])
        self.assertIn("data:read", page.context["api_key_scopes"])
