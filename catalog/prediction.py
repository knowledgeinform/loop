from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .documents import Recipe, SynthesisPrediction
from .composition_model import get_composition_model
from .rietveld_refinement import _parse_formula

_FIELD_LABELS: dict[str, str] = {
    "milling_time_hours": "Milling time",
    "milling_rpm": "Milling speed",
    "ball_powder_ratio": "Ball:Powder ratio",
    "atmosphere": "Atmosphere",
    "jar_material": "Jar material",
    "ball_material": "Ball material",
    "process_control_agent": "Process control agent",
    "temperature_c": "Temperature",
    "max_temp_c": "Max temperature",
    "anneal_temp": "Anneal temperature",
    "duration_hours": "Duration",
    "hold_time_hours": "Hold time",
    "ramp_rate_c_min": "Ramp rate",
    "quenching_medium": "Quenching medium",
    "medium_temperature_c": "Quench temperature",
    "cooling_method": "Cooling method",
    "cooling_rate_c_min": "Cooling rate",
    "total_mass_g": "Total mass",
    "mixing_time_min": "Mixing time",
    "pressure_mpa": "Pressure",
    "final_particle_size": "Particle size",
    "current_a": "Arc current",
    "number_of_remelts": "Remelts",
    "spacegroup": "Space group",
}

_TEMP_KEY_RE = re.compile(r"temp|temperature", re.I)
_SYMBOL_SPLIT_RE = re.compile(r"[\s,;/-]+")


def parse_composition_input(formula: str, elements: str) -> Dict[str, float]:
    formula = (formula or "").strip()
    elements = (elements or "").strip()

    if formula:
        try:
            parsed = _parse_formula(formula)
        except Exception as exc:
            raise ValueError(f"Unable to parse formula: {exc}") from exc
        return parsed

    if not elements:
        raise ValueError("Enter a chemical formula or a comma-separated element list.")

    symbols = [sym.strip() for sym in _SYMBOL_SPLIT_RE.split(elements) if sym.strip()]
    if not symbols:
        raise ValueError("Enter a chemical formula or a comma-separated element list.")

    return {symbol: 1.0 for symbol in symbols}


def _extract_temperatures_from_step(step: Any) -> List[float]:
    if not isinstance(step, dict):
        return []
    temps: List[float] = []
    for key, value in step.items():
        if not isinstance(value, (int, float)):
            continue
        if _TEMP_KEY_RE.search(key):
            temps.append(float(value))
    return temps


def _record_steps(record: Any) -> List[Dict[str, Any]]:
    return list(
        getattr(record, "synthesis_steps", None)
        or getattr(record, "route_steps", None)
        or []
    )


def _aggregate_recipe_temperatures(recipe: Any) -> List[float]:
    temps: List[float] = []
    stored_temperature = getattr(recipe, "temperature", None) or {}
    if isinstance(stored_temperature, dict) and stored_temperature.get("average") is not None:
        temps.append(float(stored_temperature["average"]))
    for step in _record_steps(recipe):
        temps.extend(_extract_temperatures_from_step(step))
    for trial in getattr(recipe, "trials", None) or []:
        exp_condition = getattr(trial, "exp_condition", None)
        if not exp_condition:
            continue
        additional = getattr(exp_condition, "additional_params", None) or {}
        for step in additional.get("synthesis_steps", []) or []:
            temps.extend(_extract_temperatures_from_step(step))
    for literature in getattr(recipe, "literature", None) or []:
        exp_condition = getattr(literature, "exp_condition", None)
        if not exp_condition:
            continue
        additional = getattr(exp_condition, "additional_params", None) or {}
        for step in additional.get("synthesis_steps", []) or []:
            temps.extend(_extract_temperatures_from_step(step))
    return temps


def _humanize_field(field: str) -> str:
    return _FIELD_LABELS.get(field, field.replace("_", " ").title())


