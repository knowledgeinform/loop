"""Validation + normalization for batch uploads — the single source of truth for
the record schema and typed ``synthesis_steps`` shape. Each ``normalize_*``
returns ``(normalized, errors)``; a non-empty ``errors`` rejects the record."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .auid import STRUCTURE_FAMILY_VALUES
from .documents import (
    _normalize_structure_family,
    compute_material_auid,
    normalize_elements_payload,
)

NA = "na"

PHASE_STATUS_VALUES = ("single_phase", "multi_phase", "not_confirmed")
RAW_DATA_TYPE_VALUES = ("xrd", "sem", "tem", "eds", "other", "unknown", "na")

# Synthesis-step output schema: ``step_type`` -> ``{field_name: kind}``. ``kind``
# is one of ``float``, ``int``, ``ratio``, ``str``, ``list``. These mirror the
# per-type keys produced by ``_parse_synthesis_steps_from_request`` in
# ``catalog/views.py`` — the JSON ``synthesis_steps[]`` entries use this shape.
STEP_FIELD_SCHEMA: Dict[str, Dict[str, str]] = {
    "ball_milling": {
        "milling_time_hours": "float",
        "milling_rpm": "float",
        "ball_powder_ratio": "ratio",
        "atmosphere": "str",
        "jar_material": "str",
        "ball_material": "str",
        "process_control_agent": "str",
    },
    "weighing": {
        "total_mass_g": "float",
        "precursors": "str",
        "precursors_list": "list",
    },
    "mixing": {"mixing_time_min": "float", "mixing_method": "str"},
    "pelletizing": {
        "pressure_mpa": "float",
        "hold_time_min": "float",
        "die_diameter_mm": "float",
        "lubricant": "str",
    },
    "heat_treatment": {
        "max_temp_c": "float",
        "ramp_rate_c_min": "float",
        "hold_time_hours": "float",
        "atmosphere": "str",
        "furnace_type": "str",
        "o2_partial_pressure_bar": "float",
    },
    "annealing": {
        "temperature_c": "float",
        "duration_hours": "float",
        "atmosphere": "str",
    },
    "arc_melting": {
        "current_a": "float",
        "number_of_remelts": "int",
        "hearth_material": "str",
        "atmosphere": "str",
    },
    "quenching": {"quenching_medium": "str", "medium_temperature_c": "float"},
    "cooling": {"cooling_method": "str", "cooling_rate_c_min": "float"},
    "grinding": {"grinding_method": "str", "final_particle_size": "str"},
    "xrd_measurement": {
        "radiation": "str",
        "two_theta_range": "str",
        "step_size_deg": "float",
        "scan_speed_deg_min": "float",
    },
    "other": {"description": "str"},
    "unknown": {},
    "na": {},
}
STEP_TYPE_VALUES = tuple(STEP_FIELD_SCHEMA.keys())

_PRECURSOR_KEYS = ("cas_number", "name", "formula", "purity", "supplier", "notes")
_RATIO_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$")

# Ways an extractor writes "the paper does not report this". Treated as absent
# rather than as a malformed value, so a record is not rejected for honestly
# recording a gap. Deliberately narrow: only unambiguous placeholders, never a
# value that could be a real measurement.
_NOT_RECORDED = frozenset({NA, "n/a", "none", "null", "unknown", "-", "--"})


# ---------------------------------------------------------------------------
# Small coercion helpers (self-contained to avoid importing from views.py,
# which imports this module).
# ---------------------------------------------------------------------------

def _coerce_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("true", "1", "yes", "y", "t")


def _text_or_na(value) -> str:
    text = str(value or "").strip()
    return text or NA


def _normalize_ratio(value) -> str:
    """Canonicalize a ``x:y`` ratio string; raise ``ValueError`` on bad input.

    Placeholders meaning "the paper does not report this" normalize to empty
    rather than raising. This module already writes ``NA`` into other fields
    itself (see ``_text_or_na``) and accepts it for atmosphere, furnace type and
    element sites, so rejecting it here alone was an inconsistency: an extractor
    filling every unknown field the same way had rows silently dropped from an
    import for one field out of many.
    """
    raw = str(value or "").strip()
    if not raw or raw.lower() in _NOT_RECORDED:
        return ""
    match = _RATIO_RE.match(raw)
    if not match:
        raise ValueError(f"ratio must be in x:y form (e.g. 10:1); got {raw!r}")
    return f"{match.group(1)}:{match.group(2)}"


def _normalize_authors(value) -> List[str]:
    if isinstance(value, list):
        return [str(a).strip() for a in value if str(a).strip()]
    return [a.strip() for a in str(value or "").split(",") if a.strip()]


def _normalize_element_sites(raw: Dict[str, Any]) -> Dict[str, str]:
    data = raw.get("element_sites")
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items() if k and v}
    return {}


def _clean_precursors_list(value) -> List[Dict[str, str]]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            continue
        row = {
            k: str(item.get(k) or "").strip()
            for k in _PRECURSOR_KEYS
            if item.get(k)
        }
        if row:
            cleaned.append(row)
    return cleaned


# ---------------------------------------------------------------------------
# Shared pieces
# ---------------------------------------------------------------------------

def normalize_synthesis_steps(raw_steps) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Normalize a JSON ``synthesis_steps`` list into stored step dicts."""
    errors: List[str] = []
    if raw_steps in (None, "", []):
        return [], errors
    if not isinstance(raw_steps, list):
        return [], ["'synthesis_steps' must be a list"]

    steps: List[Dict[str, Any]] = []
    for idx, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            errors.append(f"Step {idx} must be a JSON object")
            continue
        step_type = str(raw.get("step_type") or "other").strip().lower()
        if step_type not in STEP_FIELD_SCHEMA:
            errors.append(
                f"Step {idx}: invalid step_type {step_type!r} "
                f"(allowed: {', '.join(STEP_TYPE_VALUES)})"
            )
            continue

        step: Dict[str, Any] = {"step_number": idx, "step_type": step_type}
        notes = raw.get("notes")
        if notes not in (None, ""):
            step["notes"] = str(notes)

        for field, kind in STEP_FIELD_SCHEMA[step_type].items():
            val = raw.get(field)
            if val in (None, ""):
                continue
            if kind == "float":
                num = _coerce_float(val)
                if num is None:
                    errors.append(f"Step {idx}: {field} must be a number")
                else:
                    step[field] = num
            elif kind == "int":
                num = _coerce_int(val)
                if num is None:
                    errors.append(f"Step {idx}: {field} must be an integer")
                else:
                    step[field] = num
            elif kind == "ratio":
                try:
                    normalized = _normalize_ratio(val)
                except ValueError as exc:
                    errors.append(f"Step {idx}: {exc}")
                else:
                    if normalized:
                        step[field] = normalized
            elif kind == "list":
                cleaned = _clean_precursors_list(val)
                if cleaned:
                    step[field] = cleaned
            else:  # str
                step[field] = str(val)

        steps.append(step)
    return steps, errors


