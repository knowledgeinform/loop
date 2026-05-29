"""
AUID (AFLOW-style content-addressable identifier) helpers for LOOP.

Three public composite id shapes in the nested Mongo topology:

    material_auid  = "M:" + 12-hex sha256 of {elements, structure_family}
    recipe_auid    = "{material_auid}:R:" + 12-hex sha256 of
                     {material_auid, synthesis_steps}
    comp_auid      = "{material_auid}:C:" + 12-hex sha256 of
                     {material_auid, dft_inputs}

    # Example:
    #     M:afc940abcdef
    #     M:afc940abcdef:R:art454def567
    #     M:afc940abcdef:C:9fa203bcd001

Trials and literature are stored as embedded documents inside a recipe and
therefore do not carry independent hashed AUIDs; they are keyed by
(material_auid, recipe_auid, trial_id|doi).

Computational runs ("DFT calcs") are embedded inside the material and carry
their own composite content-addressable id. The ``M:…:C:…`` layout means a
comp id is self-describing: you can extract the parent ``material_auid`` and
navigate back without a separate lookup, and two DFT runs with different
inputs for the same material produce distinct ``:C:…`` suffixes.

The 12-hex slice (48 bits) gives ~281 trillion possibilities per half, which
keeps the collision probability well under 1e-4 even at 1M distinct material
classes -- comfortably above the expected long-term corpus size (~100k
records).

Canonicalization rules (applied before hashing, never on canonical data
itself):

- Lowercase categorical strings; strip whitespace.
- Round floats to 4 significant figures.
- Drop keys whose value is None / "" / [] / {}.
- Sort dict keys; preserve list order (step sequence matters).
- Wrap in a versioned envelope: {"v": 1, "kind": "...", ...}.
- json.dumps(..., sort_keys=True, separators=(",", ":")), then sha256.

Nothing outside the spec is hashed, so timestamps, usernames and free-text
notes never change an AUID.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

AUID_VERSION = 1
HASH_LEN = 12

MATERIAL_PREFIX = "M:"
RECIPE_INFIX = ":R:"
COMP_INFIX = ":C:"
LIT_PREFIX = "L:"

# Retained for backward-compat imports; all live ids now start with M:.
COMP_PREFIX = "C:"

ALL_PREFIXES = (MATERIAL_PREFIX,)


_FLOAT_SIGFIGS = 4


def _round_sig(value: float, sigfigs: int = _FLOAT_SIGFIGS) -> float:
    if value == 0 or not math.isfinite(value):
        return float(value)
    magnitude = math.floor(math.log10(abs(value)))
    factor = 10 ** (sigfigs - 1 - magnitude)
    return round(value * factor) / factor


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _normalize_scalar(value: Any) -> Any:
    """Canonicalize a single scalar value for hashing."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int,)):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return _round_sig(float(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip().lower()
    return value


def canonicalize(value: Any) -> Any:
    """Recursively normalize a JSON-like structure for deterministic hashing."""
    if isinstance(value, dict):
        normalized: Dict[str, Any] = {}
        for key in sorted(str(k) for k in value.keys()):
            original_key = key if key in value else next(
                (k for k in value.keys() if str(k) == key), key
            )
            child = canonicalize(value[original_key])
            if _is_empty(child):
                continue
            normalized[str(key).strip().lower()] = child
        return normalized
    if isinstance(value, (list, tuple)):
        normalized_list: List[Any] = []
        for item in value:
            child = canonicalize(item)
            if _is_empty(child):
                continue
            normalized_list.append(child)
        return normalized_list
    return _normalize_scalar(value)


def _canonical_json(envelope: Dict[str, Any]) -> str:
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_hex(kind: str, payload: Dict[str, Any]) -> str:
    envelope = {"v": AUID_VERSION, "kind": kind, **canonicalize(payload)}
    digest = hashlib.sha256(_canonical_json(envelope).encode("utf-8")).hexdigest()
    return digest[:HASH_LEN]


# =============================================================================
# Element / structure normalization (pure functions of user input)
# =============================================================================

