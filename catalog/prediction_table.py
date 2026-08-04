"""Natural-language prediction table backed by every LOOP record type.

The prediction workspace deliberately has one input: a materials question.
This module turns that question into a small query intent, ranks saved
computational/ML values, and enriches each result with recipe-derived
structure, synthesis-route, and temperature predictions.

``EmbeddedDFT.ml_predictions`` is the schema-free integration boundary for
Bellatrix and other prediction models.  Property lookup is case/format
insensitive so model payloads do not have to use LOOP's typed field names.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from django.conf import settings

from .chemscreen import ChemScreenExperimentalOutcome, iter_experimental_outcomes
from .composition_model import _composition_similarity, _normalized_amounts
from .documents import Material, Recipe
from .permissions import is_visible_to_user, visible_recipe_children
from .prediction import parse_composition_input, predict_synthesis_route
from .synthesis_prediction import (
    build_framework_prediction,
    store_prediction as store_synthesis_prediction,
    stored_prediction as stored_synthesis_prediction,
)
from .aflow_client import cached_aflow_summary


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


@dataclass(frozen=True)
class PropertySpec:
    key: str
    label: str
    unit: str
    aliases: Tuple[str, ...]
    typed_field: Optional[str] = None
    payload_keys: Tuple[str, ...] = ()


PROPERTY_SPECS: Tuple[PropertySpec, ...] = (
    PropertySpec(
        "thermal_conductivity_300k",
        "Thermal conductivity (300 K)",
        "W/m/K",
        ("thermal conductivity", "conductivity", "kappa"),
        "thermal_conductivity_300k",
        ("agl_thermal_conductivity_300K", "thermal_conductivity", "conductivity", "kappa"),
    ),
    PropertySpec(
        "dft_formation_energy_ev",
        "Formation energy",
        "eV/atom",
        ("formation energy", "formation enthalpy", "enthalpy formation", "enthalpy"),
        "dft_formation_energy_ev",
        ("enthalpy_formation_atom", "formation_energy", "formation_enthalpy"),
    ),
    PropertySpec(
        "dft_hull_distance_ev",
        "Hull distance",
        "eV/atom",
        ("hull distance", "energy above hull", "ehull"),
        "dft_hull_distance_ev",
        ("hull_distance", "energy_above_hull", "ehull"),
    ),
    PropertySpec(
        "dft_bandgap_ev",
        "Band gap",
        "eV",
        ("band gap", "bandgap", "egap"),
        "dft_bandgap_ev",
        ("Egap", "Egap_fit", "band_gap"),
    ),
    PropertySpec(
        "debye_temperature",
        "Debye temperature",
        "K",
        ("debye temperature", "debye"),
        "debye_temperature",
        ("agl_debye", "debye"),
    ),
    PropertySpec(
        "efa",
        "Entropy forming ability (EFA)",
        "",
        ("entropy forming ability", "efa"),
        None,
        ("entropy_forming_ability", "entropy forming ability", "EFA"),
    ),
    PropertySpec(
        "deed",
        "DEED",
        "",
        ("deed",),
        None,
        ("DEED", "deed"),
    ),
    PropertySpec(
        "d2h",
        "d2h",
        "",
        ("d2h",),
        None,
        ("d2h",),
    ),
    PropertySpec(
        "ml_predicted",
        "Model prediction",
        "",
        ("ml predicted", "model prediction", "prediction score", "predicted score"),
        None,
        ("ML_Predicted", "ml_predicted", "prediction"),
    ),
)

_PROPERTY_BY_KEY = {spec.key: spec for spec in PROPERTY_SPECS}
_LOW_WORDS = ("lowest", "minimum", "minimize", "smallest", "least")
_HIGH_WORDS = ("highest", "maximum", "maximize", "largest", "greatest")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_FORMULA_TOKEN_RE = re.compile(
    r"(?:\([A-Za-z0-9.]+\)[A-Za-z]?\d*(?:\.\d+)?)|"
    r"(?:[A-Z][a-z]?(?:\d+(?:\.\d+)?)?){2,}"
)
_ELEMENT_SYMBOLS = frozenset(
    """
    H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni
    Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe
    Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg
    Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg
    Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og
    """.split()
)
_3D_TRANSITION_METALS = frozenset("Sc Ti V Cr Mn Fe Co Ni Cu Zn".split())
_SCREENING_CONCERNS: Dict[str, Tuple[str, int]] = {
    "V": ("V: oxidation-state and oxygen-control sensitivity", 2),
    "Cr": ("Cr: oxidation-state and handling concern", 2),
    "Cu": ("Cu: Jahn–Teller distortion / phase-separation risk", 2),
    "Zn": ("Zn: volatility risk during high-temperature synthesis", 2),
    "Co": ("Co: supply and handling concern", 1),
    "Ni": ("Ni: handling concern", 1),
}
_ROST_ROUTE_SOURCE = {
    "label": "Rost et al., Nature Communications (2015)",
    "url": "https://doi.org/10.1038/ncomms9485",
}
_CONTROLLED_ATMOSPHERE_ROUTE_SOURCE = {
    "label": "Almishal et al., Nature Communications (2025)",
    "url": "https://doi.org/10.1038/s41467-025-63567-z",
}


@dataclass(frozen=True)
class PredictionQuery:
    raw: str
    property_spec: Optional[PropertySpec]
    ascending: bool
    formula: str
    limit: int
    structure_family: str


def _safe_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        match = _NUMBER_RE.search(value.strip())
        if match:
            try:
                number = float(match.group(0))
                return number if math.isfinite(number) else None
            except ValueError:
                return None
    return None


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_mongo"):
        try:
            return value.to_mongo().to_dict()
        except Exception:
            return {}
    return {}


def _find_payload_value(payload: Any, keys: Sequence[str]) -> Tuple[Optional[float], str]:
    wanted = {_normalized_key(key) for key in keys}
    stack: List[Any] = [payload]
    while stack:
        current = stack.pop()
        if not isinstance(current, Mapping):
            continue
        for key, value in current.items():
            if _normalized_key(key) in wanted:
                number = _safe_number(value)
                if number is not None:
                    return number, str(key)
            if isinstance(value, Mapping):
                stack.append(value)
    return None, ""


def dft_property_value(dft: Any, spec: PropertySpec) -> Tuple[Optional[float], str]:
    """Return ``(value, provenance)`` for a typed, DFT, or ML payload field."""
    if spec.typed_field:
        value = _safe_number(_get(dft, spec.typed_field))
        if value is not None:
            return value, "DFT"

    extended_data = _get(dft, "extended_data", {}) or {}
    value, _ = _find_payload_value(extended_data, spec.payload_keys)
    if value is not None:
        calculation_method = str(extended_data.get("calculation_method") or "").strip()
        return value, calculation_method or "DFT"

    value, _ = _find_payload_value(_get(dft, "ml_predictions", {}) or {}, spec.payload_keys)
    if value is not None:
        model = _find_model_label(_get(dft, "ml_predictions", {}) or {})
        return value, model or "ML prediction"
    return None, ""


def _find_model_label(payload: Mapping[str, Any]) -> str:
    for key in ("model", "model_name", "model_version", "source"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _sanitize_formula(formula: str) -> str:
    cleaned = (formula or "").strip().strip(".,;:")
    # Users commonly type the terminal oxygen in an oxide formula as ``o``.
    cleaned = re.sub(r"\)o(?=$|\d)", ")O", cleaned)
    return cleaned


def _extract_formula(prompt: str) -> str:
    def valid(candidate: str) -> bool:
        try:
            parsed = parse_composition_input(candidate, "")
        except (TypeError, ValueError):
            return False
        return bool(parsed) and set(parsed).issubset(_ELEMENT_SYMBOLS)

    stripped = _sanitize_formula(prompt)
    if valid(stripped):
        return stripped
    for match in _FORMULA_TOKEN_RE.finditer(prompt or ""):
        candidate = _sanitize_formula(match.group(0))
        if valid(candidate):
            return candidate
    return ""


def parse_prediction_query(prompt: str, default_limit: int = 10) -> PredictionQuery:
    raw = (prompt or "").strip()
    lowered = raw.lower()
    spec = next(
        (
            candidate
            for candidate in PROPERTY_SPECS
            if any(alias in lowered for alias in candidate.aliases)
        ),
        None,
    )
    ascending = not any(word in lowered for word in _HIGH_WORDS)
    if any(word in lowered for word in _LOW_WORDS):
        ascending = True

    limit = default_limit
    match = re.search(r"\b(?:top|first|show|give\s+me)\s+(\d{1,2})\b", lowered)
    if match:
        limit = max(1, min(int(match.group(1)), 50))

    structure = next(
        (
            family
            for family in ("rocksalt", "spinel", "pyrochlore", "perovskite", "fluorite")
            if family in lowered
        ),
        "",
    )
    return PredictionQuery(
        raw=raw,
        property_spec=spec,
        ascending=ascending,
        formula=_extract_formula(raw),
        limit=limit,
        structure_family=structure,
    )


def format_composition(elements: Mapping[str, Any]) -> str:
    """Render a stable, human-readable formula with oxygen last."""
    values: Dict[str, float] = {}
    for symbol, raw in (elements or {}).items():
        number = _safe_number(raw)
        if number is not None and number > 0:
            values[str(symbol)] = number
    if not values:
        return "Unknown"
    scale = min(values.values())
    normalized = {symbol: value / scale for symbol, value in values.items()}
    ordered = sorted(symbol for symbol in normalized if symbol != "O")
    if "O" in normalized:
        ordered.append("O")
    parts: List[str] = []
    for symbol in ordered:
        value = normalized[symbol]
        rounded = round(value)
        display: str
        if abs(value - rounded) < 1e-6:
            display = "" if rounded == 1 else str(int(rounded))
        else:
            display = f"{value:.4g}"
        parts.append(f"{symbol}{display}")
    return "".join(parts)


def _format_high_entropy_oxide(elements: Mapping[str, Any]) -> str:
    values = {
        str(symbol): _safe_number(raw) or 0.0
        for symbol, raw in (elements or {}).items()
    }
    oxygen = values.get("O", 0.0)
    metals = sorted(symbol for symbol, value in values.items() if symbol != "O" and value > 0)
    metal_total = sum(values[symbol] for symbol in metals)
    if not metals or metal_total <= 0:
        return format_composition(elements)
    cation_parts = "".join(
        f"{symbol}{values[symbol] / metal_total:.3g}" for symbol in metals
    )
    oxygen_ratio = oxygen / metal_total if oxygen > 0 else 0.0
    oxygen_display = (
        ""
        if abs(oxygen_ratio - 1.0) < 1e-6
        else f"{oxygen_ratio:.3g}"
        if oxygen_ratio > 0
        else ""
    )
    return f"({cation_parts})O{oxygen_display}"


def _visible_dfts(material: Any, user_affiliations: Iterable[str]) -> List[Any]:
    return [
        dft
        for dft in (_get(material, "dft_calculations", []) or [])
        if is_visible_to_user(_get(dft, "visibility_affiliations", None), user_affiliations)
    ]


def _best_dft(dfts: Sequence[Any], ranking_spec: Optional[PropertySpec]) -> Any:
    if ranking_spec:
        for dft in dfts:
            if dft_property_value(dft, ranking_spec)[0] is not None:
                return dft
    return max(
        dfts,
        key=lambda dft: sum(
            dft_property_value(dft, spec)[0] is not None for spec in PROPERTY_SPECS
        ),
        default=None,
    )


def _thermodynamic_values(dft: Any, ranking_spec: Optional[PropertySpec]) -> List[Dict[str, Any]]:
    if dft is None:
        return []
    keys = ["dft_formation_energy_ev", "dft_hull_distance_ev", "efa", "deed", "d2h"]
    if ranking_spec and ranking_spec.key not in keys:
        keys.append(ranking_spec.key)
    values: List[Dict[str, Any]] = []
    for key in keys:
        spec = _PROPERTY_BY_KEY[key]
        value, source = dft_property_value(dft, spec)
        if value is None:
            continue
        values.append(
            {
                "key": spec.key,
                "label": spec.label,
                "value": round(value, 6),
                "unit": spec.unit,
                "source": source,
            }
        )
    known = {_normalized_key(item["key"]) for item in values}
    model_payload = _get(dft, "ml_predictions", {}) or {}
    model_label = _find_model_label(model_payload) or "ML prediction"
    for raw_key, raw_value in _as_mapping(model_payload).items():
        key = _normalized_key(raw_key)
        if key in {"model", "modelname", "modelversion", "source"} or key in known:
            continue
        value = _safe_number(raw_value)
        if value is None:
            continue
        values.append(
            {
                "key": key,
                "label": str(raw_key).replace("_", " "),
                "value": round(value, 6),
                "unit": "",
                "source": model_label,
            }
        )
    return values


def _route_text(route_steps: Sequence[Mapping[str, Any]]) -> str:
    names = [str(step.get("step_type") or "").strip() for step in route_steps]
    return " → ".join(name for name in names if name) or "No route estimate"


def _synthesis_route_text(prediction: Mapping[str, Any]) -> str:
    """Expose the parameters that make a generated route composition-specific."""
    methodology = str(prediction.get("methodology") or "").strip()
    if not methodology:
        return _route_text(prediction.get("route_steps") or [])
    precursor_formulas = [
        str(item.get("formula") or "").strip()
        for item in prediction.get("precursors") or []
        if isinstance(item, Mapping) and str(item.get("formula") or "").strip()
    ]
    temperature = prediction.get("temperature") or {}
    center = temperature.get("average")
    atmosphere = str(prediction.get("atmosphere") or "unspecified atmosphere")
    cooling = str(prediction.get("cooling_method") or "unspecified cooling")
    parts = [methodology.title()]
    if precursor_formulas:
        parts.append(" + ".join(precursor_formulas))
    parts.append(_route_text(prediction.get("route_steps") or []))
    if center is not None:
        parts.append(f"{center} °C in {atmosphere}")
    parts.append(cooling)
    return " · ".join(parts)


def derived_d2h(efa: Any, deed: Any) -> Optional[float]:
    """Derive d2h from ChemScreen's DEED = sqrt(EFA / d2h) relation."""
    efa_value = _safe_number(efa)
    deed_value = _safe_number(deed)
    if efa_value is None or deed_value is None or efa_value < 0 or deed_value <= 0:
        return None
    value = efa_value / (deed_value ** 2)
    return value if math.isfinite(value) else None


