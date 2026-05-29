"""
Read-time helpers for the nested Material + Recipe topology.

Most of what used to be Mongo-side ``$facet``/``$unionWith`` machinery
collapses to direct document fetches now that the data actually lives in the
shape the UI reads. Three public entry points remain:

    composition_view(material_auid, user_affiliations=None) -> dict
        Fetches one ``materials`` doc plus all its ``recipes`` and flattens
        the trials/literature arrays back into the shape the composition
        detail template expects. No aggregation pipeline required.

    browse_materials(...) -> BrowseMaterialsResult
        ``$lookup`` (slim recipe projection for rollups), per-recipe rollups, optional
        ``$facet`` pagination when ``skip``/``limit`` are set (not used with semantic AUID lists).

    catalog_landing_page_totals() -> dict
        Full-database counts (materials, trials, literature rows, DFT entries).
        No organization visibility filter — used on the public home page.
    catalog_stats() -> dict
        Backward-compatible alias of :func:`catalog_landing_page_totals`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from mongoengine.connection import get_db

from . import search as search_mod
from .auid import lit_auid as _lit_auid
from .documents import Material, MLEmbedding, Recipe


# =============================================================================
# composition_view: the joined page for a single material class
# =============================================================================

def _visible_to(user_affiliations: Optional[Iterable[str]], tags: Optional[Iterable[str]]) -> bool:
    """Pure-Python visibility test for embedded records."""
    tag_list = list(tags or []) or ["S4E"]
    user_list = list(user_affiliations or [])
    if "S4E" in user_list:
        return True
    if not user_list:
        return "S4E" in tag_list
    if "S4E" in tag_list:
        return True
    return any(t in tag_list for t in user_list)


def _embedded_to_dict(embedded_obj) -> Dict[str, Any]:
    """Serialize an EmbeddedDocument into a plain dict the template can read."""
    if embedded_obj is None:
        return {}
    raw = embedded_obj.to_mongo().to_dict()
    return raw


def composition_view(
    material_auid: str,
    user_affiliations: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Return the full joined view for a material class."""
    material = Material.objects(id=material_auid).first()
    if material is None:
        return {
            "material_auid": material_auid,
            "annotation": None,
            "elements": None,
            "element_symbols": [],
            "structure_family": None,
            "num_elements": 0,
            "trials": [],
            "literature": [],
            "computational": [],
            "ml_embeddings": [],
            "recipes": [],
            "recipe_groups": [],
            "exists": False,
        }

    recipes_qs = Recipe.objects(material_auid=material_auid).order_by("-created_at")
    recipes = list(recipes_qs)

    trials: List[Dict[str, Any]] = []
    literature: List[Dict[str, Any]] = []
    recipes_for_template: List[Dict[str, Any]] = []
    recipe_groups: List[Dict[str, Any]] = []

    for recipe in recipes:
        recipe_visible = _visible_to(user_affiliations, recipe.visibility_affiliations)
        recipe_trials_dicts: List[Dict[str, Any]] = []
        recipe_lits_dicts: List[Dict[str, Any]] = []

        for trial in recipe.trials or []:
            if not _visible_to(user_affiliations, trial.visibility_affiliations):
                continue
            d = _embedded_to_dict(trial)
            d["material_auid"] = material_auid
            d["recipe_auid"] = recipe.id
            recipe_trials_dicts.append(d)

        for lit in recipe.literature or []:
            if not _visible_to(user_affiliations, lit.visibility_affiliations):
                continue
            d = _embedded_to_dict(lit)
            d["material_auid"] = material_auid
            d["recipe_auid"] = recipe.id
            recipe_lits_dicts.append(d)

        trials.extend(recipe_trials_dicts)
        literature.extend(recipe_lits_dicts)

        recipes_for_template.append({
            "recipe_auid": recipe.id,
            "material_auid": material_auid,
            "synthesis_steps": list(recipe.synthesis_steps or []),
            "visibility_affiliations": list(recipe.visibility_affiliations or []),
            "trial_count": len(recipe_trials_dicts),
            "literature_count": len(recipe_lits_dicts),
            "created_at": recipe.created_at,
            "updated_at": recipe.updated_at,
        })

        if recipe_visible or recipe_trials_dicts or recipe_lits_dicts:
            recipe_groups.append({
                "recipe_auid": recipe.id,
                "synthesis_steps": list(recipe.synthesis_steps or []),
                "trial_count": len(recipe_trials_dicts),
                "literature_count": len(recipe_lits_dicts),
                "trials": recipe_trials_dicts,
                "literature": recipe_lits_dicts,
            })

    trials.sort(
        key=lambda row: (row.get("trial_date") or row.get("created_at")),
        reverse=True,
    )
    literature.sort(key=lambda row: row.get("created_at") or "")

    computational: List[Dict[str, Any]] = []
    for dft in material.dft_calculations or []:
        if not _visible_to(user_affiliations, dft.visibility_affiliations):
            continue
        d = _embedded_to_dict(dft)
        d["material_auid"] = material_auid
        computational.append(d)
    computational.sort(key=lambda row: row.get("created_at") or "", reverse=True)

    annotation = {
        "display_name": material.display_name,
        "notes": material.notes,
        "curator": material.curator,
        "default_visibility_affiliations": list(material.default_visibility_affiliations or []),
    }
    if not any(annotation[k] for k in ("display_name", "notes", "curator")):
        annotation = None

    ml_embeddings = [
        emb.to_mongo().to_dict()
        for emb in MLEmbedding.objects(material_auid=material_auid)
    ]

    return {
        "material_auid": material_auid,
        "annotation": annotation,
        "elements": dict(material.elements or {}),
        "element_symbols": list(material.element_symbols or []),
        "structure_family": material.structure_family,
        "num_elements": material.num_elements,
        "material_created_at": getattr(material, "created_at", None),
        "trials": trials,
        "literature": literature,
        "computational": computational,
        "ml_embeddings": ml_embeddings,
        "recipes": recipes_for_template,
        "recipe_groups": recipe_groups,
        "exists": True,
    }


