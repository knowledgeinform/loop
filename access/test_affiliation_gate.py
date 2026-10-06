"""LOOP approval needs an affiliation set by an admin; sign-up asks for none.

Run in the dev stack or the production image:
    python manage.py test access.test_affiliation_gate
None of these tests needs MongoDB: the affiliations are patched.

WHY THESE TESTS EXIST
Decided by Corey Oses, 2026-10-01: the public sign-up form names no partner
group and asks for no affiliation; an admin sets the affiliation when
approving the account. An account in the Approved group without an
affiliation waits, as an unapproved account does, everywhere approval is
checked (the gate middleware, the API, the views, /api/v1/access/); since
#50 the record filters also treat no affiliation as none (a second guard;
they used to treat it as S4E, which sees every record). Saving
the Users page with no affiliation ticked removes the account's affiliation
instead of storing S4E. Oak Ridge is no longer an affiliation.

IF A TEST FAILS
Read what it guards before changing it. This is the first of two guards
for an account without an affiliation; the record filters are the second.
"""

from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from access import policy
from access.tests import COMMON, logged_in, make_user
from catalog import documents
from catalog.api.permissions import IsApprovedUser
from catalog.forms import SignupForm
from catalog.permissions import canonical_affiliation

AFFILIATIONS = "access.policy.loop_affiliations"
CONTACT = "s4e-loop@mintaka.arch.jhu.edu"


def _signup_url():
    try:
        return reverse("signup")
    except NoReverseMatch:  # DEV_LOCKDOWN removes self-registration
        return None


@override_settings(**COMMON)
class SignupAsksForNoAffiliationTests(TestCase):
    def test_the_form_has_no_affiliation_field(self):
        self.assertNotIn("affiliations", SignupForm().fields)

    def test_the_page_names_no_partner(self):
        url = _signup_url()
        if url is None:
            self.skipTest("sign-up is off (DEV_LOCKDOWN)")
        page = Client().get(url)
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertNotIn('name="affiliations"', html)
        for name in ("APL", "Oak Ridge", "MIT"):
            self.assertNotIn(f">{name}<", html)

    def test_signing_up_writes_no_affiliation(self):
        url = _signup_url()
        if url is None:
            self.skipTest("sign-up is off (DEV_LOCKDOWN)")
        with mock.patch.object(documents, "upsert_user_affiliations") as upsert, \
                mock.patch.object(documents.UserAffiliation, "objects") as objects:
            response = Client().post(url, {
                "username": "newcomer",
                "first_name": "New",
                "last_name": "Comer",
                "email": "newcomer@example.org",
                "password1": "Signup-test-pw-91",
                "password2": "Signup-test-pw-91",
            })
        self.assertEqual(response.status_code, 302, response.content[:500])
        self.assertTrue(get_user_model().objects.filter(username="newcomer").exists())
        upsert.assert_not_called()
        objects.assert_not_called()
        # The lab's LOOP address (Corey, 2026-10-03): the new user is told to
        # write to it, and the sign-up notice goes there.
        self.assertEqual([m.to for m in mail.outbox], [["newcomer@example.org"], [CONTACT]])
        self.assertIn(CONTACT, mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].reply_to, [CONTACT])

    def test_the_waiting_pages_give_the_lab_address(self):
        if _signup_url() is None:
            self.skipTest("sign-up is off (DEV_LOCKDOWN)")
        for name in ("awaiting_approval", "signup_complete"):
            html = Client().get(reverse(name)).content.decode()
            self.assertIn(f"mailto:{CONTACT}", html, name)
            self.assertNotIn("mailto:loop@", html, name)