@lru_cache(maxsize=4)
def _chemscreen_experimental_outcomes(
    root: str,
) -> Tuple[ChemScreenExperimentalOutcome, ...]:
    try:
        return tuple(iter_experimental_outcomes(root))
    except (OSError, ValueError):
        return ()


def predict_experimental_outlook(
    elements: Mapping[str, Any],
    *,
    outcomes: Optional[Sequence[ChemScreenExperimentalOutcome]] = None,
    neighbor_count: int = 5,
) -> Dict[str, Any]:
    """Estimate single-phase likelihood from nearby ChemScreen experiments.

    An exact ChemScreen outcome is reported as measured. Otherwise the
    weighted k-NN percentage is explicitly labelled as an unvalidated
    composition-neighbor estimate.
    """
    if outcomes is None:
        root = str(getattr(settings, "CHEMSCREEN_ROOT", "") or "")
        outcomes = _chemscreen_experimental_outcomes(root) if root else ()
    target = _normalized_amounts(dict(elements or {}))
    scored: List[Tuple[float, ChemScreenExperimentalOutcome]] = []
    for outcome in outcomes:
        score = _composition_similarity(
            target,
            _normalized_amounts(outcome.elements),
        )
        if score > 0:
            scored.append((score, outcome))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return {
            "status": "No ChemScreen outcome model",
            "detail": "Awaiting experimental labels",
            "probability": None,
            "observed": False,
            "source": "",
        }

    best_score, best = scored[0]
    if best_score >= 0.999999:
        return {
            "status": "Single phase" if best.single_phase else "Multi-phase",
            "detail": "ChemScreen experimental result",
            "probability": 100 if best.single_phase else 0,
            "observed": True,
            "source": best.source,
        }

    neighbors = scored[: max(1, neighbor_count)]
    weights = [score ** 4 for score, _ in neighbors]
    total_weight = sum(weights)
    probability = (
        sum(
            weight * (1.0 if outcome.single_phase else 0.0)
            for weight, (_, outcome) in zip(weights, neighbors)
        )
        / total_weight
        if total_weight
        else 0.0
    )
    # With only a small experimental corpus, an unobserved composition should
    # never display false certainty even when all nearest labels agree.
    percent = max(5, min(95, int(round(probability * 100))))
    return {
        "status": f"{percent}% single-phase likelihood",
        "detail": f"ChemScreen {len(neighbors)}-neighbor estimate · unvalidated",
        "probability": percent,
        "observed": False,
        "source": best.source,
        "top_match_score": round(best_score, 3),
    }


