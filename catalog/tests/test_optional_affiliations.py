"""An account without an affiliation reads nothing, at every layer.

Since #51 the sign-up asks for no affiliation: an admin sets it at approval,
and LOOP approval needs it (access.policy.has_loop_access). These tests cover
the second guard: the record filters, downloads and predictions also treat a
missing or empty affiliation as none, never as S4E.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.test import TestCase, override_settings
from django.urls import reverse

from access import policy
from access.tests import COMMON
from catalog import aggregation, api_download, views
from catalog.documents import (
    Material, UserAffiliation, VISIBILITY_DEFAULT,
    get_user_affiliations, upsert_user_affiliations,
)
from catalog.permissions import is_visible_to_user
from catalog.prediction_table import run_prediction_query, screen_3d_transition_metal_oxides


@override_settings(**COMMON, ARCHIVE_ENABLED=False)
class OptionalAffiliationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "outside-researcher", "outside@example.edu", "Research-test-pass-23"
        )
        UserAffiliation.objects(user_id=self.user.id).delete()
        self.addCleanup(lambda: UserAffiliation.objects(user_id=self.user.id).delete())

    def test_new_signup_has_no_affiliation_and_no_access(self):
        response = self.client.post(reverse("signup"), {
            "username": "new-outside-researcher",
            "first_name": "New", "last_name": "Researcher",
            "email": "new-outside@example.edu",
            "password1": "Research-test-pass-23",
            "password2": "Research-test-pass-23",
        })
        self.assertRedirects(response, reverse("signup_pending"), fetch_redirect_response=False)
        user = get_user_model().objects.get(username="new-outside-researcher")
        self.addCleanup(lambda: UserAffiliation.objects(user_id=user.id).delete())
        self.assertEqual(get_user_affiliations(user), [])
        self.assertEqual(views._user_affiliations(user), [])
        self.assertFalse(policy.has_loop_access(user))
        self.assertFalse(policy.has_chaos_access(user))

    def test_missing_empty_and_removed_membership_never_default_to_s4e(self):
        self.assertEqual(get_user_affiliations(AnonymousUser()), [])
        self.assertEqual(get_user_affiliations(self.user), [])
        self.assertEqual(views._user_affiliations(self.user), [])
        upsert_user_affiliations(self.user, ["APL", "APL", "invalid"])
        self.assertEqual(views._user_affiliations(self.user), ["APL"])
        # Saving none removes the record (#51); reading it back gives none.
        upsert_user_affiliations(self.user, [])
        self.assertIsNone(UserAffiliation.objects(user_id=self.user.id).first())
        self.assertEqual(views._user_affiliations(self.user), [])
        # A record stored with an empty list (older data) also reads as none.
        UserAffiliation(user_id=self.user.id, username=self.user.username, affiliations=[]).save().validate()
        self.assertEqual(get_user_affiliations(self.user), [])
        self.assertEqual(views._user_affiliations(self.user), [])

    def test_default_profile_is_empty_but_data_visibility_stays_s4e(self):
        profile = UserAffiliation(user_id=self.user.id, username=self.user.username).save()
        self.assertEqual(profile.affiliations, [])
        self.assertEqual(VISIBILITY_DEFAULT, ["S4E"])
        self.assertEqual(views._get_visibility_affiliations_for_create(self.user), ["S4E"])

    def test_affiliated_users_keep_their_existing_access(self):
        for affiliation in ("S4E", "APL", "MIT"):
            with self.subTest(affiliation=affiliation):
                upsert_user_affiliations(self.user, [affiliation])
                self.assertEqual(views._user_affiliations(self.user), [affiliation])
                self.assertTrue(is_visible_to_user([affiliation], views._user_affiliations(self.user)))

    def test_unaffiliated_user_cannot_read_any_organization_visibility(self):
        for empty in (None, [], "", "[]"):
            for tags in (None, [], ["S4E"], ["APL"], ["S4E", "MIT"]):
                with self.subTest(empty=empty, tags=tags):
                    self.assertFalse(is_visible_to_user(tags, empty))

    def test_no_affiliation_browse_returns_no_rows_or_metadata(self):
        with patch("catalog.aggregation.get_db", side_effect=AssertionError("unexpected catalog read")):
            result = aggregation.browse_materials(user_affiliations=[], skip=0, limit=25)
            self.assertEqual(result.rows, [])
            self.assertEqual(result.total_count, 0)
            self.assertEqual(aggregation.browse_literature_flat(user_affiliations=[]), [])
            self.assertEqual(aggregation.browse_computational_flat(user_affiliations=[]), [])
            self.assertFalse(aggregation.composition_view("M:outside", [])['exists'])

    def test_predictions_do_not_default_empty_membership_to_s4e(self):
        for affiliations in (None, []):
            with self.subTest(affiliations=affiliations):
                self.assertEqual(screen_3d_transition_metal_oxides(user_affiliations=affiliations)["rows"], [])
                self.assertEqual(run_prediction_query("lowest conductivity", user_affiliations=affiliations)["rows"], [])

    def test_staff_without_affiliations_cannot_read_or_download_material(self):
        # Since #51 an approved account without an affiliation is refused
        # before the API (403). Staff pass that check, so a staff account
        # without one shows the second guard: the record filters.
        self.user.groups.add(Group.objects.get_or_create(name="Approved")[0])
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        material = Material(
            id="M:aa1122334455", elements={"Fe": 1}, structure_family="rocksalt",
            default_visibility_affiliations=["S4E"],
        ).save()
        self.addCleanup(material.delete)
        self.client.force_login(self.user)
        response = self.client.get(f"/api/v1/materials/{material.id}/")
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(api_download.build_composition_zip(material.id, []))
        response = self.client.get("/api/v1/materials/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], [])

    def test_verified_edu_user_can_get_chaos_without_loop_membership(self):
        upsert_user_affiliations(self.user, [])
        policy.record_verified_email(self.user)
        policy.request_chaos_access(self.user, affiliation="External University", purpose="Research")
        self.assertTrue(policy.has_chaos_access(self.user))
        self.assertFalse(policy.has_loop_access(self.user))
        self.assertEqual(views._user_affiliations(self.user), [])
        self.client.force_login(self.user)
        data = self.client.get("/api/v1/access/").json()["data"]
        self.assertTrue(data["chaos"])
        self.assertFalse(data["loop"])