def _format_route_steps(synthesis_steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    for step in synthesis_steps or []:
        step_type = str(step.get("step_type") or "Unknown").replace("_", " ").title()
        details: List[str] = []
        for key in sorted(step.keys()):
            if key in ("step_type", "step_number"):
                continue
            value = step.get(key)
            if value is None or value == "":
                continue
            if key == "notes":
                details.append(f"Notes: {value}")
                continue
            details.append(f"{_humanize_field(key)}: {value}")
        steps.append({"step_type": step_type, "details": details})
    return steps


def _format_temperature_range(temperatures: List[float]) -> Optional[Dict[str, Any]]:
    if not temperatures:
        return None
    temperatures = sorted(temperatures)
    low = temperatures[0]
    high = temperatures[-1]
    average = sum(temperatures) / len(temperatures)
    return {
        "low": round(low, 1),
        "high": round(high, 1),
        "average": round(average, 1),
        "count": len(temperatures),
    }


def _build_support_record(recipe: Any, score: float) -> Dict[str, Any]:
    temps = _aggregate_recipe_temperatures(recipe)
    if isinstance(recipe, SynthesisPrediction):
        source = f"Predicted pseudo-label ({recipe.model_version})"
    elif recipe.trials:
        source = "Experimental"
    elif recipe.literature:
        source = "Literature"
    else:
        source = "Computational"

    step_types = [
        str(step.get("step_type") or "").replace("_", " ").title()
        for step in _record_steps(recipe)
        if step and step.get("step_type")
    ]
    return {
        "recipe_auid": str(recipe.id),
        "material_auid": recipe.material_auid,
        "structure_family": recipe.structure_family,
        "source": source,
        "score": round(score, 3),
        "temperatures": temps,
        "step_types": " → ".join(step_types) if step_types else "N/A",
    }


def _collect_candidates(
    elements: Dict[str, float],
    structure_family: Optional[str],
    limit: int = 12,
) -> List[Tuple[Any, float]]:
    return get_composition_model().nearest(elements, structure_family, limit=limit)


def predict_synthesis_route(
    elements: Dict[str, float],
    structure_family: Optional[str],
) -> Dict[str, Any]:
    target_symbols = sorted(elements.keys())
    model = get_composition_model()
    candidates = _collect_candidates(elements, structure_family)

    prediction: Dict[str, Any] = {
        "target_elements": target_symbols,
        "structure_family": structure_family or "unknown",
        "model": "composition k-nearest neighbors",
        "training_record_count": model.training_record_count,
        "candidate_count": len(candidates),
        "top_match_score": 0.0,
        "route_steps": [],
        "temperature": None,
        "supporting_records": [],
    }

    if not candidates:
        return prediction

    prediction["top_match_score"] = round(candidates[0][1], 3)
    prediction["supporting_records"] = [_build_support_record(recipe, score) for recipe, score in candidates[:10]]

    # Prefer the closest route that also carries temperature evidence.  The
    # route and temperature must come from the same process record; averaging
    # temperatures across unrelated routes can recommend an impossible
    # combination (for example, an arc-melting route at a ball-milling
    # temperature).
    route_recipe = next(
        (
            recipe
            for recipe, _ in candidates
            if _record_steps(recipe) and _aggregate_recipe_temperatures(recipe)
        ),
        None,
    )
    if route_recipe is None:
        route_recipe = next(
            (recipe for recipe, _ in candidates if _record_steps(recipe)),
            candidates[0][0],
        )

    if _record_steps(route_recipe):
        prediction["route_steps"] = _format_route_steps(_record_steps(route_recipe))
    else:
        prediction["route_steps"] = []

    all_temperatures = _aggregate_recipe_temperatures(route_recipe)
    if all_temperatures:
        prediction["temperature_source"] = route_recipe.id
    else:
        for recipe, _ in candidates:
            all_temperatures.extend(_aggregate_recipe_temperatures(recipe))
        if all_temperatures:
            prediction["temperature_source"] = "nearest recipe ensemble"

    prediction["temperature"] = _format_temperature_range(all_temperatures)
    if isinstance(route_recipe, SynthesisPrediction):
        prediction.update({
            "model": route_recipe.model_version,
            "methodology": route_recipe.methodology,
            "confidence": route_recipe.confidence,
            "source_label": (
                f"{route_recipe.model_version} pseudo-label · "
                f"{round(float(route_recipe.confidence or 0) * 100)}% confidence"
            ),
            "source_detail": "Generated prediction; laboratory validation is still required.",
        })
    return prediction