def chemscreen_route_prior(
    elements: Mapping[str, Any],
    structure_family: str,
) -> Dict[str, Any]:
    """Return a literature-backed HEO processing prior when recipes are absent."""
    cations = {str(symbol) for symbol in elements if str(symbol) != "O"}
    controlled_atmosphere = bool(cations & {"Ti", "V", "Cr", "Mn", "Fe"})
    if controlled_atmosphere:
        source = _CONTROLLED_ATMOSPHERE_ROUTE_SOURCE
        route_steps = [
            {"step_type": "Equimolar binary oxide mixing"},
            {"step_type": "High-energy ball milling"},
            {"step_type": "Pellet pressing"},
            {"step_type": "Controlled-atmosphere sintering"},
            {"step_type": "Controlled cooling / quench"},
        ]
        temperature = {
            "low": 1100.0,
            "high": 1100.0,
            "average": 1100.0,
            "count": 1,
        }
        atmosphere = (
            "Ar screening prior; oxygen potential needs composition-specific validation"
        )
    else:
        source = _ROST_ROUTE_SOURCE
        route_steps = [
            {"step_type": "Equimolar binary oxide mixing"},
            {"step_type": "Ball milling"},
            {"step_type": "Pellet pressing"},
            {"step_type": "Air annealing"},
            {"step_type": "Air quench"},
        ]
        temperature = {
            "low": 900.0,
            "high": 1000.0,
            "average": 950.0,
            "count": 2,
        }
        atmosphere = "Air-fired rocksalt HEO literature prior"
    return {
        "target_elements": sorted(elements),
        "structure_family": structure_family or "rocksalt",
        "model": "ChemScreen literature synthesis prior",
        "candidate_count": 0,
        "top_match_score": 0.0,
        "route_steps": route_steps,
        "temperature": temperature,
        "supporting_records": [],
        "source_label": source["label"],
        "source_url": source["url"],
        "source_detail": atmosphere,
        "is_literature_prior": True,
    }


