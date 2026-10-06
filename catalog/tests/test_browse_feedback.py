"""Regression cases from researcher feedback, backed by disposable MongoDB."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from types import SimpleNamespace

from django.test import SimpleTestCase, RequestFactory, override_settings, tag

from catalog import aggregation, browse_search, views
from catalog.documents import Material, Recipe, EmbeddedTrial, EmbeddedLiterature, EmbeddedDFT, ExpCondition


class FormulaTests(SimpleTestCase):
    def test_composition_and_coefficients(self):
        self.assertEqual(browse_search.parse_formula('AlCoCuFeZn'),
                         ({'Al': 1., 'Co': 1., 'Cu': 1., 'Fe': 1., 'Zn': 1.}, False))
        self.assertEqual(browse_search.parse_formula('Fe2O3'), ({'Fe': 2., 'O': 3.}, True))

    def test_identifiers_and_prose_are_not_formulas(self):
        for value in ('Oses 92', 'oxide', 'ball milled oxides', 'XxFe', 'Fe0O', 'Fe2O3 junk'):
            with self.subTest(value=value):
                self.assertIsNone(browse_search.parse_formula(value))

    def test_missing_dates_last_and_stable_ties(self):
        now = datetime(2026, 1, 1)
        rows = [{'material_auid': 'z'}, {'material_auid': 'b', 'created_at': now},
                {'material_auid': 'a', 'created_at': now.replace(tzinfo=timezone.utc)}]
        for direction in ('oldest', 'newest'):
            browse_search.sort_by_added(rows, direction)
            self.assertEqual([r['material_auid'] for r in rows], ['a', 'b', 'z'])


@tag('mongo')
@override_settings(ARCHIVE_ENABLED=False, EMBEDDINGS_ON_WRITE=False, CHEMSCREEN_AUTOTRAIN_ENABLED=False,
                   S4E_SHELL_URL='')
class BrowseFeedbackTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.prefix = 'M:feedback'
        self.old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.new = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.addCleanup(lambda: Recipe.objects(material_auid__startswith=self.prefix).delete())
        self.addCleanup(lambda: Material.objects(id__startswith=self.prefix).delete())
        self.material('a', {'Al': 1, 'Co': 1, 'Cu': 1, 'Fe': 1, 'Zn': 1}, self.old)
        self.material('b', {'Fe': 2, 'O': 3}, self.new)
        self.material('c', {'Al': 1, 'Co': 1, 'Cu': 1, 'Fe': 1, 'Zn': 1, 'O': 5}, self.old + timedelta(days=1))
        self.recipe('a', 'one', [self.trial('t1', 'Oses 92'), self.trial('t2', 'Oses 920')])
        self.recipe('a', 'two', [self.trial('t3', 'Other')])
        self.recipe('b', 'one', [self.trial('private', 'Hidden 92', ['MIT'])])

    def material(self, suffix, elements, created):
        return Material(id=self.prefix + suffix, elements=elements, element_symbols=list(elements),
                        num_elements=len(elements), structure_family='rocksalt', created_at=created,
                        latest_trial_date=self.new if suffix == 'a' else self.old).save()

    def trial(self, trial_id, batch, visibility=None):
        return EmbeddedTrial(trial_id=trial_id, trial_date=self.old, created_at=self.old,
                             visibility_affiliations=visibility or ['APL'],
                             exp_condition=ExpCondition(additional_params={'source_batch_id': batch}))

    def recipe(self, suffix, name, trials=None, literature=None):
        material = Material.objects.get(id=self.prefix + suffix)
        return Recipe(id=material.id + ':R:' + name, material_auid=material.id,
                      elements=material.elements, element_symbols=material.element_symbols,
                      num_elements=material.num_elements, structure_family='rocksalt',
                      synthesis_steps=[{'step_type': 'other', 'description': name}],
                      trials=trials or [], literature=literature or [],
                      visibility_affiliations=['APL'], created_at=self.old).save()

    def browse(self, **params):
        request = self.factory.get('/browse/', params)
        request.user = SimpleNamespace(is_authenticated=True)
        with patch.object(views, '_user_affiliations', return_value=['APL']), patch.object(views, 'render', side_effect=lambda req, tpl, context: context):
            return views.browse_data(request)

    def test_composition_exact_element_set_without_semantic(self):
        with patch.object(views.vector_search_mod, 'semantic_material_auids') as semantic:
            result = self.browse(search='AlCoCuFeZn')
        semantic.assert_not_called()
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['composition_rows'][0]['material_auid'], self.prefix + 'a')
        self.assertEqual(result['search_mode'], 'composition')

    def test_ratios_and_no_match(self):
        self.assertEqual(browse_search.catalog_matches('Fe4O6', ['APL']).materials, {self.prefix + 'b'})
        self.assertEqual(browse_search.catalog_matches('FeO', ['APL']).materials, {self.prefix + 'b'})
        self.assertEqual(browse_search.catalog_matches('Fe3O4', ['APL']).materials, set())
        self.assertEqual(self.browse(search='HfTa')['total'], 0)

    def test_batch_exact_and_only_matching_recipe(self):
        result = self.browse(search='oses 92', view='recipes')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['composition_rows'][0]['recipe_auid'], self.prefix + 'a:R:one')
        self.assertEqual(self.browse(search='Oses 9')['total'], 0)

    def test_identifier_stays_exact_in_semantic_mode(self):
        with patch.object(views.vector_search_mod, 'semantic_material_auids') as semantic:
            self.assertEqual(self.browse(search='Oses 92', search_type='semantic')['total'], 1)
            self.assertEqual(self.browse(search='Missing 999', search_type='semantic')['total'], 0)
        semantic.assert_not_called()

    def test_hidden_batch_does_not_reveal_parent(self):
        self.assertEqual(self.browse(search='Hidden 92')['total'], 0)
        self.assertEqual(self.browse(search='.*')['total'], 0)

    def test_database_sorts_before_pagination_not_trial_date(self):
        for direction, expected in [('newest', 'b'), ('oldest', 'a')]:
            result = self.browse(sort=direction, per_page=1)
            self.assertEqual(result['composition_rows'][0]['material_auid'], self.prefix + expected)
            self.assertEqual(result['total'], 3)
            second = self.browse(sort=direction, per_page=1, page=2)
            self.assertEqual(second['composition_rows'][0]['material_auid'], self.prefix + 'c')

    def test_filtered_sort_and_negative_page(self):
        result = self.browse(sort='oldest', has_experiments='true', per_page=1, page=-2)
        self.assertEqual(result['page'], 1)
        self.assertEqual(result['composition_rows'][0]['material_auid'], self.prefix + 'a')

    def test_semantic_opt_in_and_outage_are_explicit(self):
        with patch.object(views.vector_search_mod, 'semantic_material_auids', return_value=[(self.prefix+'b', .8), (self.prefix+'a', .7)]):
            result = self.browse(search='quenched oxides', search_type='semantic')
        self.assertEqual(result['search_mode'], 'semantic')
        self.assertEqual(result['composition_rows'][0]['material_auid'], self.prefix+'b')
        with patch.object(views.vector_search_mod, 'semantic_material_auids', side_effect=views.vector_search_mod.VectorSearchUnavailable('internal error')):
            result = self.browse(search='quenched oxides', search_type='semantic')
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['search_mode'], 'unavailable')
        self.assertNotIn('internal error', result['semantic_error'])

    def test_child_dates_and_literal_doi_dont_expand_to_siblings(self):
        lit1 = EmbeddedLiterature(doi='10.1234/one', lit_id='L:one', title='Test paper one', created_at=self.new, visibility_affiliations=['APL'])
        lit2 = EmbeddedLiterature(doi='10.1234/two', lit_id='L:two', title='Test paper two', created_at=self.old, visibility_affiliations=['APL'])
        self.recipe('c', 'papers', literature=[lit1, lit2])
        result = self.browse(view='literature', sort='newest', per_page=1)
        self.assertEqual(result['composition_rows'][0]['doi'], lit1.doi)
        result = self.browse(view='literature', search=lit2.doi)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['composition_rows'][0]['doi'], lit2.doi)
        material = Material.objects.get(id=self.prefix+'c')
        material.dft_calculations = [EmbeddedDFT(comp_auid='C:new', created_at=self.new, visibility_affiliations=['APL']), EmbeddedDFT(comp_auid='C:old', created_at=self.old, visibility_affiliations=['APL'])]
        material.save()
        result = self.browse(view='computational', sort='oldest', per_page=1)
        self.assertEqual(result['composition_rows'][0]['comp_auid'], 'C:old')

    def test_home_links_and_rendered_controls(self):
        from django.template.loader import render_to_string
        html = render_to_string('index.html', {})
        self.assertIn('?view=recipes&amp;has_experiments=true', html)
        self.assertIn('?view=literature&amp;has_literature=true', html)
        self.assertIn('?view=computational&amp;has_computational=true', html)
        self.assertLess(html.index('Catalog holdings'), html.index('Discover related materials'))
        request = self.factory.get('/browse/')
        context = self.browse()
        html = render_to_string('catalog/browse_data.html', context, request=request)
        self.assertIn('Date added: oldest first', html)
        self.assertNotIn('Advanced filters', html)
        self.assertIn('2025-12-31', html)
