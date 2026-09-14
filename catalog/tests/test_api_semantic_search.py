"""Semantic retrieval authorization and API contracts without a live model/index."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from catalog.api.search import semantic_search
from catalog.documents import Material, Recipe
from catalog.models import APIKey
from catalog.vector_search import VECTOR_FIELDS


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class SemanticSearchTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(pk=731, is_authenticated=True, is_staff=True)
        self.visible = SimpleNamespace(
            id="M:000000000001", elements={"Ni": 1, "O": 1}, element_symbols=["Ni", "O"],
            num_elements=2, structure_family="rocksalt", display_name="NiO", notes="",
            curator="", created_at=None, updated_at=None,
            default_visibility_affiliations=["APL"], dft_calculations=[],
        )
        self.other = SimpleNamespace(**{**vars(self.visible), "id": "M:000000000002",
                                       "default_visibility_affiliations": ["Oak Ridge"]})
        self.recipe = SimpleNamespace(id="M:000000000001:R:000000000003",
                                     material_auid=self.visible.id,
                                     visibility_affiliations=["Oak Ridge"])
        self.collection = MagicMock()
        self.collection.list_search_indexes.return_value = [
            {"name": name, "queryable": True} for _, name in VECTOR_FIELDS
        ]
        self.collection.aggregate.return_value = [
            {"scope": "material", "material_auid": self.visible.id, "_score": .7},
            {"scope": "recipe", "material_auid": self.visible.id,
             "recipe_auid": self.recipe.id, "_score": .99},
            {"scope": "material", "material_auid": self.other.id, "_score": .98},
        ]
        db = MagicMock()
        db.__getitem__.return_value = self.collection
        self.patches = [
            patch("catalog.api.search._user_affiliations", return_value=["APL"]),
            patch("catalog.vector_search.get_db", return_value=db),
            patch("catalog.vector_search.embeddings_mod.embed_text", return_value=[.1, .2]),
            patch.object(Material, "objects", side_effect=lambda **kw: SimpleNamespace(
                first=lambda: {self.visible.id: self.visible, self.other.id: self.other}.get(kw.get("id")))),
            patch.object(Recipe, "objects", return_value=SimpleNamespace(first=lambda: self.recipe)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def request(self, params=None, *, authenticated=True):
        request = self.factory.get("/api/v1/search/", params or {"q": "quenched nickel oxide"})
        if authenticated:
            force_authenticate(request, user=self.user)
        return semantic_search(request)

    def test_private_material_and_recipe_cannot_affect_results_or_scores(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["data"]), 1)
        hit = response.data["data"][0]
        self.assertEqual(hit["similarity_score"], .7)
        self.assertEqual(hit["matched_records"], [{"scope": "material"}])
        self.assertFalse(response.data["meta"]["complete"])

    def test_exact_filters_are_applied_before_ranking(self):
        for params in ({"q": "oxide", "elements": "Co,O"}, {"q": "oxide", "structure_family": "spinel"}):
            response = self.request(params)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["data"], [])

    def test_private_computational_score_is_excluded(self):
        cid = self.visible.id + ":C:000000000004"
        self.visible.dft_calculations = [SimpleNamespace(comp_auid=cid, visibility_affiliations=["Oak Ridge"])]
        self.collection.aggregate.return_value = [
            {"scope": "comp", "material_auid": self.visible.id, "comp_auid": cid, "_score": .99}
        ]
        self.assertEqual(self.request().data["data"], [])

    def test_unknown_and_orphan_scopes_are_excluded(self):
        self.collection.aggregate.return_value = [
            {"scope": "invalid", "material_auid": self.visible.id, "_score": .99},
            {"scope": "material", "material_auid": "M:missing", "_score": .99},
        ]
        self.assertEqual(self.request().data["data"], [])

    def test_missing_indexes_returns_503_instead_of_empty_success(self):
        self.collection.list_search_indexes.return_value = []
        response = self.request()
        self.assertEqual(response.status_code, 503)
        self.assertIn("/materials/", response.data["detail"])

    def test_nonsense_query_can_return_honest_empty_results(self):
        self.collection.aggregate.return_value = [
            {"scope": "material", "material_auid": self.visible.id, "_score": .2}
        ]
        self.assertEqual(self.request().data["data"], [])

    def test_bad_inputs_and_unsupported_pagination_rejected(self):
        for params in ({"q": ""}, {"q": "x", "limit": "nan"}, {"q": "x", "limit": "101"},
                       {"q": "x", "offset": "100"}, {"q": "x", "structure_family": "typo"}):
            self.assertEqual(self.request(params).status_code, 400)

    def test_authentication_is_required(self):
        self.assertIn(self.request(authenticated=False).status_code, (401, 403))

    def test_api_key_requires_data_read_scope(self):
        request = self.factory.get("/api/v1/search/", {"q": "nickel oxide"})
        force_authenticate(request, user=self.user, token=APIKey(scopes=["data:write"]))
        self.assertEqual(semantic_search(request).status_code, 403)