def normalize_elements(elements: Dict[str, float], scale: int = 1000) -> Dict[str, int]:
    """Scale element ratios to integers that sum to ``scale`` (default 1000).

    Returned dict has alphabetically-sorted string keys and positive int values.
    Raises ``ValueError`` on invalid input; that is a bug in the caller, not a
    hashing failure.
    """
    if not elements:
        raise ValueError("elements cannot be empty")

    cleaned: Dict[str, float] = {}
    for symbol, raw in elements.items():
        if symbol is None:
            continue
        try:
            ratio = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"non-numeric ratio for element {symbol!r}: {raw!r}") from exc
        if ratio <= 0:
            raise ValueError(f"ratio for element {symbol!r} must be positive (got {raw!r})")
        cleaned[str(symbol).strip()] = ratio

    if not cleaned:
        raise ValueError("elements dict has no valid entries")

    total = sum(cleaned.values())
    if total <= 0:
        raise ValueError("sum of element ratios must be positive")

    scaled = {sym: round((val / total) * scale) for sym, val in cleaned.items()}
    scaled = {sym: amt for sym, amt in scaled.items() if amt > 0}
    if not scaled:
        raise ValueError("scaled element dict collapsed to empty")
    return dict(sorted(scaled.items(), key=lambda item: item[0]))


STRUCTURE_FAMILY_VALUES = frozenset(
    ("rocksalt", "pyrochlore", "spinel", "perovskite", "fluorite", "other")
)


def normalize_structure_family(value: Any) -> str:
    raw = (str(value or "").strip().lower()) or "rocksalt"
    if raw not in STRUCTURE_FAMILY_VALUES:
        raise ValueError(f"invalid structure_family: {value!r}")
    return raw


def element_symbols(elements: Dict[str, int]) -> List[str]:
    return sorted(elements.keys())


# =============================================================================
# AUID builders
# =============================================================================

def material_auid(elements: Dict[str, float], structure_family: str) -> str:
    """``M:`` + 12-hex hash of the normalized (elements, structure_family)."""
    norm_elements = normalize_elements(elements)
    norm_sf = normalize_structure_family(structure_family)
    h = _hash_hex(
        "material",
        {"elements": norm_elements, "structure_family": norm_sf},
    )
    return f"{MATERIAL_PREFIX}{h}"


def recipe_auid(material: str, synthesis_steps: Optional[Iterable[Dict[str, Any]]]) -> str:
    """Composite id: ``{material_auid}:R:{12-hex hash of (material, steps)}``.

    Two recipes with the same material and identical synthesis steps produce
    the same composite id (cross-source dedup).
    """
    if not material or not material.startswith(MATERIAL_PREFIX):
        raise ValueError(f"material_auid must start with {MATERIAL_PREFIX!r}, got {material!r}")
    steps_list = list(synthesis_steps or [])
    h = _hash_hex(
        "recipe",
        {"material_auid": material, "steps": steps_list},
    )
    return build_recipe_id(material, h)


def lit_auid(doi: str) -> str:
    """``L:`` + 12-hex hash of the normalized DOI.

    Deterministic: same DOI always produces the same lit_id. URL-safe (only
    hex chars after the ``L:`` prefix, no slashes or special characters).
    """
    h = _hash_hex("literature", {"doi": (doi or "").strip().lower()})
    return f"{LIT_PREFIX}{h}"


def comp_auid(material: str, dft_inputs: Dict[str, Any]) -> str:
    """Composite id: ``{material_auid}:C:{12-hex hash of (material, dft_inputs)}``.

    Two DFT runs against the same material with the same canonical inputs
    collapse to the same comp id (cross-source dedup); differing inputs
    (functional, k-mesh, ENCUT, pseudopotential, ...) produce distinct ids.
    """
    if not material or not material.startswith(MATERIAL_PREFIX):
        raise ValueError(f"material_auid must start with {MATERIAL_PREFIX!r}, got {material!r}")
    h = _hash_hex(
        "computational",
        {"material_auid": material, "dft_inputs": dft_inputs or {}},
    )
    return build_comp_id(material, h)


# =============================================================================
# Composite recipe id helpers
# =============================================================================

def build_recipe_id(material: str, recipe_short: str) -> str:
    """Assemble a composite recipe id from a material_auid and its short hash."""
    if not material or not material.startswith(MATERIAL_PREFIX):
        raise ValueError(f"material_auid must start with {MATERIAL_PREFIX!r}, got {material!r}")
    short = (recipe_short or "").strip().lower()
    if not short:
        raise ValueError("recipe_short cannot be empty")
    if RECIPE_INFIX in short or MATERIAL_PREFIX in short:
        raise ValueError(f"recipe_short must be a bare hash, got {recipe_short!r}")
    return f"{material}{RECIPE_INFIX}{short}"