def _infer_structure(elements: Mapping[str, Any]) -> str:
    cations = {
        key: _safe_number(value) or 0.0
        for key, value in elements.items()
        if key != "O"
    }
    oxygen = _safe_number(elements.get("O")) or 0.0
    cation_total = sum(cations.values())
    if cation_total <= 0 or oxygen <= 0:
        return "unknown"
    ratio = oxygen / cation_total
    if 1.60 <= ratio <= 1.90 and 3.0 <= cation_total <= 5.0:
        return "pyrochlore"
    if 1.80 <= ratio <= 2.20:
        return "fluorite"
    if 1.35 <= ratio <= 1.65 and len(cations) >= 2:
        return "perovskite"
    if 1.15 <= ratio <= 1.45 and len(cations) >= 2:
        return "spinel"
    if 0.80 <= ratio <= 1.20:
        return "rocksalt"
    return "unknown"


def _recipes_for_material(material_auid: str) -> List[Any]:
    if not material_auid:
        return []
    return list(Recipe.objects(material_auid=material_auid))


def _material_row(
    material: Any,
    *,
    query: PredictionQuery,
    user_affiliations: Iterable[str],
    selected_dft: Any = None,
    composition_override: str = "",
    neighbor_estimate: bool = False,
) -> Dict[str, Any]:
    material_auid = str(_get(material, "id", "") or _get(material, "_id", ""))
    elements = dict(_get(material, "elements", {}) or {})
    dfts = _visible_dfts(material, user_affiliations)
    chosen_dft = selected_dft or _best_dft(dfts, query.property_spec)
    model_only_dfts = [
        dft
        for dft in dfts
        if _get(dft, "ml_predictions", {})
        and "model" in str(_get(dft, "dft_source", "")).lower()
    ]
    observed_dfts = [dft for dft in dfts if dft not in model_only_dfts]
    chemscreen_dft = any(
        str((_get(dft, "extended_data", {}) or {}).get("DFT", "")).lower()
        in {"yes", "true", "1"}
        for dft in dfts
    )
    chemscreen_exp = any(
        str((_get(dft, "extended_data", {}) or {}).get("EXP", "")).lower()
        in {"yes", "true", "1"}
        for dft in dfts
    )

    recipes = _recipes_for_material(material_auid)
    visible_trials = []
    visible_literature = []
    for recipe in recipes:
        trials, literature, _ = visible_recipe_children(recipe, user_affiliations)
        visible_trials.extend(trials)
        visible_literature.extend(literature)

    structure = str(_get(material, "structure_family", "") or "unknown")
    route_prediction = predict_synthesis_route(elements, structure)
    if structure in ("", "unknown"):
        support = route_prediction.get("supporting_records") or []
        structure = str((support[0] if support else {}).get("structure_family") or "")
        if not structure or structure == "unknown":
            structure = _infer_structure(elements)
    temperature = route_prediction.get("temperature") or {}
    ranking_value = None
    ranking_source = ""
    if query.property_spec and chosen_dft is not None:
        ranking_value, ranking_source = dft_property_value(chosen_dft, query.property_spec)
        if neighbor_estimate and ranking_value is not None:
            ranking_source = f"composition neighbor · {ranking_source}"

    source_name = str(_get(chosen_dft, "dft_source", "") or "") if chosen_dft else ""
    return {
        "material_auid": material_auid,
        "composition_display": composition_override or _get(material, "display_name", "") or format_composition(elements),
        "elements": elements,
        "predicted_structure": structure.replace("_", " ").title(),
        "route_steps": route_prediction.get("route_steps") or [],
        "route_summary": _route_text(route_prediction.get("route_steps") or []),
        "transition_temperature": temperature,
        "predicted_tc": temperature.get("average"),
        "thermodynamic_data": _thermodynamic_values(chosen_dft, query.property_spec),
        "ranking_value": round(ranking_value, 6) if ranking_value is not None else None,
        "ranking_label": query.property_spec.label if query.property_spec else "",
        "ranking_unit": query.property_spec.unit if query.property_spec else "",
        "ranking_source": ranking_source,
        "dft_count": len(observed_dfts),
        "model_prediction_count": len(model_only_dfts),
        "dft_source": source_name,
        "dft_status": (
            f"Neighbor estimate ({source_name or ranking_source})"
            if neighbor_estimate and chosen_dft is not None
            else f"{len(observed_dfts)} record{'s' if len(observed_dfts) != 1 else ''}"
            if observed_dfts
            else "ChemScreen: reported"
            if chemscreen_dft
            else "No DFT"
        ),
        "exp_count": len(visible_trials),
        "exp_status": (
            f"{len(visible_trials)} trial{'s' if len(visible_trials) != 1 else ''}"
            if visible_trials
            else "ChemScreen: reported"
            if chemscreen_exp
            else "No EXP"
        ),
        "literature_count": len(visible_literature),
        "model": route_prediction.get("model", "composition model"),
        "model_evidence": route_prediction.get("candidate_count", 0),
        "top_match_score": route_prediction.get("top_match_score", 0),
    }


