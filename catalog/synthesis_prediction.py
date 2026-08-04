"""Composition-specific synthesis fallback with explicit uncertainty.

The framework is intentionally an estimator, not a virtual experiment.  It
combines precursor thermal proxies, the Tammann rule, structure/oxidation-state
constraints, and available DFT/AFLOW evidence.  Every result is stored apart
from verified recipes so predictions cannot masquerade as ground truth.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Mapping, Optional

from .documents import SynthesisPrediction


MODEL_VERSION = "loop-synthesis-framework-v1"
ROST_URL = "https://www.nature.com/articles/ncomms9485"
CONTROLLED_HEO_URL = "https://www.nature.com/articles/s41467-025-63567-z"
SOL_GEL_URL = "https://pubs.acs.org/doi/10.1021/acsami.0c11899"
TAMMANN_ML_URL = "https://pmc.ncbi.nlm.nih.gov/articles/PMC9407029/"

# Common commercially available oxide starting points.  A decomposition
# temperature is never substituted into the Tammann equation as a melting
# point; it instead creates a precursor-conversion step and an upper-risk flag.
_OXIDE_PRECURSORS: Dict[str, Dict[str, Any]] = {
    "Sc": {"formula": "Sc2O3", "cation_count": 2, "melting_c": 2485.0},
    "Ti": {"formula": "TiO2", "cation_count": 1, "melting_c": 1843.0},
    "V": {"formula": "V2O5", "cation_count": 2, "melting_c": 690.0, "volatile": True},
    "Cr": {"formula": "Cr2O3", "cation_count": 2, "melting_c": 2435.0},
    "Mn": {"formula": "MnO2", "cation_count": 1, "decomposes_c": 535.0},
    "Fe": {"formula": "Fe2O3", "cation_count": 2, "melting_c": 1565.0},
    "Co": {"formula": "Co3O4", "cation_count": 3, "decomposes_c": 895.0},
    "Ni": {"formula": "NiO", "cation_count": 1, "melting_c": 1955.0},
    "Cu": {"formula": "CuO", "cation_count": 1, "decomposes_c": 1026.0},
    "Zn": {"formula": "ZnO", "cation_count": 1, "melting_c": 1975.0, "volatile": True},
}

_OXIDATION_SENSITIVE = {"Ti", "V", "Cr", "Mn", "Fe"}


def _safe_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalized_cations(elements: Mapping[str, Any]) -> Dict[str, float]:
    raw: Dict[str, float] = {}
    for symbol, value in (elements or {}).items():
        if str(symbol) == "O":
            continue
        number = _safe_number(value)
        if number is not None and number > 0:
            raw[str(symbol)] = number
    total = sum(raw.values())
    return {symbol: value / total for symbol, value in raw.items()} if total else {}


def _precursor_plan(cations: Mapping[str, float], wet_chemical: bool) -> list[Dict[str, Any]]:
    plan: list[Dict[str, Any]] = []
    for symbol in sorted(cations):
        fraction = cations[symbol]
        oxide = _OXIDE_PRECURSORS.get(symbol, {})
        if wet_chemical:
            plan.append({
                "element": symbol,
                "formula": f"{symbol} nitrate (hydrate state to be specified)",
                "target_cation_fraction": round(fraction, 6),
                "moles_per_cation_basis": round(fraction, 6),
                "requires_mass_recalculation_from_certificate": True,
            })
            continue
        cation_count = int(oxide.get("cation_count") or 1)
        row = {
            "element": symbol,
            "formula": oxide.get("formula") or f"{symbol} oxide",
            "target_cation_fraction": round(fraction, 6),
            "moles_per_cation_basis": round(fraction / cation_count, 6),
        }
        if oxide.get("melting_c") is not None:
            row["melting_c"] = oxide["melting_c"]
        if oxide.get("decomposes_c") is not None:
            row["decomposes_c"] = oxide["decomposes_c"]
        if oxide.get("volatile"):
            row["volatility_risk"] = True
        plan.append(row)
    return plan


def _temperature_estimate(
    precursors: list[Dict[str, Any]],
    *,
    wet_chemical: bool,
    structure_family: str,
    d2h: Optional[float],
) -> Dict[str, Any]:
    melting = [float(row["melting_c"]) for row in precursors if row.get("melting_c") is not None]
    if melting:
        # Use the mean precursor melting point as the multi-component kinetic
        # proxy, while also retaining the user's conventional 0.5*T_m lower
        # bound for the lowest-melting precursor.
        tammann_low = 0.5 * (min(melting) + 273.15) - 273.15
        tammann_mean = 0.5 * (sum(melting) / len(melting) + 273.15) - 273.15
    else:
        tammann_low = 500.0
        tammann_mean = 650.0

    family = (structure_family or "unknown").lower()
    family_floor = {
        "rocksalt": 850.0,
        "spinel": 650.0,
        "perovskite": 750.0,
        "fluorite": 600.0,
        "pyrochlore": 800.0,
    }.get(family, 700.0)
    kinetic_offset = 75.0 if wet_chemical else 125.0
    center = max(family_floor, tammann_mean + kinetic_offset)
    stability_adjustment = 0.0
    if d2h is not None:
        if d2h >= 0.15:
            stability_adjustment = 100.0
        elif d2h >= 0.08:
            stability_adjustment = 50.0
        elif d2h <= 0.02:
            stability_adjustment = -25.0
    center += stability_adjustment
    if wet_chemical:
        center = min(center, 950.0)
    center = min(max(center, 450.0), 1200.0)
    center = round(center / 25.0) * 25.0
    half_width = 50.0 if wet_chemical else 75.0
    return {
        "low": round(center - half_width, 1),
        "high": round(center + half_width, 1),
        "average": round(center, 1),
        "count": 0,
        "tammann_min_c": round(tammann_low, 1),
        "tammann_mean_c": round(tammann_mean, 1),
        "stability_adjustment_c": stability_adjustment,
        "is_estimate": True,
    }


def build_framework_prediction(
    elements: Mapping[str, Any],
    structure_family: str,
    *,
    d2h: Optional[float] = None,
    experimental_probability: Optional[float] = None,
    aflow_summary: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a material-specific, auditable route when no recipe is known."""
    cations = _normalized_cations(elements)
    if not cations:
        raise ValueError("A synthesis prediction requires at least one non-oxygen element.")
    d2h = _safe_number(d2h)
    experimental_probability = _safe_number(experimental_probability)
    aflow_summary = dict(aflow_summary or {})

    family = (structure_family or "unknown").lower()
    # High metastability and nanoparticle-oriented structures benefit most
    # from precursor-level atomic mixing.  Otherwise retain the established
    # bulk-ceramic solid-state route.
    wet_chemical = family in {"spinel", "fluorite"} or (d2h is not None and d2h >= 0.15)
    methodology = "citrate sol-gel" if wet_chemical else "solid-state ceramic"
    precursors = _precursor_plan(cations, wet_chemical)
    temperature = _temperature_estimate(
        precursors,
        wet_chemical=wet_chemical,
        structure_family=family,
        d2h=d2h,
    )

    sensitive = sorted(set(cations) & _OXIDATION_SENSITIVE)
    if sensitive and family == "rocksalt":
        atmosphere = "Flowing Ar; tune oxygen partial pressure after phase-diagram/CALPHAD review"
    elif "V" in cations:
        atmosphere = "Air or controlled O2; verify vanadium oxidation state by TGA/XRD"
    else:
        atmosphere = "Air"

    metastable = d2h is not None and d2h >= 0.08
    cooling_method = (
        "Rapid quench under the synthesis atmosphere"
        if metastable or family == "rocksalt"
        else "Controlled furnace cooling at 3–5 °C/min"
    )
    hold_hours = 4.0 if wet_chemical else 5.0
    route_steps: list[Dict[str, Any]]
    if wet_chemical:
        route_steps = [
            {"step_type": "Precursor weighing", "precursors_list": precursors, "notes": "Use the listed cation fractions; hydrate/purity corrections are required before weighing."},
            {"step_type": "Solution mixing", "notes": "Dissolve nitrate precursors separately, combine, then add citric acid as complexant."},
            {"step_type": "Sol-gel formation", "notes": "Adjust pH for a stable mixed solution; heat while stirring until a homogeneous gel forms."},
            {"step_type": "Organic burnout", "temperature_c": 300.0, "duration_hours": 3.0, "atmosphere": "Air"},
            {"step_type": "Calcination", "temperature_c": temperature["average"], "duration_hours": hold_hours, "atmosphere": atmosphere},
            {"step_type": "Cooling", "cooling_method": cooling_method},
            {"step_type": "Validation", "notes": "Run XRD and TGA/DSC; adjust the calcination window to the final reaction peak and stable phase field."},
        ]
        literature_url = SOL_GEL_URL
    else:
        route_steps = [
            {"step_type": "Precursor weighing", "precursors_list": precursors, "notes": "Amounts are per one mole of total cations; correct for precursor purity."},
            {"step_type": "High-energy ball milling", "duration_hours": 2.5, "notes": "Use oxide-compatible media and prevent cross-contamination."},
            {"step_type": "Pellet pressing", "pressure_mpa": 80.0, "hold_time_seconds": 30.0},
            {"step_type": "Sintering", "temperature_c": temperature["average"], "duration_hours": hold_hours, "atmosphere": atmosphere},
            {"step_type": "Cooling", "cooling_method": cooling_method},
            {"step_type": "Validation", "notes": "Run XRD and TGA/DSC; revise temperature/atmosphere against relevant phase diagrams or CALPHAD before execution."},
        ]
        literature_url = CONTROLLED_HEO_URL if sensitive else ROST_URL

    evidence = [
        {"kind": "method", "label": f"{methodology} HEO precedent", "url": literature_url},
        {"kind": "temperature", "label": "Tammann/precursor-stability estimate", "url": TAMMANN_ML_URL},
    ]
    if d2h is not None:
        evidence.append({"kind": "thermodynamics", "label": "LOOP DFT/model hull-distance input", "value": d2h})
    if aflow_summary:
        evidence.append({"kind": "thermodynamics", "label": "Cached AFLOW exact-species summary", "value": aflow_summary})
    if experimental_probability is not None:
        evidence.append({"kind": "experiment", "label": "ChemScreen neighbor success probability", "value": experimental_probability})

    confidence = 0.35
    confidence += 0.10 if d2h is not None else 0.0
    confidence += 0.08 if aflow_summary else 0.0
    confidence += 0.07 if experimental_probability is not None else 0.0
    confidence = min(confidence, 0.65)
    assumptions = [
        "No exact validated LOOP recipe was available.",
        "The temperature is a screening window, not a measured reaction temperature.",
        "TGA/DSC and binary/ternary phase-field validation are required before laboratory use.",
    ]
    payload = {
        "target_elements": sorted(elements),
        "structure_family": family or "unknown",
        "model": MODEL_VERSION,
        "methodology": methodology,
        "precursors": precursors,
        "route_steps": route_steps,
        "temperature": temperature,
        "atmosphere": atmosphere,
        "cooling_method": cooling_method,
        "evidence": evidence,
        "assumptions": assumptions,
        "confidence": round(confidence, 2),
        "prediction_status": "predicted",
        "training_weight": 0.2,
        "source_label": f"{MODEL_VERSION} · {round(confidence * 100)}% confidence",
        "source_url": literature_url,
        "source_detail": assumptions[-1],
        "temperature_source_label": "Tammann + structure + hull-stability estimate",
        "temperature_source_url": TAMMANN_ML_URL,
        "is_framework_prediction": True,
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    payload["source_hash"] = hashlib.sha256(encoded).hexdigest()
    return payload


def stored_prediction(material_auid: str) -> Optional[Dict[str, Any]]:
    record = SynthesisPrediction.objects(id=str(material_auid)).first()
    if record is None or record.prediction_status == "rejected":
        return None
    return {
        "target_elements": sorted(record.elements or {}),
        "structure_family": record.structure_family,
        "model": record.model_version,
        "methodology": record.methodology,
        "precursors": list(record.precursors or []),
        "route_steps": list(record.route_steps or []),
        "temperature": dict(record.temperature or {}),
        "atmosphere": record.atmosphere,
        "cooling_method": record.cooling_method,
        "evidence": list(record.evidence or []),
        "assumptions": list(record.assumptions or []),
        "confidence": record.confidence,
        "prediction_status": record.prediction_status,
        "training_weight": record.training_weight,
        "source_label": f"{record.model_version} · {round(record.confidence * 100)}% confidence",
        "source_detail": (record.assumptions or [""])[-1],
        "temperature_source_label": "Tammann + structure + hull-stability estimate",
        "temperature_source_url": TAMMANN_ML_URL,
        "is_framework_prediction": True,
    }


def store_prediction(material_auid: str, elements: Mapping[str, Any], prediction: Mapping[str, Any]) -> SynthesisPrediction:
    record = SynthesisPrediction.objects(id=str(material_auid)).first()
    created_at = record.created_at if record is not None else None
    record = SynthesisPrediction(
        id=str(material_auid),
        material_auid=str(material_auid),
        elements=dict(elements),
        structure_family=str(prediction.get("structure_family") or "unknown"),
        methodology=str(prediction.get("methodology") or ""),
        precursors=list(prediction.get("precursors") or []),
        route_steps=list(prediction.get("route_steps") or []),
        temperature=dict(prediction.get("temperature") or {}),
        atmosphere=str(prediction.get("atmosphere") or ""),
        cooling_method=str(prediction.get("cooling_method") or ""),
        evidence=list(prediction.get("evidence") or []),
        assumptions=list(prediction.get("assumptions") or []),
        confidence=float(prediction.get("confidence") or 0.0),
        prediction_status=str(prediction.get("prediction_status") or "predicted"),
        training_eligible=True,
        training_weight=float(prediction.get("training_weight") or 0.2),
        model_version=str(prediction.get("model") or MODEL_VERSION),
        source_hash=str(prediction.get("source_hash") or ""),
        validation=dict(record.validation or {}) if record is not None else {},
    )
    if created_at is not None:
        record.created_at = created_at
    record.save(force_insert=False)
    return record


__all__ = [
    "MODEL_VERSION",
    "build_framework_prediction",
    "store_prediction",
    "stored_prediction",
]