# =============================================================================
# Browse aggregations: per-recipe rollups (avoid giant _all_trials / _all_lits)
# =============================================================================


def _mongo_embedded_visible_cond(user_tags: List[str]) -> Dict[str, Any]:
    """``$filter`` condition for ``as: item`` trial/literature subdocs (browse visibility)."""
    return {
        "$or": [
            {"$in": ["S4E", {"$ifNull": ["$$item.visibility_affiliations", []]}]},
            {
                "$gt": [
                    {
                        "$size": {
                            "$setIntersection": [
                                {"$ifNull": ["$$item.visibility_affiliations", []]},
                                list(user_tags),
                            ]
                        }
                    },
                    0,
                ]
            },
        ]
    }


def _trial_count_expr(*, is_public_user: bool, user_tags: List[str]) -> Dict[str, Any]:
    if is_public_user:
        return {
            "$sum": {
                "$map": {
                    "input": "$_recipes",
                    "as": "r",
                    "in": {"$size": {"$ifNull": ["$$r.trials", []]}},
                }
            }
        }
    ut = list(user_tags)
    cond = _mongo_embedded_visible_cond(ut)
    return {
        "$sum": {
            "$map": {
                "input": "$_recipes",
                "as": "r",
                "in": {
                    "$size": {
                        "$filter": {
                            "input": {"$ifNull": ["$$r.trials", []]},
                            "as": "item",
                            "cond": cond,
                        }
                    }
                },
            }
        }
    }


def _literature_count_expr(*, is_public_user: bool, user_tags: List[str]) -> Dict[str, Any]:
    if is_public_user:
        return {
            "$sum": {
                "$map": {
                    "input": "$_recipes",
                    "as": "r",
                    "in": {"$size": {"$ifNull": ["$$r.literature", []]}},
                }
            }
        }
    ut = list(user_tags)
    cond = _mongo_embedded_visible_cond(ut)
    return {
        "$sum": {
            "$map": {
                "input": "$_recipes",
                "as": "r",
                "in": {
                    "$size": {
                        "$filter": {
                            "input": {"$ifNull": ["$$r.literature", []]},
                            "as": "item",
                            "cond": cond,
                        }
                    }
                },
            }
        }
    }


