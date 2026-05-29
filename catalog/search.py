"""
Element-set and structure-family query builders used by both the live
composition view and the browse page.

All helpers return plain MongoEngine ``Q`` objects or ``$match`` dicts so
they compose cleanly into aggregation pipelines without leaking pipeline
syntax into callers.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


def element_match_stage(elements: Optional[Iterable[str]]) -> Optional[Dict[str, Any]]:
    """Build a ``$match`` stage requiring every listed element to be present.

    Backed by the multi-key index on ``element_symbols`` that every recipe
    collection declares.
    """
    symbols = [str(s).strip() for s in (elements or []) if s and str(s).strip()]
    if not symbols:
        return None
    return {"$match": {"element_symbols": {"$all": symbols}}}


def structure_family_match_stage(family: Optional[str]) -> Optional[Dict[str, Any]]:
    cleaned = (family or "").strip().lower()
    if not cleaned:
        return None
    return {"$match": {"structure_family": cleaned}}


def num_elements_match_stage(
    min_elements: Optional[int], max_elements: Optional[int]
) -> Optional[Dict[str, Any]]:
    clause: Dict[str, Any] = {}
    if min_elements is not None:
        clause["$gte"] = int(min_elements)
    if max_elements is not None:
        clause["$lte"] = int(max_elements)
    if not clause:
        return None
    return {"$match": {"num_elements": clause}}


def text_match_stage(query: Optional[str], field: str = "material_auid") -> Optional[Dict[str, Any]]:
    """Case-insensitive substring match against a string field."""
    cleaned = (query or "").strip()
    if not cleaned:
        return None
    return {
        "$match": {
            field: {"$regex": cleaned, "$options": "i"},
        }
    }


def visibility_match_stage(user_affiliations: Optional[Iterable[str]]) -> Optional[Dict[str, Any]]:
    """Filter out records whose visibility tags do not overlap with the user.

    S4E users bypass this filter (they see everything). For other users we
    require either the record to carry S4E (public within the org) *or* at
    least one overlapping non-S4E tag.
    """
    tags = [t for t in (user_affiliations or []) if t]
    if "S4E" in tags:
        return None
    if not tags:
        return {"$match": {"visibility_affiliations": "S4E"}}
    return {
        "$match": {
            "visibility_affiliations": {"$in": list(tags) + ["S4E"]},
        }
    }


def combine_match_stages(*stages: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for stage in stages:
        if stage:
            out.append(stage)
    return out


__all__ = [
    "element_match_stage",
    "structure_family_match_stage",
    "num_elements_match_stage",
    "text_match_stage",
    "visibility_match_stage",
    "combine_match_stages",
]