def _candidate_materials_for_formula(elements: Mapping[str, float], limit: int = 100) -> List[Any]:
    symbols = sorted(elements)
    fields = (
        "id", "elements", "element_symbols", "structure_family", "display_name",
        "dft_calculations",
    )
    exact = list(
        Material.objects(element_symbols__all=symbols).only(*fields).limit(limit)
    )
    candidates = exact
    if not candidates:
        # A genuinely novel combination can still borrow thermodynamic
        # evidence from partially overlapping compositions.
        candidates = list(
            Material.objects(element_symbols__in=symbols).only(*fields).limit(limit * 5)
        )
    scored: List[Tuple[float, Any]] = []
    target = _normalized_amounts(dict(elements))
    for material in candidates:
        score = _composition_similarity(target, _normalized_amounts(_get(material, "elements", {}) or {}))
        if score > 0:
            scored.append((score, material))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [material for _, material in scored]


def _property_mongo_paths(spec: PropertySpec) -> List[str]:
    paths: List[str] = []
    if spec.typed_field:
        paths.append(f"dft_calculations.{spec.typed_field}")
    for key in spec.payload_keys:
        paths.extend(
            (
                f"dft_calculations.extended_data.{key}",
                f"dft_calculations.ml_predictions.{key}",
                f"dft_calculations.ml_predictions.thermodynamics.{key}",
                f"dft_calculations.ml_predictions.properties.{key}",
            )
        )
    return paths


