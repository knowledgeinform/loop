"""Literal catalog lookup, separate from opt-in semantic discovery."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import re

from .documents import Material, Recipe
from .permissions import is_visible_to_user, recipe_or_children_visible


@lru_cache(maxsize=1)
def _symbols():
    data = json.loads((Path(__file__).parent / 'static/js/elements.json').read_text())
    return {item['symbol'] for item in data}


def parse_formula(query):
    """Recognize complete, case-sensitive simple formulas, never partial words.

    Bare symbols describe an element set. Explicit coefficients also constrain
    relative composition. Parentheses and other formula syntax are not guessed.
    """
    tokens = re.findall(r'([A-Z][a-z]?)(\d+(?:\.\d+)?)?', query)
    if not tokens or ''.join(symbol + amount for symbol, amount in tokens) != query:
        return None
    if any(symbol not in _symbols() for symbol, _ in tokens):
        return None
    elements = {}
    for symbol, amount in tokens:
        value = float(amount or 1)
        if value <= 0:
            return None
        elements[symbol] = elements.get(symbol, 0) + value
    return elements, any(amount for _, amount in tokens)


def _same_ratios(stored, requested):
    try:
        left = {key: float(value) for key, value in stored.items()}
        total_left, total_right = sum(left.values()), sum(requested.values())
        return total_left > 0 and all(
            abs(left[key] / total_left - value / total_right) < 0.0001
            for key, value in requested.items()
        )
    except (TypeError, ValueError, KeyError):
        return False


@dataclass
class CatalogMatches:
    materials: set = field(default_factory=set)
    parent_materials: set = field(default_factory=set)
    recipes: set = field(default_factory=set)
    parent_recipes: set = field(default_factory=set)
    literature: set = field(default_factory=set)
    computations: set = field(default_factory=set)
    mode: str = 'catalog'

    def includes_recipe(self, material_id, recipe_id):
        return material_id in self.parent_materials or recipe_id in self.recipes

    def includes_literature(self, row):
        return (row.get('material_auid') in self.parent_materials
                or row.get('recipe_auid') in self.parent_recipes
                or (row.get('recipe_auid'), row.get('doi')) in self.literature)

    def includes_computation(self, row):
        return (row.get('material_auid') in self.parent_materials
                or (row.get('material_auid'), row.get('comp_auid')) in self.computations)


def catalog_matches(query, affiliations):
    """Find literal identifiers/titles using only visible embedded records.

    Identifier matches are exact (case-insensitive); paper titles use literal
    substring matching. User input is escaped before reaching Mongo's regex.
    """
    result = CatalogMatches()
    formula = parse_formula(query)
    if formula:
        elements, ratios = formula
        candidates = Material.objects(__raw__={
            'element_symbols': {'$all': list(elements), '$size': len(elements)},
        }).only('id', 'elements')
        for material in candidates:
            if not ratios or _same_ratios(material.elements, elements):
                result.parent_materials.add(material.id)
        result.materials.update(result.parent_materials)
        result.mode = 'composition'
        return result

    pattern = {'$regex': '^' + re.escape(query) + '$', '$options': 'i'}
    title_pattern = {'$regex': re.escape(query), '$options': 'i'}
    normalized = query.casefold()
    exact = lambda value: str(value or '').strip().casefold() == normalized
    for material in Material.objects(__raw__={'$or': [
        {'_id': pattern}, {'display_name': pattern}, {'dft_calculations.comp_auid': pattern},
    ]}).only('id', 'display_name', 'dft_calculations'):
        if exact(material.id) or exact(material.display_name):
            result.parent_materials.add(material.id)
            result.materials.add(material.id)
        for comp in material.dft_calculations or []:
            if exact(comp.comp_auid) and is_visible_to_user(comp.visibility_affiliations, affiliations):
                result.computations.add((material.id, comp.comp_auid))
                result.materials.add(material.id)

    fields = ('_id', 'trials.trial_id', 'trials.exp_condition.additional_params.source_batch_id',
              'literature.doi', 'literature.lit_id')
    candidates = Recipe.objects(__raw__={'$or': [
        *({name: pattern} for name in fields), {'literature.title': title_pattern},
    ]}).only('id', 'material_auid', 'trials', 'literature', 'visibility_affiliations')
    for recipe in candidates:
        if exact(recipe.id) and recipe_or_children_visible(recipe, affiliations):
            result.parent_recipes.add(recipe.id)
            result.recipes.add(recipe.id)
        for trial in recipe.trials or []:
            params = getattr(trial.exp_condition, 'additional_params', {}) or {}
            if (exact(trial.trial_id) or exact(params.get('source_batch_id'))) and is_visible_to_user(trial.visibility_affiliations, affiliations):
                result.recipes.add(recipe.id)
        for lit in recipe.literature or []:
            if (exact(lit.doi) or exact(lit.lit_id) or normalized in (lit.title or '').casefold()) and is_visible_to_user(lit.visibility_affiliations, affiliations):
                result.literature.add((recipe.id, lit.doi))
                result.recipes.add(recipe.id)
        if recipe.id in result.recipes:
            result.materials.add(recipe.material_auid)
    return result


def sort_by_added(rows, direction):
    """Sort before pagination, with stable ties and missing dates last."""
    def timestamp(row):
        value = row.get('created_at')
        if isinstance(value, datetime):
            return value.replace(tzinfo=value.tzinfo or timezone.utc).timestamp()
        return None

    def key(row):
        value = timestamp(row)
        identity = tuple(str(row.get(name) or '') for name in
                         ('material_auid', 'recipe_auid', 'lit_id', 'comp_auid'))
        return (value is None, (value or 0) * (-1 if direction == 'newest' else 1), identity)
    rows.sort(key=key)