def split_recipe_id(recipe_id: str) -> Tuple[str, str]:
    """Split ``M:...:R:...`` into ``(material_auid, recipe_short)``."""
    if not recipe_id or not recipe_id.startswith(MATERIAL_PREFIX):
        raise ValueError(f"recipe_id must start with {MATERIAL_PREFIX!r}, got {recipe_id!r}")
    if RECIPE_INFIX not in recipe_id:
        raise ValueError(f"recipe_id missing {RECIPE_INFIX!r} infix, got {recipe_id!r}")
    material, _, recipe_short = recipe_id.partition(RECIPE_INFIX)
    if not recipe_short:
        raise ValueError(f"recipe_id has empty recipe component: {recipe_id!r}")
    return material, recipe_short


def material_auid_of(recipe_id: str) -> str:
    """Return just the ``M:...`` prefix of a composite recipe or comp id."""
    if RECIPE_INFIX in (recipe_id or ""):
        return split_recipe_id(recipe_id)[0]
    if COMP_INFIX in (recipe_id or ""):
        return split_comp_id(recipe_id)[0]
    if (recipe_id or "").startswith(MATERIAL_PREFIX):
        return recipe_id
    raise ValueError(f"cannot extract material_auid from {recipe_id!r}")


# =============================================================================
# Composite comp id helpers
# =============================================================================

def build_comp_id(material: str, comp_short: str) -> str:
    """Assemble a composite comp id from a material_auid and its short hash."""
    if not material or not material.startswith(MATERIAL_PREFIX):
        raise ValueError(f"material_auid must start with {MATERIAL_PREFIX!r}, got {material!r}")
    short = (comp_short or "").strip().lower()
    if not short:
        raise ValueError("comp_short cannot be empty")
    if COMP_INFIX in short or RECIPE_INFIX in short or MATERIAL_PREFIX in short:
        raise ValueError(f"comp_short must be a bare hash, got {comp_short!r}")
    return f"{material}{COMP_INFIX}{short}"


def split_comp_id(comp_id: str) -> Tuple[str, str]:
    """Split ``M:...:C:...`` into ``(material_auid, comp_short)``."""
    if not comp_id or not comp_id.startswith(MATERIAL_PREFIX):
        raise ValueError(f"comp_id must start with {MATERIAL_PREFIX!r}, got {comp_id!r}")
    if COMP_INFIX not in comp_id:
        raise ValueError(f"comp_id missing {COMP_INFIX!r} infix, got {comp_id!r}")
    material, _, comp_short = comp_id.partition(COMP_INFIX)
    if not comp_short:
        raise ValueError(f"comp_id has empty comp component: {comp_id!r}")
    return material, comp_short


# =============================================================================
# Convenience + dispatch
# =============================================================================

def is_material_auid(value: str) -> bool:
    return (
        bool(value)
        and value.startswith(MATERIAL_PREFIX)
        and RECIPE_INFIX not in value
        and COMP_INFIX not in value
        and len(value) > len(MATERIAL_PREFIX)
    )


def is_recipe_id(value: str) -> bool:
    return bool(value) and value.startswith(MATERIAL_PREFIX) and RECIPE_INFIX in value


def is_comp_auid(value: str) -> bool:
    return bool(value) and value.startswith(MATERIAL_PREFIX) and COMP_INFIX in value


def auid_kind(auid: str) -> Optional[str]:
    """Return the entity kind for an AUID, or None if unrecognized."""
    if not auid:
        return None
    if is_recipe_id(auid):
        return "recipe"
    if is_comp_auid(auid):
        return "computational"
    if is_material_auid(auid):
        return "material"
    return None


# =============================================================================
# Canonical subcomponents (exposed for tests + admin tooling)
# =============================================================================

def canonical_elements_and_structure(
    elements: Dict[str, float], structure_family: str
) -> Tuple[Dict[str, int], str]:
    return normalize_elements(elements), normalize_structure_family(structure_family)


__all__ = [
    "AUID_VERSION",
    "HASH_LEN",
    "MATERIAL_PREFIX",
    "RECIPE_INFIX",
    "COMP_INFIX",
    "COMP_PREFIX",
    "ALL_PREFIXES",
    "STRUCTURE_FAMILY_VALUES",
    "canonicalize",
    "canonical_elements_and_structure",
    "normalize_elements",
    "normalize_structure_family",
    "element_symbols",
    "material_auid",
    "recipe_auid",
    "comp_auid",
    "build_recipe_id",
    "split_recipe_id",
    "build_comp_id",
    "split_comp_id",
    "material_auid_of",
    "is_material_auid",
    "is_recipe_id",
    "is_comp_auid",
    "auid_kind",
    "LIT_PREFIX",
    "lit_auid",
]
