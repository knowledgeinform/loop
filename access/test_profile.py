"""Tests for the profile every account fills in once, and for the usage record.

Run in the dev stack or the production image: python manage.py test access
None of these tests needs MongoDB (every page request below is answered by a
middleware or by a view that does not read the catalog; the Account page's
partner affiliations, which live in MongoDB, are replaced by a fixed list).

WHY THESE TESTS EXIST
Decided by Corey Oses, 2026-09-29: every account, new and existing, staff
included, gives its institution, country, role, sector, research areas and
intended use once, and accepts the notice that use of the tools is recorded;
and every request to LOOP is recorded with the account and the visitor's
address, and kept for the life of the project plus five years. Both are what
the S4E Laboratory reports to its funders. The tests guard the two halves:
nobody gets past without a profile (pages, the older JSON endpoints, the
versioned API with a key), the CHAOS gate and kiosk can read the state from
/api/v1/access/, and each request leaves one line with the account, how it
signed in and the address, without secrets.

IF A TEST FAILS
Read what it guards before changing it. Loosening the profile requirement
lets in accounts the reports cannot describe; loosening the record loses use
that cannot be recovered later.
"""

import json
import os
import shutil
import tempfile
from unittest import mock

from django.contrib.sessions.models import Session
from django.core.management import CommandError, call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from access import policy
from access.forms import _orcid_checksum_ok
from access.models import Profile, TermsAcceptance
from access.tests import COMMON, logged_in, make_user
from catalog.models import APIKey

PROFILE_ON = dict(COMMON, PROFILE_REQUIRED=True, USAGE_NOTICE_VERSION="notice-1")


# LOOP approval is the Approved group plus an affiliation, which lives in
# MongoDB (access.policy.loop_affiliations). These tests need no MongoDB, so
# every account here has the S4E affiliation and the group alone decides;
# test_affiliation_gate.py covers the affiliation half.
_affiliations = mock.patch("access.policy.loop_affiliations", return_value=["S4E"])


def setUpModule():
    _affiliations.start()


def tearDownModule():
    _affiliations.stop()


GOOD = {
    "institution": "Johns Hopkins University",
    "ror_id": "https://ror.org/00za53h95",
    "ror_type": "education",
    "country": "US",
    "role": "graduate",
    "sector": "academia",
    "research_areas": ["high_entropy", "ml"],
    "intended_use": "Screening high-entropy oxide compositions for a thesis chapter.",
    "orcid": "0000-0002-1825-0097",
    "heard_from": "colleague",
    "contact_ok": "on",
    "accept_usage_notice": "on",
}


def complete_profile(user):
    Profile.objects.create(
        user=user,
        institution="Example University",
        country="US",
        role="faculty",
        sector="academia",
        research_areas=["ml"],
        intended_use="Teaching and research on oxides.",
        completed_at=timezone.now(),
    )
    policy.record_usage_notice(user)


def not_sent_to_profile(response):
    return not response.get("Location", "").startswith(reverse("profile"))


class OrcidTests(SimpleTestCase):
    def test_check_digit(self):
        self.assertTrue(_orcid_checksum_ok("0000-0002-1825-0097"))
        self.assertTrue(_orcid_checksum_ok("0000-0002-1694-233X"))
        self.assertFalse(_orcid_checksum_ok("0000-0002-1825-0098"))