def _resolve_composition(
    raw: Dict[str, Any],
    locked_elements: Optional[Dict[str, Any]],
    locked_structure: Optional[str],
    errors: List[str],
) -> Tuple[Dict[str, float], Optional[str]]:
    """Resolve ``(raw_elements, structure_family)`` honoring a locked material."""
    # Structure family.
    structure_family: Optional[str] = None
    if locked_structure:
        try:
            structure_family = _normalize_structure_family(locked_structure)
        except ValueError:
            errors.append(f"Invalid locked structure_family: {locked_structure!r}")
    else:
        sf_raw = raw.get("structure_family")
        if sf_raw in (None, ""):
            errors.append("Missing required field: structure_family")
        else:
            try:
                structure_family = _normalize_structure_family(sf_raw)
            except ValueError:
                errors.append(
                    f"Invalid structure_family: {sf_raw!r} "
                    f"(allowed: {', '.join(sorted(STRUCTURE_FAMILY_VALUES))})"
                )

    # Elements.
    raw_elements: Dict[str, float] = {}
    if locked_elements:
        try:
            raw_elements = normalize_elements_payload(locked_elements)
        except ValueError as exc:
            errors.append(f"locked elements: {exc}")
    else:
        els = raw.get("elements")
        if els in (None, "", {}, []):
            errors.append("Missing required field: elements")
        else:
            try:
                raw_elements = normalize_elements_payload(els)
            except ValueError as exc:
                errors.append(f"elements: {exc}")

    # Locked-composition mismatch check: a file that explicitly carries a
    # different composition than the page lock is rejected.
    if (
        not errors
        and locked_elements
        and locked_structure
        and structure_family
        and (raw.get("elements") or raw.get("structure_family"))
    ):
        try:
            file_elements = (
                normalize_elements_payload(raw["elements"])
                if raw.get("elements")
                else raw_elements
            )
            file_structure = (
                _normalize_structure_family(raw["structure_family"])
                if raw.get("structure_family")
                else structure_family
            )
            locked_auid = compute_material_auid(raw_elements, structure_family)
            file_auid = compute_material_auid(file_elements, file_structure)
            if file_auid != locked_auid:
                errors.append(
                    "Composition in file does not match the locked material "
                    "for this page; remove the elements/structure_family keys "
                    "or upload from the unlocked add page."
                )
        except ValueError:
            # The file's own composition is malformed; surface as an error.
            errors.append("File composition could not be validated against the page lock.")

    return raw_elements, structure_family


