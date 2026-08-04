import unittest
import uuid

from django.test import Client, SimpleTestCase, TestCase

from catalog.services import bulk_literature_delete as bulk
from catalog.tests.mongo_guard import mongo_reachable as _mongo_reachable


def _make_recipe(entries):
    """A minimal but valid Recipe holding the given literature entries.

    id, elements and structure_family are all required by the document, and the
    id is a real recipe auid so the composite "M:...:R:..." shape is exercised.
    """
    from catalog.documents import (
        EmbeddedLiterature, Recipe, compute_material_auid, compute_recipe_auid,
    )
    from catalog.services.batch_experiment_upload import parse_composition_formula

    elements = parse_composition_formula("(Co0.5Ni0.5)O")
    auid = compute_material_auid(elements, "rocksalt")
    steps = [{"step_number": 1, "step_type": "other",
              "description": f"bulk-delete test {uuid.uuid4().hex[:8]}"}]
    recipe = Recipe(
        id=compute_recipe_auid(auid, steps),
        material_auid=auid,
        elements=elements,
        element_symbols=sorted(elements),
        num_elements=len(elements),
        structure_family="rocksalt",
        synthesis_steps=steps,
        literature=[EmbeddedLiterature(**e) for e in entries],
    )
    recipe.save()
    return recipe, auid


def _drop(auid):
    from catalog.documents import Material, Recipe
    Recipe.objects(material_auid=auid).delete()
    Material.objects(id=auid).delete()


class _FakeUser:
    is_authenticated = True

    def __init__(self, username, superuser=False):
        self.username = username
        self.is_superuser = superuser

    def get_username(self):
        return self.username