@override_settings(**PROFILE_ON)
class ProfileRequiredTests(TestCase):
    def test_an_account_without_a_profile_is_sent_to_the_profile_page(self):
        user = make_user("noprofile", "noprofile@example.com", loop_approved=True)
        response = logged_in(user).get("/browse/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("profile") + "?next="))
        self.assertIn("browse", response["Location"])

    def test_staff_are_not_exempt(self):
        staff = make_user("staffer", "staffer@example.com", is_staff=True, is_superuser=True)
        response = logged_in(staff).get("/browse/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("profile")))

    def test_the_page_shows_the_notice_apart_from_the_questions(self):
        # The notice of what is recorded sits in its own box before the form,
        # and names each kind of record concretely.
        user = make_user("reader", "reader@example.com", loop_approved=True)
        page = logged_in(user).get(reverse("profile")).content.decode()
        notice = page[page.index('<aside class="usage-notice'):page.index("</aside>")]
        for words in ("What we record", "CHAOS-Agent conversations", "IP address", "life of the projects plus five years"):
            self.assertIn(words, notice)
        self.assertLess(page.index("</aside>"), page.index('<form method="post" novalidate>'))
        self.assertIn("What will you use LOOP or CHAOS for?", page)
        self.assertIn("I have read the notice above describing what is recorded.", page)

    def test_saving_the_profile_records_the_notice_and_lets_the_account_through(self):
        user = make_user("filler", "filler@example.com", loop_approved=True)
        client = logged_in(user)
        self.assertEqual(client.get(reverse("profile")).status_code, 200)

        response = client.post(
            reverse("profile") + "?next=/no-such-page/",
            GOOD,
            HTTP_CF_CONNECTING_IP="203.0.113.5",
            HTTP_USER_AGENT="profile-test",
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/no-such-page/")
        profile = Profile.objects.get(user=user)
        self.assertEqual(profile.institution, "Johns Hopkins University")
        self.assertEqual(profile.ror_id, "https://ror.org/00za53h95")
        self.assertEqual(profile.ror_type, "education")
        self.assertEqual(profile.country, "US")
        self.assertEqual(profile.research_areas, ["high_entropy", "ml"])
        self.assertEqual(profile.orcid, "0000-0002-1825-0097")
        self.assertTrue(profile.contact_ok)
        notice = TermsAcceptance.objects.get(user=user, document="usage")
        self.assertEqual(notice.version, "notice-1")
        self.assertEqual(notice.ip, "203.0.113.5")
        self.assertTrue(policy.profile_complete(user))
        self.assertTrue(not_sent_to_profile(client.get("/no-such-page/")))

    def test_incomplete_or_wrong_answers_are_refused(self):
        user = make_user("sloppy", "sloppy@example.com")
        client = logged_in(user)
        for field, value in (
            ("accept_usage_notice", None),
            ("orcid", "0000-0002-1825-0098"),
            ("intended_use", "data"),
            ("research_areas", None),
            ("country", "XX"),
        ):
            data = dict(GOOD)
            if value is None:
                data.pop(field)
            else:
                data[field] = value
            response = client.post(reverse("profile"), data)
            self.assertEqual(response.status_code, 200, field)
            self.assertIn(field, response.context["form"].errors, field)
        self.assertFalse(Profile.objects.filter(user=user).exists())
        self.assertFalse(policy.profile_complete(user))

    def test_a_ror_id_is_kept_only_when_well_formed(self):
        user = make_user("rorcheck", "rorcheck@example.com")
        data = dict(GOOD, ror_id="javascript:alert(1)", ror_type="education")
        logged_in(user).post(reverse("profile"), data)
        profile = Profile.objects.get(user=user)
        self.assertEqual(profile.ror_id, "")
        self.assertEqual(profile.ror_type, "")

    def test_next_must_stay_on_this_site(self):
        user = make_user("redirected", "redirected@example.com")
        response = logged_in(user).post(reverse("profile") + "?next=https://evil.example/", GOOD)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response["Location"].startswith("https://evil.example"))

    def test_a_new_notice_version_asks_again_with_the_answers_filled_in(self):
        user = make_user("returning", "returning@example.com", loop_approved=True)
        complete_profile(user)
        client = logged_in(user)
        self.assertTrue(not_sent_to_profile(client.get("/no-such-page/")))
        with self.settings(USAGE_NOTICE_VERSION="notice-2"):
            response = client.get("/no-such-page/")
            self.assertTrue(response["Location"].startswith(reverse("profile")))
            page = client.get(reverse("profile"))
            self.assertEqual(page.context["form"].initial["institution"], "Example University")

    def test_the_older_json_endpoints_answer_403_with_the_profile_address(self):
        user = make_user("jsonuser", "jsonuser@example.com", loop_approved=True)
        response = logged_in(user).get("/api/precursors/")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["profile_url"].endswith(reverse("profile")))

    def test_an_api_key_is_refused_until_its_owner_has_a_profile(self):
        user = make_user("keyowner", "keyowner@example.com", loop_approved=True)
        _, raw = APIKey.issue(user=user, name="script", scopes=["data:read"])
        before = Client().get("/api/v1/me/", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(before.status_code, 403)
        self.assertIn("profile_url", before.json())
        complete_profile(user)
        after = Client().get("/api/v1/me/", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(after.status_code, 200)

    def test_every_way_of_sending_a_key_is_checked(self):
        # The versioned API also takes "Authorization: ApiKey <key>"; an
        # earlier version of the middleware did not read it, and a key owner
        # without a profile got through.
        user = make_user("apikeyword", "apikeyword@example.com", loop_approved=True)
        _, raw = APIKey.issue(user=user, name="script", scopes=["data:read"])
        for headers in (
            {"HTTP_AUTHORIZATION": f"ApiKey {raw}"},
            {"HTTP_AUTHORIZATION": f"Token {raw}"},
            {"HTTP_X_API_KEY": raw},
        ):
            response = Client().get("/api/v1/me/", **headers)
            self.assertEqual(response.status_code, 403, headers)

    def test_a_key_is_checked_even_with_a_signed_in_session(self):
        # A signed-in account with a profile must not carry in the key of an
        # account without one.
        holder = make_user("holder", "holder@example.com", loop_approved=True)
        complete_profile(holder)
        other = make_user("keylender", "keylender@example.com", loop_approved=True)
        _, raw = APIKey.issue(user=other, name="lent", scopes=["data:read"])
        response = logged_in(holder).get("/api/v1/me/", HTTP_AUTHORIZATION=f"Bearer {raw}")
        self.assertEqual(response.status_code, 403)

    def test_the_access_endpoint_reports_the_profile_state(self):
        staff = make_user("reporter", "reporter@example.com", is_staff=True)
        client = logged_in(staff)
        data = client.get("/api/v1/access/").json()["data"]
        self.assertFalse(data["profile_complete"])
        self.assertFalse(data["chaos"])
        self.assertFalse(data["loop"])
        self.assertTrue(data["profile_url"].endswith(reverse("profile")))
        complete_profile(staff)
        data = client.get("/api/v1/access/").json()["data"]
        self.assertTrue(data["profile_complete"])
        self.assertTrue(data["chaos"])
        self.assertTrue(data["loop"])

    def test_the_requirement_can_be_switched_off(self):
        user = make_user("switched", "switched@example.com", loop_approved=True)
        with self.settings(PROFILE_REQUIRED=False):
            client = logged_in(user)
            self.assertTrue(not_sent_to_profile(client.get("/no-such-page/")))
            # ... and the CHAOS gate and kiosk follow the switch
            data = client.get("/api/v1/access/").json()["data"]
            self.assertTrue(data["profile_complete"])
            self.assertFalse(data["profile_required"])
            self.assertTrue(data["loop"])

    def test_a_pasted_orcid_address_is_accepted(self):
        user = make_user("orcidurl", "orcidurl@example.com")
        logged_in(user).post(reverse("profile"), dict(GOOD, orcid="https://orcid.org/0000-0002-1825-0097"))
        self.assertEqual(Profile.objects.get(user=user).orcid, "0000-0002-1825-0097")


@override_settings(**PROFILE_ON)
class SignOutEveryoneTests(TestCase):
    def test_every_session_goes(self):
        logged_in(make_user("one", "one@example.com"))
        logged_in(make_user("two", "two@example.com"))
        self.assertGreaterEqual(Session.objects.count(), 2)
        with self.assertRaises(CommandError):
            call_command("signout_everyone")
        call_command("signout_everyone", "--yes")
        self.assertEqual(Session.objects.count(), 0)


class UsageRecordTests(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings_override = override_settings(**dict(COMMON, USAGE_DIR=self.dir))
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.dir, ignore_errors=True)

    def lines(self):
        folder = os.path.join(self.dir, "loop")
        out = []
        for name in sorted(os.listdir(folder)):
            with open(os.path.join(folder, name)) as fh:
                out += [json.loads(line) for line in fh if line.strip()]
        return out

    def test_a_signed_in_request_is_recorded_with_the_address_and_no_secrets(self):
        user = make_user("recorded", "recorded@example.com", loop_approved=True)
        logged_in(user).get(
            "/no-such-page/?species=Fe&keyword=oxide&token=abc123&api_key=zzz",
            HTTP_CF_CONNECTING_IP="203.0.113.7",
            HTTP_USER_AGENT="usage-test",
            HTTP_REFERER="https://s4e.ai/loop/accounts/reset/MQ/abc-secret/?next=/x&csrf_token=t",
        )
        line = self.lines()[-1]
        self.assertEqual(line["tool"], "loop")
        self.assertEqual(line["account"], "recorded")
        self.assertEqual(line["user_id"], user.pk)
        self.assertEqual(line["via"], "session")
        self.assertEqual(line["ip"], "203.0.113.7")
        self.assertEqual(line["ua"], "usage-test")
        self.assertEqual(line["method"], "GET")
        self.assertEqual(line["path"], "/no-such-page/")
        self.assertEqual(line["q"], "species=Fe&keyword=oxide&token=***&api_key=***")
        self.assertEqual(line["referer"], "https://s4e.ai/loop/accounts/reset/MQ/***/?next=/x&csrf_token=***")
        self.assertNotIn("internal", line)
        self.assertIn("status", line)
        self.assertRegex(line["t"], r"^\d{4}-\d{2}-\d{2}T")

    def test_an_api_key_request_records_the_key_prefix(self):
        user = make_user("scripted", "scripted@example.com", loop_approved=True)
        credential, raw = APIKey.issue(user=user, name="script", scopes=["data:read"])
        Client().get("/api/v1/access/", HTTP_AUTHORIZATION=f"Bearer {raw}")
        line = self.lines()[-1]
        self.assertEqual(line["via"], "key")
        self.assertEqual(line["key"], credential.prefix)
        self.assertEqual(line["account"], "scripted")
        self.assertNotIn(raw, json.dumps(line))

    def test_the_chaos_sign_in_checks_are_marked_internal(self):
        user = make_user("checked", "checked@example.com", loop_approved=True)
        logged_in(user).get("/api/v1/access/", HTTP_USER_AGENT="chaos-kiosk access check")
        self.assertTrue(self.lines()[-1]["internal"])

    def test_reset_links_are_recorded_without_their_token(self):
        Client().get("/accounts/reset/MQ/abc-123def/")
        line = self.lines()[-1]
        self.assertEqual(line["path"], "/accounts/reset/MQ/***/")
        self.assertNotIn("account", line)
        self.assertEqual(line["via"], "none")

    def test_nothing_is_written_without_a_usage_folder(self):
        with self.settings(USAGE_DIR=""):
            Client().get("/no-such-page/")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "loop")))

    def test_a_failed_write_does_not_break_the_page(self):
        blocker = os.path.join(self.dir, "not-a-folder")
        with open(blocker, "w") as fh:
            fh.write("x")
        with self.settings(USAGE_DIR=blocker):
            response = Client().get(reverse("chaos_terms"))
        self.assertEqual(response.status_code, 200)