def _material_auid_or_none(raw_elements, structure_family) -> Optional[str]:
    if not raw_elements or not structure_family:
        return None
    try:
        return compute_material_auid(raw_elements, structure_family)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Public normalizers
# ---------------------------------------------------------------------------

def normalize_experiment_payload(
    raw,
    *,
    locked_elements: Optional[Dict[str, Any]] = None,
    locked_structure: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Validate + normalize one experimental record."""
    if not isinstance(raw, dict):
        return {}, ["Record must be a JSON object"]

    errors: List[str] = []
    raw_elements, structure_family = _resolve_composition(
        raw, locked_elements, locked_structure, errors
    )

    phase_raw = str(raw.get("phase_status") or "").strip().lower()
    if not phase_raw:
        errors.append("Missing required field: phase_status")
    elif phase_raw not in PHASE_STATUS_VALUES:
        errors.append(
            f"Invalid phase_status: {raw.get('phase_status')!r} "
            f"(allowed: {', '.join(PHASE_STATUS_VALUES)})"
        )

    steps, step_errors = normalize_synthesis_steps(raw.get("synthesis_steps"))
    errors.extend(step_errors)

    raw_data_type = str(raw.get("raw_data_type") or "xrd").strip().lower()
    if raw_data_type not in RAW_DATA_TYPE_VALUES:
        raw_data_type = "xrd"

    normalized = {
        "raw_elements": raw_elements,
        "structure_family": structure_family,
        "synthesis_steps": steps,
        "phase_status": phase_raw if phase_raw in PHASE_STATUS_VALUES else "not_confirmed",
        "spacegroup": (str(raw.get("spacegroup") or "").strip() or "unknown"),
        "element_sites": _normalize_element_sites(raw),
        "raw_data_type": raw_data_type,
        "notes": _text_or_na(raw.get("comments")),
        "material_auid": _material_auid_or_none(raw_elements, structure_family),
    }
    return normalized, errors


def normalize_literature_payload(
    raw,
    *,
    locked_elements: Optional[Dict[str, Any]] = None,
    locked_structure: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Validate + normalize one literature record."""
    if not isinstance(raw, dict):
        return {}, ["Record must be a JSON object"]

    errors: List[str] = []
    raw_elements, structure_family = _resolve_composition(
        raw, locked_elements, locked_structure, errors
    )

    doi = str(raw.get("doi") or "").strip()
    if not doi:
        errors.append("Missing required field: doi")

    ss_raw = raw.get("synthesis_successful")
    if ss_raw is None or (isinstance(ss_raw, str) and ss_raw.strip() == ""):
        errors.append("Missing required field: synthesis_successful")
        synthesis_successful = False
    else:
        synthesis_successful = _coerce_bool(ss_raw)

    steps, step_errors = normalize_synthesis_steps(raw.get("synthesis_steps"))
    errors.extend(step_errors)

    normalized = {
        "raw_elements": raw_elements,
        "structure_family": structure_family,
        "synthesis_steps": steps,
        "doi": doi,
        "synthesis_successful": synthesis_successful,
        "title": _text_or_na(raw.get("title")),
        "authors": _normalize_authors(raw.get("authors")),
        "journal": _text_or_na(raw.get("journal")),
        "year": _coerce_int(raw.get("year")),
        "findings": _text_or_na(raw.get("findings")),
        "spacegroup": str(raw.get("spacegroup") or "unknown") or "unknown",
        "element_sites": _normalize_element_sites(raw),
        "material_auid": _material_auid_or_none(raw_elements, structure_family),
    }
    return normalized, errors


def normalize_record(
    record_type: str,
    raw,
    *,
    locked_elements: Optional[Dict[str, Any]] = None,
    locked_structure: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Dispatch to the experiment/literature normalizer by ``record_type``."""
    if record_type == "experiment":
        return normalize_experiment_payload(
            raw, locked_elements=locked_elements, locked_structure=locked_structure
        )
    if record_type == "literature":
        return normalize_literature_payload(
            raw, locked_elements=locked_elements, locked_structure=locked_structure
        )
    return {}, [f"Unknown record type: {record_type!r}"]