def _latest_trial_date_expr(*, is_public_user: bool, user_tags: List[str]) -> Dict[str, Any]:
    """Latest ``trial_date`` among visible trials; only materializes date scalars."""
    if is_public_user:
        return {
            "$max": {
                "$reduce": {
                    "input": "$_recipes",
                    "initialValue": [],
                    "in": {
                        "$concatArrays": [
                            "$$value",
                            {
                                "$map": {
                                    "input": {"$ifNull": ["$$this.trials", []]},
                                    "as": "t",
                                    "in": "$$t.trial_date",
                                }
                            },
                        ]
                    },
                }
            }
        }
    ut = list(user_tags)
    cond = _mongo_embedded_visible_cond(ut)
    return {
        "$max": {
            "$map": {
                "input": "$_recipes",
                "as": "r",
                "in": {
                    "$max": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": {"$ifNull": ["$$r.trials", []]},
                                    "as": "item",
                                    "cond": cond,
                                }
                            },
                            "as": "t",
                            "in": "$$t.trial_date",
                        }
                    }
                },
            }
        }
    }


# Maximum page size for server-side browse pagination (materials mode).
BROWSE_MATERIALS_MAX_LIMIT = 100


@dataclass
class BrowseMaterialsResult:
    """Rows from :func:`browse_materials` plus total count for pagination."""

    rows: List[Dict[str, Any]]
    total_count: int


def _recipes_lookup_stage(*, slim: bool) -> Dict[str, Any]:
    """``$lookup`` into recipes; slim keeps only fields needed for visibility rollups."""
    coll = Recipe._meta["collection"]
    if slim:
        return {
            "$lookup": {
                "from": coll,
                "let": {"mid": "$_id"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$material_auid", "$$mid"]}}},
                    {
                        "$project": {
                            "_id": 1,
                            "material_auid": 1,
                            "trials": {
                                "$map": {
                                    "input": {"$ifNull": ["$trials", []]},
                                    "as": "t",
                                    "in": {
                                        "trial_date": "$$t.trial_date",
                                        "visibility_affiliations": {
                                            "$ifNull": ["$$t.visibility_affiliations", []]
                                        },
                                    },
                                }
                            },
                            "literature": {
                                "$map": {
                                    "input": {"$ifNull": ["$literature", []]},
                                    "as": "l",
                                    "in": {
                                        "visibility_affiliations": {
                                            "$ifNull": ["$$l.visibility_affiliations", []]
                                        },
                                    },
                                }
                            },
                        }
                    },
                ],
                "as": "_recipes",
            }
        }
    return {
        "$lookup": {
            "from": coll,
            "localField": "_id",
            "foreignField": "material_auid",
            "as": "_recipes",
        }
    }


# =============================================================================
# browse_materials: one row per material, summarised via $lookup on recipes
# =============================================================================

