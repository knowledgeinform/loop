"""Bulk selection and deletion of literature records.

Literature entries are embedded documents inside ``Recipe.literature``, so
removing several from one recipe is a single ``save()`` rather than N. That is
why this groups selections by recipe before writing.

Two behaviours here exist because the single-record path already has them and
silently diverging would be worse than inheriting them:

* Permission is per record (uploader or superuser), checked for every selection
  rather than once for the batch. Checking once would let a user delete another
  person's record by including it in a batch alongside their own.
* Deletion matches ``lit_id`` **or** the normalised DOI, mirroring
  ``views.delete_literature``. Selecting one row can therefore remove more than
  one entry when a recipe holds duplicates of the same DOI, so the count is
  computed and shown before anything is written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from catalog.documents import Recipe


SEPARATOR = "|"   # recipe_id may contain ':' so that cannot be the delimiter

# One checkbox is posted per selected record, and Django rejects a form with more
# than DATA_UPLOAD_MAX_NUMBER_FIELDS (1000 by default) fields -- it raises
# TooManyFieldsSent, which surfaces as a bare 400 with no explanation. Capping
# below that means the user is told to delete in batches instead of meeting a
# wall they cannot interpret. Deliberately well under 1000 to leave room for the
# CSRF token and any future form fields.
MAX_SELECTION = 500


@dataclass
class LiteratureRow:
    """One selectable record, flattened for display."""

    recipe_id: str
    lit_id: str
    doi: str
    title: str
    journal: str
    year: Any
    material_auid: str
    extracted_by: str
    created_at: Any = None

    @property
    def token(self) -> str:
        """Opaque value for a checkbox, parsed back by ``parse_selection``."""
        return f"{self.recipe_id}{SEPARATOR}{self.lit_id}"


@dataclass
class DeletionPlan:
    rows: list[LiteratureRow] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # Entries that will also go because they share a DOI with a selected row.
    cascade: list[LiteratureRow] = field(default_factory=list)

    @property
    def total_removed(self) -> int:
        return len(self.rows) + len(self.cascade)


def _norm_doi(value: Any) -> str:
    return str(value or "").strip().lower()


def list_user_literature(username: str, *, include_all: bool = False) -> list[LiteratureRow]:
    """Every literature entry uploaded by ``username``, newest first.

    ``include_all`` returns entries regardless of uploader, for superusers.
    """
    wanted = str(username or "").strip().lower()
    rows: list[LiteratureRow] = []

    for recipe in Recipe.objects.only("id", "material_auid", "literature"):
        for lit in recipe.literature or []:
            owner = str(getattr(lit, "extracted_by", "") or "")
            if not include_all and owner.strip().lower() != wanted:
                continue
            rows.append(
                LiteratureRow(
                    recipe_id=str(recipe.id),
                    lit_id=str(getattr(lit, "lit_id", "") or ""),
                    doi=str(lit.doi or ""),
                    title=str(getattr(lit, "title", "") or ""),
                    journal=str(getattr(lit, "journal", "") or ""),
                    year=getattr(lit, "year", None),
                    material_auid=str(recipe.material_auid or ""),
                    extracted_by=owner,
                    created_at=getattr(lit, "created_at", None),
                )
            )

    rows.sort(key=lambda r: (r.created_at is None, r.created_at), reverse=True)
    return rows


def parse_selection(raw_tokens: Iterable[str]) -> list[tuple[str, str]]:
    """Turn checkbox values back into ``(recipe_id, lit_id)`` pairs.

    Malformed tokens are dropped rather than raising: a hand-edited form should
    not 500, and anything unparseable simply cannot match a record.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for token in raw_tokens:
        text = str(token or "").strip()
        if SEPARATOR not in text:
            continue
        recipe_id, _, lit_id = text.partition(SEPARATOR)
        recipe_id, lit_id = recipe_id.strip(), lit_id.strip()
        if not recipe_id or not lit_id:
            continue
        if (recipe_id, lit_id) in seen:
            continue
        seen.add((recipe_id, lit_id))
        pairs.append((recipe_id, lit_id))
    return pairs