class ParseSelectionTests(SimpleTestCase):
    def test_parses_pairs(self):
        pairs = bulk.parse_selection(["M:a:R:b|L:1", "M:c:R:d|L:2"])
        self.assertEqual(pairs, [("M:a:R:b", "L:1"), ("M:c:R:d", "L:2")])

    def test_recipe_ids_containing_colons_survive(self):
        # Recipe ids look like "M:<hash>:R:<hash>", so ':' cannot be the delimiter.
        pairs = bulk.parse_selection(["M:cf4daabb9365:R:34762c3136dc|L:abc123"])
        self.assertEqual(pairs, [("M:cf4daabb9365:R:34762c3136dc", "L:abc123")])

    def test_malformed_tokens_are_dropped_not_raised(self):
        # A hand-edited form must not 500.
        self.assertEqual(bulk.parse_selection(["", "garbage", "|", "a|", "|b", None]), [])

    def test_duplicates_collapse(self):
        pairs = bulk.parse_selection(["R1|L1", "R1|L1", "R1|L2"])
        self.assertEqual(pairs, [("R1", "L1"), ("R1", "L2")])

    def test_token_round_trips(self):
        row = bulk.LiteratureRow(
            recipe_id="M:x:R:y", lit_id="L:z", doi="10.1/a", title="t",
            journal="j", year=2024, material_auid="M:x", extracted_by="u",
        )
        self.assertEqual(bulk.parse_selection([row.token]), [("M:x:R:y", "L:z")])


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class BulkDeleteIntegrationTests(SimpleTestCase):
    """Exercises the real embedded-document writes, not a mock."""

    def _make_recipe(self, entries):
        return _make_recipe(entries)

    def _cleanup(self, auid):
        _drop(auid)

    def test_deletes_only_selected_and_only_own_records(self):
        mine, theirs = f"10.1/{uuid.uuid4().hex[:6]}", f"10.2/{uuid.uuid4().hex[:6]}"
        keep = f"10.3/{uuid.uuid4().hex[:6]}"
        recipe, auid = self._make_recipe([
            {"lit_id": "L:mine",  "doi": mine,  "extracted_by": "alice"},
            {"lit_id": "L:their", "doi": theirs, "extracted_by": "bob"},
            {"lit_id": "L:keep",  "doi": keep,  "extracted_by": "alice"},
        ])
        try:
            rid = str(recipe.id)
            alice = _FakeUser("alice")

            # Alice selects her own record and Bob's.
            result = bulk.execute_plan(alice, [(rid, "L:mine"), (rid, "L:their")])
            self.assertEqual(result["removed"], 1)
            self.assertEqual(len(result["denied"]), 1)

            from catalog.documents import Recipe
            left = {l.lit_id for l in Recipe.objects(id=rid).first().literature}
            self.assertEqual(left, {"L:their", "L:keep"})   # Bob's survived
        finally:
            self._cleanup(auid)

    def test_superuser_may_delete_another_users_record(self):
        doi = f"10.4/{uuid.uuid4().hex[:6]}"
        recipe, auid = self._make_recipe([
            {"lit_id": "L:x", "doi": doi, "extracted_by": "bob"},
        ])
        try:
            rid = str(recipe.id)
            result = bulk.execute_plan(_FakeUser("root", superuser=True), [(rid, "L:x")])
            self.assertEqual(result["removed"], 1)
            self.assertEqual(result["denied"], [])
        finally:
            self._cleanup(auid)

    def test_same_doi_siblings_are_reported_before_deleting(self):
        # Deletion matches DOI as well as lit_id, inherited from delete_literature,
        # so selecting one row can remove two. The plan must say so up front.
        doi = f"10.5/{uuid.uuid4().hex[:6]}"
        recipe, auid = self._make_recipe([
            {"lit_id": "L:a", "doi": doi, "extracted_by": "alice"},
            {"lit_id": "L:b", "doi": doi.upper(), "extracted_by": "alice"},
        ])
        try:
            rid = str(recipe.id)
            alice = _FakeUser("alice")

            plan = bulk.build_plan(alice, [(rid, "L:a")])
            self.assertEqual(len(plan.rows), 1)
            self.assertEqual(len(plan.cascade), 1)      # surfaced, not hidden
            self.assertEqual(plan.total_removed, 2)

            result = bulk.execute_plan(alice, [(rid, "L:a")])
            self.assertEqual(result["removed"], 2)
        finally:
            self._cleanup(auid)

    def test_one_save_per_recipe_regardless_of_record_count(self):
        doi1, doi2, doi3 = (f"10.6/{uuid.uuid4().hex[:6]}" for _ in range(3))
        recipe, auid = self._make_recipe([
            {"lit_id": "L:1", "doi": doi1, "extracted_by": "alice"},
            {"lit_id": "L:2", "doi": doi2, "extracted_by": "alice"},
            {"lit_id": "L:3", "doi": doi3, "extracted_by": "alice"},
        ])
        try:
            rid = str(recipe.id)
            result = bulk.execute_plan(
                _FakeUser("alice"), [(rid, "L:1"), (rid, "L:2"), (rid, "L:3")]
            )
            self.assertEqual(result["removed"], 3)
            self.assertEqual(result["recipes_touched"], 1)
        finally:
            self._cleanup(auid)

    def test_missing_records_are_reported_not_fatal(self):
        doi = f"10.7/{uuid.uuid4().hex[:6]}"
        recipe, auid = self._make_recipe([
            {"lit_id": "L:real", "doi": doi, "extracted_by": "alice"},
        ])
        try:
            rid = str(recipe.id)
            result = bulk.execute_plan(
                _FakeUser("alice"), [(rid, "L:real"), (rid, "L:ghost"), ("M:nope:R:nope", "L:x")]
            )
            self.assertEqual(result["removed"], 1)
            self.assertEqual(len(result["missing"]), 2)
        finally:
            self._cleanup(auid)

    def test_list_user_literature_filters_by_uploader(self):
        d1, d2 = f"10.8/{uuid.uuid4().hex[:6]}", f"10.9/{uuid.uuid4().hex[:6]}"
        recipe, auid = self._make_recipe([
            {"lit_id": "L:a", "doi": d1, "extracted_by": "alice"},
            {"lit_id": "L:b", "doi": d2, "extracted_by": "bob"},
        ])
        try:
            dois = {r.doi for r in bulk.list_user_literature("alice")}
            self.assertIn(d1, dois)
            self.assertNotIn(d2, dois)

            all_dois = {r.doi for r in bulk.list_user_literature("alice", include_all=True)}
            self.assertIn(d2, all_dois)
        finally:
            self._cleanup(auid)

    def test_uploader_match_is_case_insensitive(self):
        doi = f"10.10/{uuid.uuid4().hex[:6]}"
        recipe, auid = self._make_recipe([
            {"lit_id": "L:a", "doi": doi, "extracted_by": "Alice"},
        ])
        try:
            rows = bulk.list_user_literature("alice")
            self.assertIn(doi, {r.doi for r in rows})
            result = bulk.execute_plan(_FakeUser("alice"), [(str(recipe.id), "L:a")])
            self.assertEqual(result["removed"], 1)
        finally:
            self._cleanup(auid)


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class BulkDeleteViewTests(TestCase):
    """Drives the real pages. TestCase (not SimpleTestCase) because logging in
    needs the auth database; Mongo is not managed by Django so it is cleaned up
    by hand."""

    def setUp(self):
        from django.conf import settings
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group
        U = get_user_model()
        # ApprovedGateMiddleware redirects anyone outside the approved group away
        # from protected pages, so test users must be in it or every request 302s.
        approved, _ = Group.objects.get_or_create(
            name=getattr(settings, "APPROVED_GROUP_NAME", "Approved"))
        self.alice = U.objects.create_user("alice", "a@example.com", "pw-alice-123")
        self.bob = U.objects.create_user("bob", "b@example.com", "pw-bob-123")
        self.alice.groups.add(approved)
        self.bob.groups.add(approved)
        self.d1 = f"10.21/{uuid.uuid4().hex[:6]}"
        self.d2 = f"10.22/{uuid.uuid4().hex[:6]}"
        self.d3 = f"10.23/{uuid.uuid4().hex[:6]}"
        self.recipe, self.auid = _make_recipe([
            {"lit_id": "L:a", "doi": self.d1, "title": "Paper A", "extracted_by": "alice"},
            {"lit_id": "L:b", "doi": self.d2, "title": "Paper B", "extracted_by": "alice"},
            {"lit_id": "L:c", "doi": self.d3, "title": "Paper C", "extracted_by": "bob"},
        ])
        self.rid = str(self.recipe.id)
        # secure=True because DEBUG is False here, so SECURE_SSL_REDIRECT turns
        # every plain-HTTP request into a 302 before it reaches a view.
        self.client = Client(secure=True)
        self.client.login(username="alice", password="pw-alice-123")

    def tearDown(self):
        _drop(self.auid)

    def _remaining(self):
        from catalog.documents import Recipe
        return {l.lit_id for l in Recipe.objects(id=self.rid).first().literature}

    def test_page_lists_only_own_records(self):
        html = self.client.get("/my/literature/").content.decode()
        self.assertEqual(html.count('name="selected"'), 2)
        self.assertIn(self.d1, html)
        self.assertNotIn(self.d3, html)          # bob's is not offered
        self.assertIn('id="check-all"', html)
        self.assertIn('id="bulk-delete" disabled', html)   # nothing selected yet

    def test_anonymous_is_redirected(self):
        self.assertEqual(Client(secure=True).get("/my/literature/").status_code, 302)

    def test_get_cannot_delete(self):
        self.assertEqual(self.client.get("/my/literature/delete/").status_code, 405)

    def test_first_post_confirms_and_writes_nothing(self):
        r = self.client.post("/my/literature/delete/",
                             {"selected": [f"{self.rid}|L:a", f"{self.rid}|L:c"]})
        html = r.content.decode()
        self.assertIn("Confirm deletion", html)
        self.assertIn("1 record will be permanently deleted", html)
        self.assertIn("uploaded by someone else", html)     # bob's is reported skipped
        self.assertEqual(self._remaining(), {"L:a", "L:b", "L:c"})

    def test_confirmed_post_deletes_only_permitted(self):
        r = self.client.post("/my/literature/delete/",
                             {"selected": [f"{self.rid}|L:a", f"{self.rid}|L:c"],
                              "confirm": "yes"}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._remaining(), {"L:b", "L:c"})

    def test_another_user_cannot_delete_via_crafted_post(self):
        # bob posts alice's record id directly, bypassing the listing page.
        c = Client(secure=True); c.login(username="bob", password="pw-bob-123")
        c.post("/my/literature/delete/", {"selected": [f"{self.rid}|L:b"], "confirm": "yes"})
        self.assertIn("L:b", self._remaining())

    def test_empty_selection_is_rejected(self):
        r = self.client.post("/my/literature/delete/", {}, follow=True)
        self.assertEqual(self._remaining(), {"L:a", "L:b", "L:c"})
        self.assertContains(r, "No records were selected")