def _ranked_catalog_materials(query: PredictionQuery) -> List[Any]:
    """Rank across the full Mongo collection before hydrating table rows.

    The CHAOS/Bellatrix corpus can contain hundreds of thousands of
    computational records.  Sorting a Python prefix would make "lowest" mean
    "lowest in the first page"; the aggregation unwinds and sorts the entire
    collection, then keeps the best calculation per material.
    """
    spec = query.property_spec
    if spec is None:
        return []
    conversions = [
        {
            "$convert": {
                "input": f"${path}",
                "to": "double",
                "onError": None,
                "onNull": None,
            }
        }
        for path in _property_mongo_paths(spec)
    ]
    if not conversions:
        return []
    rank_expression: Any = conversions[-1]
    for conversion in reversed(conversions[:-1]):
        rank_expression = {"$ifNull": [conversion, rank_expression]}

    pipeline: List[Dict[str, Any]] = [{"$unwind": "$dft_calculations"}]
    if query.structure_family:
        pipeline.append({"$match": {"structure_family": query.structure_family}})
    pipeline.extend(
        (
            {"$set": {"_prediction_rank_value": rank_expression}},
            {"$match": {"_prediction_rank_value": {"$ne": None}}},
            {
                "$sort": {
                    "_prediction_rank_value": 1 if query.ascending else -1,
                    "_id": 1,
                }
            },
            {"$group": {"_id": "$_id", "material": {"$first": "$$ROOT"}}},
            {"$replaceRoot": {"newRoot": "$material"}},
            {"$limit": max(query.limit * 10, 100)},
            {"$set": {"dft_calculations": ["$dft_calculations"]}},
        )
    )
    try:
        return list(Material._get_collection().aggregate(pipeline))
    except Exception:
        # Development deployments and mocked unit tests may not provide every
        # aggregation operator.  The bounded fallback keeps the page usable;
        # production Mongo/Atlas uses the full-collection path above.
        return []


def _catalog_materials(query: PredictionQuery, scan_limit: int = 5000) -> List[Any]:
    if query.property_spec:
        ranked = _ranked_catalog_materials(query)
        if ranked:
            return ranked
    objects = Material.objects
    if query.structure_family:
        objects = objects(structure_family=query.structure_family)
    return list(
        objects.only(
            "id", "elements", "element_symbols", "structure_family", "display_name",
            "dft_calculations",
        ).limit(scan_limit if query.property_spec else query.limit)
    )