def build_plan(user, selections: Iterable[tuple[str, str]]) -> DeletionPlan:
    """Resolve selections into what would actually be removed.

    Runs before any write so the confirmation screen can state the true count,
    including DOI-duplicate entries the user did not tick.
    """
    from catalog.views import _is_uploader_or_superuser   # avoids an import cycle

    plan = DeletionPlan()
    by_recipe: dict[str, list[str]] = {}
    for recipe_id, lit_id in selections:
        by_recipe.setdefault(recipe_id, []).append(lit_id)

    for recipe_id, lit_ids in by_recipe.items():
        recipe = Recipe.objects(id=recipe_id).first()
        if recipe is None:
            plan.missing.extend(f"{recipe_id}{SEPARATOR}{l}" for l in lit_ids)
            continue

        entries = list(recipe.literature or [])
        chosen_dois: set[str] = set()

        for lit_id in lit_ids:
            match = next(
                (l for l in entries if str(getattr(l, "lit_id", "")) == lit_id), None
            )
            if match is None:
                plan.missing.append(f"{recipe_id}{SEPARATOR}{lit_id}")
                continue
            if not _is_uploader_or_superuser(user, getattr(match, "extracted_by", "")):
                plan.denied.append(f"{match.doi} (uploaded by {match.extracted_by or 'unknown'})")
                continue
            chosen_dois.add(_norm_doi(match.doi))
            plan.rows.append(
                LiteratureRow(
                    recipe_id=recipe_id,
                    lit_id=lit_id,
                    doi=str(match.doi or ""),
                    title=str(getattr(match, "title", "") or ""),
                    journal=str(getattr(match, "journal", "") or ""),
                    year=getattr(match, "year", None),
                    material_auid=str(recipe.material_auid or ""),
                    extracted_by=str(getattr(match, "extracted_by", "") or ""),
                )
            )

        # Same-DOI siblings the user did not tick, which the DOI match will take.
        selected_ids = {r.lit_id for r in plan.rows if r.recipe_id == recipe_id}
        for other in entries:
            other_id = str(getattr(other, "lit_id", "") or "")
            if other_id in selected_ids:
                continue
            if _norm_doi(other.doi) in chosen_dois:
                plan.cascade.append(
                    LiteratureRow(
                        recipe_id=recipe_id,
                        lit_id=other_id,
                        doi=str(other.doi or ""),
                        title=str(getattr(other, "title", "") or ""),
                        journal=str(getattr(other, "journal", "") or ""),
                        year=getattr(other, "year", None),
                        material_auid=str(recipe.material_auid or ""),
                        extracted_by=str(getattr(other, "extracted_by", "") or ""),
                    )
                )

    return plan


def execute_plan(user, selections: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """Delete the permitted selections. Returns a summary for the flash message.

    Re-derives the plan rather than trusting one passed from the confirmation
    step, so a tampered or stale form cannot widen what gets deleted.
    """
    plan = build_plan(user, selections)

    removed = 0
    recipes_touched = 0
    by_recipe: dict[str, set[str]] = {}
    dois_by_recipe: dict[str, set[str]] = {}

    for row in plan.rows:
        by_recipe.setdefault(row.recipe_id, set()).add(row.lit_id)
        dois_by_recipe.setdefault(row.recipe_id, set()).add(_norm_doi(row.doi))

    for recipe_id, lit_ids in by_recipe.items():
        recipe = Recipe.objects(id=recipe_id).first()
        if recipe is None:
            continue
        before = len(recipe.literature or [])
        dois = dois_by_recipe.get(recipe_id, set())
        recipe.literature = [
            lit for lit in (recipe.literature or [])
            if str(getattr(lit, "lit_id", "")) not in lit_ids
            and _norm_doi(lit.doi) not in dois
        ]
        after = len(recipe.literature)
        if after != before:
            recipe.save()          # one write per recipe, not per record
            removed += before - after
            recipes_touched += 1

    return {
        "removed": removed,
        "recipes_touched": recipes_touched,
        "denied": plan.denied,
        "missing": plan.missing,
    }