def browse_materials(
    elements: Optional[Iterable[str]] = None,
    structure_family: Optional[str] = None,
    min_elements: Optional[int] = None,
    max_elements: Optional[int] = None,
    has_experiments: Optional[bool] = None,
    has_literature: Optional[bool] = None,
    has_computational: Optional[bool] = None,
    user_affiliations: Optional[Iterable[str]] = None,
    material_auid_query: Optional[str] = None,
    material_auid_in: Optional[Iterable[str]] = None,
    skip: Optional[int] = None,
    limit: Optional[int] = None,
) -> BrowseMaterialsResult:
    """Return one summary row per material matching the filters.

    When ``material_auid_in`` is provided, rows are re-sorted in Python to
    match that order (semantic search). Server-side ``skip``/``limit`` are
    ignored in that case so ordering stays correct.

    When ``skip`` and ``limit`` are set and ``material_auid_in`` is None, uses
    ``$facet`` to return one page and a total count in one round-trip.
    """
    db = get_db()

    prefilter_stages = search_mod.combine_match_stages(
        search_mod.element_match_stage(elements),
        search_mod.structure_family_match_stage(structure_family),
        search_mod.num_elements_match_stage(min_elements, max_elements),
        search_mod.text_match_stage(material_auid_query, field="_id"),
    )

    auid_order: Dict[str, int] = {}
    if material_auid_in is not None:
        auid_list = [str(a) for a in material_auid_in if a]
        if not auid_list:
            return BrowseMaterialsResult([], 0)
        auid_order = {auid: idx for idx, auid in enumerate(auid_list)}
        prefilter_stages.append({"$match": {"_id": {"$in": auid_list}}})

    user_tags = list(user_affiliations or [])
    is_public_user = "S4E" in user_tags or not user_tags

    use_facet = (
        skip is not None
        and limit is not None
        and not auid_order
    )
    skip_n = max(0, int(skip)) if skip is not None else 0
    limit_n = min(max(1, int(limit)), BROWSE_MATERIALS_MAX_LIMIT) if limit is not None else 0

    pipeline: List[Dict[str, Any]] = list(prefilter_stages) + [
        _recipes_lookup_stage(slim=True),
        {
            "$project": {
                "_id": 0,
                "material_auid": "$_id",
                "elements": 1,
                "element_symbols": 1,
                "structure_family": 1,
                "num_elements": 1,
                "display_name": 1,
                "default_visibility_affiliations": 1,
                "trial_count": _trial_count_expr(
                    is_public_user=is_public_user, user_tags=user_tags
                ),
                "literature_count": _literature_count_expr(
                    is_public_user=is_public_user, user_tags=user_tags
                ),
                "computational_count": {"$size": {"$ifNull": ["$dft_calculations", []]}},
                "recipe_count": {"$size": "$_recipes"},
                "latest_trial_date": _latest_trial_date_expr(
                    is_public_user=is_public_user, user_tags=user_tags
                ),
                "created_at": 1,
            }
        },
        {
            "$addFields": {
                "has_experiments": {"$gt": ["$trial_count", 0]},
                "has_literature": {"$gt": ["$literature_count", 0]},
                "has_computational": {"$gt": ["$computational_count", 0]},
            }
        },
    ]

    if has_experiments is not None:
        pipeline.append({"$match": {"has_experiments": bool(has_experiments)}})
    if has_literature is not None:
        pipeline.append({"$match": {"has_literature": bool(has_literature)}})
    if has_computational is not None:
        pipeline.append({"$match": {"has_computational": bool(has_computational)}})

    pipeline.append(
        {"$sort": {"latest_trial_date": -1, "created_at": -1, "material_auid": 1}}
    )

    if use_facet and limit_n:
        pipeline.append({
            "$facet": {
                "meta": [{"$count": "total"}],
                "data": [{"$skip": skip_n}, {"$limit": limit_n}],
            }
        })
        raw = list(db[Material._meta["collection"]].aggregate(pipeline))
        if not raw:
            return BrowseMaterialsResult([], 0)
        facet = raw[0]
        total_count = facet["meta"][0]["total"] if facet.get("meta") else 0
        rows = facet.get("data") or []
    else:
        rows = list(db[Material._meta["collection"]].aggregate(pipeline))
        total_count = len(rows)

    if auid_order:
        rows.sort(key=lambda row: auid_order.get(row.get("material_auid"), 1 << 30))

    return BrowseMaterialsResult(rows=rows, total_count=total_count)


