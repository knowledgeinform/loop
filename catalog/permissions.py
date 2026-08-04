"""Shared affiliation-visibility rules used by views, JSON APIs, and exports."""

import json

from .documents import AFFILIATION_VALUES, VISIBILITY_DEFAULT

AFFILIATION_CANONICAL = {
    "s4e": "S4E",
    "apl": "APL",
    "oak ridge": "Oak Ridge",
    "oakridge": "Oak Ridge",
}


def canonical_affiliation(value):
    """Map a free-form affiliation string to its canonical name, or None."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in AFFILIATION_CANONICAL:
        return AFFILIATION_CANONICAL[lowered]
    normalized = " ".join(lowered.replace("-", " ").replace("_", " ").split())
    if normalized in AFFILIATION_CANONICAL:
        return AFFILIATION_CANONICAL[normalized]
    for allowed in AFFILIATION_VALUES:
        if lowered == allowed.lower():
            return allowed
    return None


def normalize_visibility_tags(tags):
    """Coerce a string/JSON/CSV/list of affiliations into a canonical list."""
    if isinstance(tags, str):
        raw = tags.strip()
        if not raw:
            tags = []
        else:
            parsed = None
            if raw.startswith("[") and raw.endswith("]"):
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = None
            if isinstance(parsed, list):
                tags = parsed
            elif "," in raw:
                tags = [part.strip() for part in raw.split(",")]
            else:
                tags = [raw]
    normalized = []
    for tag in (tags or list(VISIBILITY_DEFAULT)):
        canonical = canonical_affiliation(tag)
        if canonical and canonical not in normalized:
            normalized.append(canonical)
    return normalized


def is_visible_to_user(item_visibility, user_affiliations):
    """True when any of the user's affiliations may see the item. S4E sees all."""
    visibility = normalize_visibility_tags(item_visibility or list(VISIBILITY_DEFAULT))
    user_affiliations = normalize_visibility_tags(user_affiliations or list(VISIBILITY_DEFAULT))
    if "S4E" in user_affiliations:
        return True
    if "S4E" in visibility:
        visibility = [aff for aff in visibility if aff != "S4E"]
    if not visibility:
        return False
    return any(aff in visibility for aff in user_affiliations)


def visible_recipe_children(recipe, user_affiliations):
    """Return ``(visible_trials, visible_lits, recipe_visible)`` for one recipe."""
    trials = [
        t for t in (recipe.trials or [])
        if is_visible_to_user(getattr(t, "visibility_affiliations", None), user_affiliations)
    ]
    lits = [
        lit for lit in (recipe.literature or [])
        if is_visible_to_user(getattr(lit, "visibility_affiliations", None), user_affiliations)
    ]
    recipe_visible = is_visible_to_user(
        getattr(recipe, "visibility_affiliations", None), user_affiliations
    )
    return trials, lits, recipe_visible


def recipe_or_children_visible(recipe, user_affiliations):
    """True when the recipe itself or any trial/literature entry is visible."""
    if is_visible_to_user(getattr(recipe, "visibility_affiliations", None), user_affiliations):
        return True
    for item in list(recipe.trials or []) + list(recipe.literature or []):
        if is_visible_to_user(getattr(item, "visibility_affiliations", None), user_affiliations):
            return True
    return False