def screen_3d_transition_metal_oxides(
    *,
    user_affiliations: Optional[Iterable[str]] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    """Return a ranked table of every eligible HEO candidate.

    Eligibility is intentionally narrow: oxygen plus exactly five distinct 3d
    transition metals.  This excludes the non-transition-metal and four-cation
    ChemScreen records before ranking.  Fewer element/process concerns rank
    first; EFA (higher is better in the ChemScreen screening workflow) breaks
    ties.
    """
    affiliations = list(user_affiliations or ["S4E"])
    materials = [
        material
        for material in Material.objects(
            element_symbols__all=["O"],
            num_elements=6,
        ).only(
            "id", "elements", "element_symbols", "structure_family", "display_name",
            "dft_calculations",
        )
        if len(
            {
                symbol
                for symbol in (dict(_get(material, "elements", {}) or {}))
                if symbol != "O"
            }
        ) == 5
        and {
            symbol
            for symbol in (dict(_get(material, "elements", {}) or {}))
            if symbol != "O"
        }.issubset(_3D_TRANSITION_METALS)
    ]

    rows: List[Dict[str, Any]] = []
    has_verified_recipe_evidence = Recipe.objects.count() > 0
    from .model_training import predict_materials

    stored_model_predictions = predict_materials(materials)
    efa_spec = _PROPERTY_BY_KEY["efa"]
    deed_spec = _PROPERTY_BY_KEY["deed"]
    d2h_spec = _PROPERTY_BY_KEY["d2h"]
    for material in materials:
        elements = dict(_get(material, "elements", {}) or {})
        metals = sorted(symbol for symbol in elements if symbol != "O")
        if len(metals) != 5 or not set(metals).issubset(_3D_TRANSITION_METALS):
            continue

        dfts = _visible_dfts(material, affiliations)
        chosen = max(
            dfts,
            key=lambda dft: sum(
                dft_property_value(dft, spec)[0] is not None
                for spec in (efa_spec, deed_spec, d2h_spec)
            ),
            default=None,
        )
        efa, efa_source = dft_property_value(chosen, efa_spec) if chosen else (None, "")
        deed, deed_source = dft_property_value(chosen, deed_spec) if chosen else (None, "")
        d2h, d2h_source = dft_property_value(chosen, d2h_spec) if chosen else (None, "")
        has_observed_thermodynamic_value = any(
            value is not None for value in (efa, deed, d2h)
        )
        model_prediction: Dict[str, Any] = {}
        model_label = ""
        if efa is None or deed is None:
            model_prediction = stored_model_predictions.get(
                str(_get(material, "id", "")),
                {},
            )
            model_label = (
                f"{model_prediction.get('model_name', 'LOOP ChemScreen RF')} "
                f"{str(model_prediction.get('model_version', ''))[:18]}"
            ).strip()
            if efa is None and model_prediction.get("efa") is not None:
                efa = float(model_prediction["efa"])
                efa_source = model_label
            if deed is None and model_prediction.get("deed") is not None:
                deed = float(model_prediction["deed"])
                deed_source = model_label
        if d2h is None:
            derived_value = derived_d2h(efa, deed)
            if derived_value is not None:
                d2h = derived_value
                d2h_source = (
                    f"Derived from EFA/DEED · {model_label}"
                    if model_prediction
                    else "Derived from ChemScreen EFA/DEED"
                )

        concerns: List[str] = []
        concern_score = 0
        for symbol in metals:
            concern = _SCREENING_CONCERNS.get(symbol)
            if concern:
                concerns.append(concern[0])
                concern_score += concern[1]

        recipes = (
            _recipes_for_material(str(_get(material, "id", "")))
            if has_verified_recipe_evidence
            else []
        )
        trial_count = 0
        for recipe in recipes:
            trials, _, _ = visible_recipe_children(recipe, affiliations)
            trial_count += len(trials)
        reported_exp = any(
            str((_get(dft, "extended_data", {}) or {}).get("EXP", "")).lower()
            in {"yes", "true", "1"}
            for dft in dfts
        )
        reported_dft = any(
            str((_get(dft, "extended_data", {}) or {}).get("DFT", "")).lower()
            in {"yes", "true", "1"}
            for dft in dfts
        )
        experimental_outlook = predict_experimental_outlook(elements)
        if not trial_count and not reported_exp:
            concerns.append("No experimental synthesis validation in LOOP/ChemScreen")

        cation_amounts = [float(elements[symbol]) for symbol in metals]
        cation_average = sum(cation_amounts) / len(cation_amounts)
        equimolar_deviation = (
            max(abs(value - cation_average) for value in cation_amounts)
            / cation_average
            if cation_average
            else 1.0
        )
        if equimolar_deviation > 0.05:
            concerns.append("Cations are not near-equimolar")
            concern_score += 3

        structure_family = str(_get(material, "structure_family", "") or "unknown")
        # Evidence hierarchy: exact/neighbor LOOP recipes first, then a saved
        # material-specific framework prediction, then generate and persist a
        # new auditable pseudo-label.  Predictions are never inserted into the
        # Recipe collection and therefore cannot masquerade as experiments.
        route_prediction = (
            predict_synthesis_route(elements, structure_family)
            if has_verified_recipe_evidence
            else {}
        )
        if not route_prediction.get("route_steps"):
            route_prediction = stored_synthesis_prediction(str(_get(material, "id", ""))) or {}
        if not route_prediction.get("route_steps"):
            route_prediction = build_framework_prediction(
                elements,
                structure_family,
                d2h=d2h,
                experimental_probability=experimental_outlook.get("probability"),
                aflow_summary=cached_aflow_summary(elements),
            )
            store_synthesis_prediction(
                str(_get(material, "id", "")),
                elements,
                route_prediction,
            )
        elif not route_prediction.get("temperature"):
            temperature_fallback = build_framework_prediction(
                elements,
                structure_family,
                d2h=d2h,
                experimental_probability=experimental_outlook.get("probability"),
                aflow_summary=cached_aflow_summary(elements),
            )
            route_prediction = {
                **route_prediction,
                "temperature": temperature_fallback["temperature"],
                "source_detail": (
                    "LOOP recipe route; temperature filled by the composition-specific "
                    "Tammann/thermodynamic framework."
                ),
                "temperature_source_label": temperature_fallback["temperature_source_label"],
                "temperature_source_url": temperature_fallback["temperature_source_url"],
            }
        route_temperature = route_prediction.get("temperature") or {}
        if route_temperature:
            low = route_temperature.get("low")
            high = route_temperature.get("high")
            temperature_display = (
                f"{low}–{high} °C" if low != high else f"{low} °C"
            )
        else:
            temperature_display = "No temperature evidence"

        rows.append(
            {
                "material_auid": str(_get(material, "id", "")),
                "composition_display": _format_high_entropy_oxide(elements),
                "metals": metals,
                "structure": structure_family.replace("_", " ").title(),
                "efa": round(efa, 4) if efa is not None else None,
                "efa_source": efa_source or "Not calculated",
                "deed": round(deed, 4) if deed is not None else None,
                "deed_source": deed_source or "Not calculated",
                "d2h": round(d2h, 4) if d2h is not None else None,
                "d2h_source": d2h_source or "Not calculated",
                "prediction_model_version": str(
                    model_prediction.get("model_version") or ""
                ),
                "synthesis_route": _synthesis_route_text(route_prediction),
                "synthesis_temperature": temperature_display,
                "synthesis_route_source": route_prediction.get("source_label")
                or (
                    f"LOOP recipe k-NN · {route_prediction.get('candidate_count', 0)} neighbors"
                ),
                "synthesis_route_source_url": route_prediction.get("source_url") or "",
                "synthesis_route_detail": route_prediction.get("source_detail") or "",
                "synthesis_confidence": route_prediction.get("confidence"),
                "synthesis_methodology": route_prediction.get("methodology") or "",
                "synthesis_temperature_source": route_prediction.get(
                    "temperature_source_label"
                )
                or route_prediction.get("source_label")
                or "LOOP recipe evidence",
                "synthesis_temperature_source_url": route_prediction.get(
                    "temperature_source_url"
                )
                or route_prediction.get("source_url")
                or "",
                "dft_status": (
                    "ChemScreen DFT"
                    if has_observed_thermodynamic_value
                    and chosen
                    and "dft" in str(_get(chosen, "dft_source", "")).lower()
                    else "ChemScreen: reported"
                    if reported_dft
                    else "Model-estimated thermodynamics"
                    if model_prediction
                    else "No DFT or model estimate"
                ),
                "dft_detail": (
                    efa_source or "Observed EFA/DEED/d2h"
                    if has_observed_thermodynamic_value
                    else model_label
                    if model_prediction
                    else ""
                ),
                "exp_status": (
                    f"{trial_count} LOOP trial{'s' if trial_count != 1 else ''}"
                    if trial_count
                    else experimental_outlook["status"]
                ),
                "exp_detail": (
                    "Measured experimental evidence"
                    if trial_count
                    else experimental_outlook["detail"]
                ),
                "exp_source": experimental_outlook.get("source") or "",
                "exp_prediction_probability": experimental_outlook.get("probability"),
                "exp_prediction_observed": experimental_outlook.get("observed", False),
                "concerns": concerns,
                "concern_score": concern_score,
                "has_dft_evidence": reported_dft or has_observed_thermodynamic_value,
                "has_exp_evidence": reported_exp or trial_count > 0,
            }
        )

    rows.sort(
        key=lambda row: (
            row["concern_score"],
            not row["has_exp_evidence"],
            not row["has_dft_evidence"],
            row["efa"] is None,
            -(row["efa"] or 0.0),
            row["composition_display"],
        )
    )
    for index, row in enumerate(rows, start=1):
        row["rank"] = index

    displayed_rows = rows[:max(10, limit)]
    from .model_training import model_status as current_model_status

    return {
        "rows": displayed_rows,
        "eligible_count": len(rows),
        "displayed_count": len(displayed_rows),
        "model_status": current_model_status(),
        "criteria": (
            "Exactly five near-equimolar 3d transition metals plus oxygen; "
            "ranked by fewer known screening concerns, then experimental/DFT "
            "evidence, then higher calculated or current-model EFA."
        ),
    }


def run_prediction_query(
    prompt: str,
    *,
    user_affiliations: Optional[Iterable[str]] = None,
    default_limit: int = 10,
) -> Dict[str, Any]:
    """Execute one prompt and return table-ready rows plus an honest summary."""
    query = parse_prediction_query(prompt, default_limit=default_limit)
    if not query.raw:
        return {"rows": [], "error": "Enter a composition or a materials question.", "query": query}
    affiliations = list(user_affiliations or ["S4E"])

    if query.formula:
        try:
            elements = parse_composition_input(query.formula, "")
        except (TypeError, ValueError) as exc:
            return {"rows": [], "error": f"Unable to parse that composition: {exc}", "query": query}
        candidates = _candidate_materials_for_formula(elements)
        if candidates:
            best = candidates[0]
            # Preserve the formula exactly as the researcher entered it while
            # using the closest catalog/ChemScreen computational record.
            row = _material_row(
                best,
                query=query,
                user_affiliations=affiliations,
                composition_override=query.formula,
                neighbor_estimate=_normalized_amounts(elements)
                != _normalized_amounts(_get(best, "elements", {}) or {}),
            )
        else:
            synthetic = {
                "id": "",
                "elements": elements,
                "structure_family": query.structure_family or "unknown",
                "dft_calculations": [],
            }
            row = _material_row(
                synthetic,
                query=query,
                user_affiliations=affiliations,
                composition_override=query.formula,
            )
        return {
            "rows": [row],
            "error": None,
            "query": query,
            "summary": "Composition prediction using the closest LOOP computational and recipe evidence.",
        }

    materials = _catalog_materials(query)
    rows = [
        _material_row(material, query=query, user_affiliations=affiliations)
        for material in materials
    ]
    if query.property_spec:
        rows = [row for row in rows if row["ranking_value"] is not None]
        rows.sort(
            key=lambda row: row["ranking_value"],
            reverse=not query.ascending,
        )
    rows = rows[: query.limit]
    if not rows:
        target = query.property_spec.label if query.property_spec else "that request"
        return {
            "rows": [],
            "error": f"No visible LOOP computational/model data is available for {target}.",
            "query": query,
        }

    direction = "lowest" if query.ascending else "highest"
    summary = (
        f"Showing the {direction} {query.property_spec.label.lower()} values from "
        "LOOP DFT records and stored model predictions."
        if query.property_spec
        else "Showing materials from the combined LOOP data catalog."
    )
    return {"rows": rows, "error": None, "query": query, "summary": summary}


__all__ = [
    "PROPERTY_SPECS",
    "PredictionQuery",
    "PropertySpec",
    "chemscreen_route_prior",
    "derived_d2h",
    "dft_property_value",
    "format_composition",
    "parse_prediction_query",
    "predict_experimental_outlook",
    "run_prediction_query",
    "screen_3d_transition_metal_oxides",
]