def _material_prefilter_pipeline(
    elements: Optional[Iterable[str]] = None,
    structure_family: Optional[str] = None,
    min_elements: Optional[int] = None,
    max_elements: Optional[int] = None,
    has_experiments: Optional[bool] = None,
    has_literature: Optional[bool] = None,
    has_computational: Optional[bool] = None,
    user_affiliations: Optional[Iterable[str]] = None,
    material_auid_query: Optional[str] = None,
    material_auid_in: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Stages shared by flat browse helpers: match materials, ``$lookup`` recipes, roll up counts.

    Computes ``trial_count`` / ``literature_count`` with the same visibility rules as
    :func:`browse_materials` without materializing concatenated trial/literature arrays.
    Leaves ``_recipes`` on each material for ``$unwind`` (literature / computational flat views).
    """
    prefilter_stages = search_mod.combine_match_stages(
        search_mod.element_match_stage(elements),
        search_mod.structure_family_match_stage(structure_family),
        search_mod.num_elements_match_stage(min_elements, max_elements),
        search_mod.text_match_stage(material_auid_query, field="_id"),
    )

    auid_order: Dict[str, int] = {}
    if material_auid_in is not None:
        auid_list = [str(a) for a in material_auid_in if a]
        if not auid_list:
            return []
        prefilter_stages.append({"$match": {"_id": {"$in": auid_list}}})

    user_tags = list(user_affiliations or [])
    is_public_user = "S4E" in user_tags or not user_tags

    pipeline: List[Dict[str, Any]] = list(prefilter_stages) + [
        _recipes_lookup_stage(slim=False),
        {
            "$addFields": {
                "trial_count": _trial_count_expr(
                    is_public_user=is_public_user, user_tags=user_tags
                ),
                "literature_count": _literature_count_expr(
                    is_public_user=is_public_user, user_tags=user_tags
                ),
                "computational_count": {"$size": {"$ifNull": ["$dft_calculations", []]}},
            }
        },
        {
            "$addFields": {
                "has_experiments": {"$gt": ["$trial_count", 0]},
                "has_literature": {"$gt": ["$literature_count", 0]},
                "has_computational": {"$gt": ["$computational_count", 0]},
            }
        },
    ]

    if has_experiments is not None:
        pipeline.append({"$match": {"has_experiments": bool(has_experiments)}})
    if has_literature is not None:
        pipeline.append({"$match": {"has_literature": bool(has_literature)}})
    if has_computational is not None:
        pipeline.append({"$match": {"has_computational": bool(has_computational)}})

    return pipeline


def browse_literature_flat(
    elements: Optional[Iterable[str]] = None,
    structure_family: Optional[str] = None,
    min_elements: Optional[int] = None,
    max_elements: Optional[int] = None,
    has_experiments: Optional[bool] = None,
    has_literature: Optional[bool] = None,
    has_computational: Optional[bool] = None,
    user_affiliations: Optional[Iterable[str]] = None,
    material_auid_query: Optional[str] = None,
    material_auid_in: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """One row per embedded literature entry (before Python visibility filtering)."""
    pipeline = _material_prefilter_pipeline(
        elements=elements,
        structure_family=structure_family,
        min_elements=min_elements,
        max_elements=max_elements,
        has_experiments=has_experiments,
        has_literature=has_literature,
        has_computational=has_computational,
        user_affiliations=user_affiliations,
        material_auid_query=material_auid_query,
        material_auid_in=material_auid_in,
    )
    if not pipeline:
        return []

    pipeline += [
        {"$unwind": "$_recipes"},
        {"$match": {"_recipes.literature": {"$ne": []}}},
        {"$unwind": "$_recipes.literature"},
        {
            "$project": {
                "_id": 0,
                "material_auid": "$_id",
                "structure_family": "$structure_family",
                "element_symbols": "$element_symbols",
                "num_elements": "$num_elements",
                "recipe_auid": "$_recipes._id",
                "lit_id": "$_recipes.literature.lit_id",
                "doi": "$_recipes.literature.doi",
                "title": "$_recipes.literature.title",
                "authors": "$_recipes.literature.authors",
                "journal": "$_recipes.literature.journal",
                "year": "$_recipes.literature.year",
                "lit_visibility": "$_recipes.literature.visibility_affiliations",
            }
        },
    ]

    db = get_db()
    rows = list(db[Material._meta["collection"]].aggregate(pipeline))

    # Backfill lit_id for rows whose embedded doc predates the field.
    for row in rows:
        if not row.get("lit_id"):
            doi = row.get("doi") or ""
            row["lit_id"] = _lit_auid(doi) if doi else ""

    auid_order: Dict[str, int] = {}
    if material_auid_in is not None:
        auid_list = [str(a) for a in material_auid_in if a]
        auid_order = {auid: idx for idx, auid in enumerate(auid_list)}

    if auid_order:
        rows.sort(
            key=lambda row: (
                auid_order.get(row.get("material_auid"), 1 << 30),
                row.get("doi") or "",
            )
        )
    else:
        rows.sort(key=lambda row: (row.get("material_auid") or "", row.get("doi") or ""))

    return rows


def browse_computational_flat(
    elements: Optional[Iterable[str]] = None,
    structure_family: Optional[str] = None,
    min_elements: Optional[int] = None,
    max_elements: Optional[int] = None,
    has_experiments: Optional[bool] = None,
    has_literature: Optional[bool] = None,
    has_computational: Optional[bool] = None,
    user_affiliations: Optional[Iterable[str]] = None,
    material_auid_query: Optional[str] = None,
    material_auid_in: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """One row per embedded DFT entry (before Python visibility filtering)."""
    pipeline = _material_prefilter_pipeline(
        elements=elements,
        structure_family=structure_family,
        min_elements=min_elements,
        max_elements=max_elements,
        has_experiments=has_experiments,
        has_literature=has_literature,
        has_computational=has_computational,
        user_affiliations=user_affiliations,
        material_auid_query=material_auid_query,
        material_auid_in=material_auid_in,
    )
    if not pipeline:
        return []

    pipeline += [
        {"$match": {"dft_calculations.0": {"$exists": True}}},
        {"$unwind": "$dft_calculations"},
        {
            "$project": {
                "_id": 0,
                "material_auid": "$_id",
                "structure_family": "$structure_family",
                "element_symbols": "$element_symbols",
                "num_elements": "$num_elements",
                "comp_auid": "$dft_calculations.comp_auid",
                "dft_source": "$dft_calculations.dft_source",
                "dft_formation_energy_ev": "$dft_calculations.dft_formation_energy_ev",
                "dft_hull_distance_ev": "$dft_calculations.dft_hull_distance_ev",
                "dft_bandgap_ev": "$dft_calculations.dft_bandgap_ev",
                "dft_visibility": "$dft_calculations.visibility_affiliations",
            }
        },
    ]

    db = get_db()
    rows = list(db[Material._meta["collection"]].aggregate(pipeline))

    auid_order: Dict[str, int] = {}
    if material_auid_in is not None:
        auid_list = [str(a) for a in material_auid_in if a]
        auid_order = {auid: idx for idx, auid in enumerate(auid_list)}

    if auid_order:
        rows.sort(
            key=lambda row: (
                auid_order.get(row.get("material_auid"), 1 << 30),
                row.get("comp_auid") or "",
            )
        )
    else:
        rows.sort(key=lambda row: (row.get("material_auid") or "", row.get("comp_auid") or ""))

    return rows


def synthesis_steps_search_text(steps: Any) -> str:
    """Lowercase string for substring matching (``steps_q`` filter)."""
    try:
        return json.dumps(steps or [], sort_keys=True).lower()
    except (TypeError, ValueError):
        return ""


# =============================================================================
# Stats for the index page
# =============================================================================


def catalog_landing_page_totals() -> Dict[str, int]:
    """Counts across the whole LOOP database (no per-user visibility filter).

    Used on the landing page so visitors see how large the catalog is even when
    Browse and detail pages only show rows matching their organization access.
    """
    db = get_db()

    material_count = db[Material._meta["collection"]].count_documents({})
    recipe_count = db[Recipe._meta["collection"]].count_documents({})

    # Flatten embedded trials / literature by reading array sizes; one
    # aggregation covers both in a single scan.
    pipeline = [
        {
            "$group": {
                "_id": None,
                "trials": {"$sum": {"$size": {"$ifNull": ["$trials", []]}}},
                "literature": {"$sum": {"$size": {"$ifNull": ["$literature", []]}}},
            }
        }
    ]
    embedded = list(db[Recipe._meta["collection"]].aggregate(pipeline))
    trial_count = embedded[0]["trials"] if embedded else 0
    literature_count = embedded[0]["literature"] if embedded else 0

    # DFT runs on materials.
    dft_pipeline = [
        {
            "$group": {
                "_id": None,
                "total": {"$sum": {"$size": {"$ifNull": ["$dft_calculations", []]}}},
            }
        }
    ]
    dft = list(db[Material._meta["collection"]].aggregate(dft_pipeline))
    dft_count = dft[0]["total"] if dft else 0

    return {
        "composition_count": material_count,
        "material_count": material_count,
        "recipe_count": recipe_count,
        "literature_count": literature_count,
        "experiment_count": trial_count,
        "computational_count": dft_count,
    }


def catalog_stats() -> Dict[str, int]:
    """Backward-compatible alias of :func:`catalog_landing_page_totals`."""
    return catalog_landing_page_totals()


__all__ = [
    "BROWSE_MATERIALS_MAX_LIMIT",
    "BrowseMaterialsResult",
    "composition_view",
    "browse_materials",
    "browse_literature_flat",
    "browse_computational_flat",
    "synthesis_steps_search_text",
    "catalog_landing_page_totals",
    "catalog_stats",
]