@override_settings(**COMMON)
class ApprovalNeedsAnAffiliationTests(TestCase):
    def setUp(self):
        self.approved = make_user("approved", "approved@example.org", loop_approved=True)
        self.waiting = make_user("waiting", "waiting@example.org")
        self.staff = make_user("staffer", "staffer@example.org", is_staff=True)

    def test_policy(self):
        with mock.patch(AFFILIATIONS, return_value=[]):
            self.assertFalse(policy.has_loop_access(self.approved))
            self.assertFalse(policy.has_loop_access(self.waiting))
            self.assertTrue(policy.has_loop_access(self.staff))
        with mock.patch(AFFILIATIONS, return_value=["APL"]):
            self.assertTrue(policy.has_loop_access(self.approved))
            self.assertFalse(policy.has_loop_access(self.waiting))

    def test_the_gate_sends_an_approved_account_without_one_to_wait(self):
        client = logged_in(self.approved)
        with mock.patch(AFFILIATIONS, return_value=[]):
            response = client.get(reverse("account"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("awaiting_approval"))
        # The Account page lists the affiliations, read from MongoDB; fixed here.
        with mock.patch(AFFILIATIONS, return_value=["APL"]), \
                mock.patch("catalog.views._user_affiliations", return_value=["APL"]):
            self.assertEqual(client.get(reverse("account")).status_code, 200)

    def test_the_api_refuses_an_approved_account_without_one(self):
        client = logged_in(self.approved)
        with mock.patch(AFFILIATIONS, return_value=[]):
            self.assertEqual(client.get(reverse("api-v1-health")).status_code, 403)
            access = client.get(reverse("api-v1-access")).json()["data"]
        self.assertFalse(access["loop"])
        with mock.patch(AFFILIATIONS, return_value=["S4E"]):
            self.assertEqual(client.get(reverse("api-v1-health")).status_code, 200)
            self.assertTrue(client.get(reverse("api-v1-access")).json()["data"]["loop"])

    def test_the_api_permission_itself(self):
        request = RequestFactory().get("/api/v1/health/")
        request.user = self.approved
        with mock.patch(AFFILIATIONS, return_value=[]):
            self.assertFalse(IsApprovedUser().has_permission(request, None))
        with mock.patch(AFFILIATIONS, return_value=["MIT"]):
            self.assertTrue(IsApprovedUser().has_permission(request, None))

    def test_the_sign_up_pages_send_it_to_wait(self):
        if _signup_url() is None:
            self.skipTest("sign-up is off (DEV_LOCKDOWN)")
        client = logged_in(self.approved)
        with mock.patch(AFFILIATIONS, return_value=[]):
            for name in ("signup_pending", "signup_complete"):
                response = client.get(reverse(name))
                self.assertEqual(response["Location"], reverse("awaiting_approval"), name)


@override_settings(**COMMON)
class LoopAffiliationsTests(TestCase):
    """loop_affiliations reads the account's record with no S4E default.

    The answer is kept on the user object for the rest of the request, so
    each check below reads a fresh user, as a new request would.
    """

    def setUp(self):
        self.pk = make_user("reader", "reader@example.org", loop_approved=True).pk

    def fresh(self):
        return get_user_model().objects.get(pk=self.pk)

    def _with_record(self, record):
        objects = mock.MagicMock()
        objects.return_value.only.return_value.first.return_value = record
        return mock.patch.object(documents.UserAffiliation, "objects", objects)

    def test_no_record_means_none(self):
        with self._with_record(None):
            self.assertEqual(policy.loop_affiliations(self.fresh()), [])
            self.assertFalse(policy.has_loop_access(self.fresh()))

    def test_an_empty_record_means_none(self):
        with self._with_record(SimpleNamespace(affiliations=[])):
            self.assertEqual(policy.loop_affiliations(self.fresh()), [])

    def test_only_current_affiliations_count(self):
        with self._with_record(SimpleNamespace(affiliations=["Oak Ridge"])):
            self.assertEqual(policy.loop_affiliations(self.fresh()), [])
        with self._with_record(SimpleNamespace(affiliations=["Oak Ridge", "APL"])):
            self.assertEqual(policy.loop_affiliations(self.fresh()), ["APL"])
            self.assertTrue(policy.has_loop_access(self.fresh()))

    def test_mongodb_is_asked_once_per_request(self):
        user = self.fresh()
        with self._with_record(SimpleNamespace(affiliations=["S4E"])) as objects:
            self.assertTrue(policy.has_loop_access(user))
            self.assertTrue(policy.has_loop_access(user))
        self.assertEqual(objects.call_count, 1)

    def test_unreadable_mongodb_means_none(self):
        objects = mock.MagicMock(side_effect=RuntimeError("no MongoDB"))
        with mock.patch.object(documents.UserAffiliation, "objects", objects), \
                self.assertLogs("access.policy", level="ERROR"):
            self.assertEqual(policy.loop_affiliations(self.fresh()), [])

    def test_signed_out_means_none(self):
        anonymous = SimpleNamespace(is_authenticated=False, is_active=False)
        self.assertEqual(policy.loop_affiliations(anonymous), [])


@override_settings(**COMMON)
class MediaFilesNeedApprovalTests(TestCase):
    """/media/ is exempt from the gate; the media view checks approval itself,
    before it reads any record."""

    def test_an_approved_account_without_one_sees_no_file(self):
        from catalog import media_access

        user = make_user("viewer", "viewer@example.org", loop_approved=True)
        with mock.patch(AFFILIATIONS, return_value=[]), \
                mock.patch.object(media_access, "get_recipe", side_effect=AssertionError("read a record")):
            self.assertFalse(media_access._is_visible("R:def", "1", user))

    def test_with_one_the_trial_decides(self):
        from catalog import media_access

        user = make_user("viewer2", "viewer2@example.org", loop_approved=True)
        trial = SimpleNamespace(trial_id="1", visibility_affiliations=["APL"])
        with mock.patch(AFFILIATIONS, return_value=["APL"]), \
                mock.patch("catalog.views._user_affiliations", return_value=["APL"]), \
                mock.patch.object(media_access, "get_recipe", return_value=object()), \
                mock.patch.object(media_access, "find_embedded_trial", return_value=trial):
            self.assertTrue(media_access._is_visible("R:def", "1", user))


class SavingNoAffiliationTests(SimpleTestCase):
    """The Users page with no box ticked removes the record; it stored S4E."""

    def setUp(self):
        self.user = SimpleNamespace(id=987654321, get_username=lambda: "nobody")

    def _objects(self, record):
        objects = mock.MagicMock()
        objects.return_value.first.return_value = record
        return mock.patch.object(documents.UserAffiliation, "objects", objects)

    def test_an_existing_record_is_removed(self):
        record = mock.MagicMock()
        with self._objects(record) as objects:
            documents.upsert_user_affiliations(self.user, [])
        record.delete.assert_called_once_with()
        objects.return_value.update_one.assert_not_called()

    def test_no_record_is_created(self):
        with self._objects(None) as objects:
            documents.upsert_user_affiliations(self.user, ["Oak Ridge", ""])
        objects.return_value.update_one.assert_not_called()

    def test_a_selection_is_stored_as_before(self):
        with self._objects(None) as objects, \
                mock.patch("catalog.archive.writer.is_enabled", return_value=False):
            documents.upsert_user_affiliations(self.user, ["APL", "APL", "MIT"])
        objects.return_value.update_one.assert_called_once_with(
            set__username="nobody", set__affiliations=["APL", "MIT"], upsert=True
        )


class AffiliationListTests(SimpleTestCase):
    def test_oak_ridge_is_gone(self):
        self.assertEqual(documents.AFFILIATION_VALUES, ("S4E", "APL", "MIT"))
        self.assertIsNone(canonical_affiliation("Oak Ridge"))
        self.assertIsNone(canonical_affiliation("oakridge"))
        self.assertEqual(canonical_affiliation("apl"), "APL")
        self.assertEqual(canonical_affiliation("mit"), "MIT")