class SelectionLimitTests(SimpleTestCase):
    def test_cap_is_below_djangos_field_limit(self):
        from django.conf import settings
        # One checkbox is posted per record plus CSRF, so the cap must leave
        # headroom under DATA_UPLOAD_MAX_NUMBER_FIELDS or the form 400s with an
        # unexplained TooManyFieldsSent.
        limit = getattr(settings, "DATA_UPLOAD_MAX_NUMBER_FIELDS", 1000) or 1000
        self.assertLess(bulk.MAX_SELECTION, limit)


@unittest.skipUnless(_mongo_reachable(), "MongoDB not reachable")
class SelectionLimitViewTests(TestCase):
    def setUp(self):
        from django.conf import settings
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group
        U = get_user_model()
        approved, _ = Group.objects.get_or_create(
            name=getattr(settings, "APPROVED_GROUP_NAME", "Approved"))
        self.user = U.objects.create_user("capuser", "cap@example.com", "pw-cap-12345")
        self.user.groups.add(approved)
        self.recipe, self.auid = _make_recipe([
            {"lit_id": "L:a", "doi": f"10.31/{uuid.uuid4().hex[:6]}", "extracted_by": "capuser"},
        ])
        self.client = Client(secure=True)
        self.client.login(username="capuser", password="pw-cap-12345")

    def tearDown(self):
        _drop(self.auid)

    def test_oversized_selection_is_refused_with_an_explanation(self):
        rid = str(self.recipe.id)
        too_many = [f"{rid}|L:{i}" for i in range(bulk.MAX_SELECTION + 1)]
        r = self.client.post("/my/literature/delete/", {"selected": too_many}, follow=True)
        self.assertContains(r, "at most")
        # And nothing was touched.
        from catalog.documents import Recipe
        self.assertEqual(len(Recipe.objects(id=rid).first().literature), 1)

    def test_selection_at_the_cap_is_accepted(self):
        rid = str(self.recipe.id)
        at_cap = [f"{rid}|L:a"] + [f"{rid}|L:ghost{i}" for i in range(bulk.MAX_SELECTION - 1)]
        r = self.client.post("/my/literature/delete/", {"selected": at_cap})
        self.assertNotContains(r, "at most", status_code=200)