@override_settings(**PROFILE_ON)
class AccountPageTests(TestCase):
    """LOOP's Account page (Account > Profile in the menu) shows the same
    profile, with a link to edit it, and what the account may open."""

    def page(self, user):
        with mock.patch("catalog.views._user_affiliations", return_value=["S4E"]):
            response = logged_in(user).get(reverse("account"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_profile_is_shown_with_names_for_the_codes(self):
        user = make_user("shown", "shown@example.com", loop_approved=True)
        complete_profile(user)
        page = self.page(user)
        for words in ("Example University", "United States", "Faculty", "University or college",
                      "Machine learning and data science", "Teaching and research on oxides."):
            self.assertIn(words, page)
        self.assertIn(f'href="{reverse("profile")}?next={reverse("account")}"', page)
        self.assertIn("Edit profile", page)

    def test_access_is_shown_with_a_way_to_ask_for_chaos(self):
        user = make_user("noaccess", "noaccess@example.com", loop_approved=True)
        complete_profile(user)
        page = self.page(user)
        self.assertIn("Approved", page)
        self.assertIn(reverse("chaos_access"), page)

    @override_settings(PROFILE_REQUIRED=False)
    def test_without_a_profile_the_page_offers_to_complete_it(self):
        user = make_user("empty", "empty@example.com", loop_approved=True)
        page = self.page(user)
        self.assertIn("Complete your profile", page)

    def test_saving_the_profile_from_there_returns_there(self):
        user = make_user("editor", "editor@example.com", loop_approved=True)
        complete_profile(user)
        response = logged_in(user).post(reverse("profile") + "?next=" + reverse("account"), GOOD)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("account"))
