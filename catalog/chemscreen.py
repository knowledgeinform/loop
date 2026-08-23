"""ChemScreen artifact reader for LOOP synchronization.

ChemScreen is a batch prediction repository rather than a network service.  Its
stable interchange shapes are:

* observed JSON records: ``species`` + ``Composition`` + EFA/DEED/d2h;
* generated pool JSON: ``species`` + ``composition`` + DFT/EXP flags;
* experimental truth JSON: ``species`` + ``Composition`` + ``single_phase``;
* model output CSV: ``ML_Predicted`` + ``chem_id`` + ``Elements``.

This module normalizes those shapes without importing ChemScreen's training
stack.  The management command stores them in LOOP; the prediction table then
queries them through the same computational-record path as every other model.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Sequence


class ChemScreenImportError(ValueError):
    """Raised when a ChemScreen artifact does not match its data contract."""


@dataclass(frozen=True)
class ChemScreenRecord:
    chem_id: str
    elements: Dict[str, float]
    values: Dict[str, float]
    dft: str
    exp: str
    kind: str
    model_name: str = ""
    single_phase: str = ""
    experimental_source: str = ""
    calculation_method: str = ""
    calculation_inputs: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class ChemScreenExperimentalOutcome:
    """One measured single-/multi-phase outcome from ChemScreen."""

    chem_id: str
    elements: Dict[str, float]
    single_phase: bool
    source: str = ""


_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_CHEM_ID_RE = re.compile(r"^([A-Za-z]+)_([0-9]+)$")
_SYMBOL_RE = re.compile(r"[A-Z][a-z]?")
_OBSERVED_FILENAMES = ("lib5_organized_072125.json", "lib6_organized_072125.json")
_EXPERIMENTAL_FILENAMES = (
    "exp_truth/lib5_exp_truth.json",
    "exp_truth/lib6_exp_truth.json",
)
_CANDIDATE_FILENAME = "comp_pool/generated_compositions.json"


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUMBER_RE.search(str(value).strip())
    return float(match.group(0)) if match else None


def calculate_efa_deed(
    dg_list: Sequence[Any],
    formation_enthalpies: Sequence[Any],
    d2h: Any,
) -> Dict[str, float]:
    """Calculate ChemScreen EFA and DEED from their native equations.

    ``dg_list`` supplies POCC degeneracy weights, ``formation_enthalpies``
    supplies the corresponding DFT formation enthalpies, and ``d2h`` is the
    AFLOW convex-hull distance in eV/atom.
    """
    if not dg_list or len(dg_list) != len(formation_enthalpies):
        raise ChemScreenImportError(
            "ChemScreen EFA calculation requires equally sized dg and enthalpy lists."
        )
    weights = [_number(value) for value in dg_list]
    enthalpies = [_number(value) for value in formation_enthalpies]
    if any(value is None for value in weights + enthalpies):
        raise ChemScreenImportError(
            "ChemScreen EFA calculation inputs must all be numeric."
        )
    numeric_weights = [float(value) for value in weights if value is not None]
    numeric_enthalpies = [float(value) for value in enthalpies if value is not None]
    if any(weight <= 0 for weight in numeric_weights):
        raise ChemScreenImportError("ChemScreen POCC degeneracy weights must be positive.")
    weight_total = sum(numeric_weights)
    if weight_total <= 1:
        raise ChemScreenImportError(
            "ChemScreen EFA calculation requires total POCC degeneracy above one."
        )
    mean_enthalpy = sum(
        weight * enthalpy
        for weight, enthalpy in zip(numeric_weights, numeric_enthalpies)
    ) / weight_total
    variance = sum(
        weight * (enthalpy - mean_enthalpy) ** 2
        for weight, enthalpy in zip(numeric_weights, numeric_enthalpies)
    ) / (weight_total - 1)
    sigma = math.sqrt(variance)
    if sigma <= 0:
        raise ChemScreenImportError(
            "ChemScreen EFA is undefined when the enthalpy spread is zero."
        )
    hull_distance = _number(d2h)
    if hull_distance is None or hull_distance <= 0:
        raise ChemScreenImportError(
            "ChemScreen DEED calculation requires a positive AFLOW d2h value."
        )
    efa = 1.0 / sigma
    deed = math.sqrt(efa / hull_distance)
    return {
        "EFA": efa,
        "DEED": deed,
        "d2h": hull_distance,
        "mean_formation_enthalpy": mean_enthalpy,
        "sigma": sigma,
    }


def _element_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item for item in re.split(r"[\s,;/-]+", value.strip()) if item]
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _composition_from_lists(symbols: Sequence[Any], amounts: Sequence[Any]) -> Dict[str, float]:
    if not symbols or not amounts or len(symbols) != len(amounts):
        raise ChemScreenImportError(
            "ChemScreen species and composition arrays must be non-empty and the same length."
        )
    elements: Dict[str, float] = {}
    for raw_symbol, raw_amount in zip(symbols, amounts):
        symbol = str(raw_symbol).strip()
        amount = _number(raw_amount)
        if not symbol or amount is None or amount <= 0:
            raise ChemScreenImportError(
                f"Invalid ChemScreen composition item: {raw_symbol!r}={raw_amount!r}"
            )
        elements[symbol] = elements.get(symbol, 0.0) + amount
    return elements


def parse_observed_entry(entry: Mapping[str, Any]) -> ChemScreenRecord:
    symbols = entry.get("species") or entry.get("Elements")
    amounts = (
        entry.get("Composition")
        or entry.get("composition")
        or entry.get("Compositions")
    )
    elements = _composition_from_lists(symbols or [], amounts or [])
    values: Dict[str, float] = {}
    for key in ("EFA", "DEED", "d2h", "EFA_cce", "deed_cce"):
        value = _number(entry.get(key))
        if value is not None:
            values[key] = value
    if not values:
        raise ChemScreenImportError("Observed ChemScreen record has no EFA, DEED, or d2h value.")
    calculation_method = ""
    calculation_inputs: Optional[Dict[str, Any]] = None
    dg_list = entry.get("dg_list")
    eform_list = entry.get("eform_list")
    if isinstance(dg_list, Sequence) and isinstance(eform_list, Sequence) and values.get("d2h"):
        reported = {key: values.get(key) for key in ("EFA", "DEED", "d2h")}
        calculated = calculate_efa_deed(dg_list, eform_list, values["d2h"])
        values.update({key: calculated[key] for key in ("EFA", "DEED", "d2h")})
        calculation_method = "LOOP direct calculation"
        calculation_inputs = {
            "dg_list": list(dg_list),
            "eform_list": list(eform_list),
            "aflow_d2h": calculated["d2h"],
            "mean_formation_enthalpy": calculated["mean_formation_enthalpy"],
            "sigma": calculated["sigma"],
            "reported_values": reported,
        }
    return ChemScreenRecord(
        chem_id=str(entry.get("chem_id") or "").strip(),
        elements=elements,
        values=values,
        dft=str(entry.get("DFT") or "yes").strip().lower(),
        exp=str(entry.get("EXP") or "no").strip().lower(),
        kind="observed",
        calculation_method=calculation_method,
        calculation_inputs=calculation_inputs,
    )


def _split_chem_id(chem_id: str, explicit_elements: Iterable[str]) -> Dict[str, float]:
    match = _CHEM_ID_RE.match((chem_id or "").strip())
    if not match:
        raise ChemScreenImportError(f"Invalid ChemScreen chem_id: {chem_id!r}")
    formula_part, amount_part = match.groups()
    symbols = _SYMBOL_RE.findall(formula_part)
    if "".join(symbols) != formula_part:
        raise ChemScreenImportError(f"Unable to parse elements from ChemScreen chem_id: {chem_id!r}")

    # ChemScreen encodes fractions as integer thousandths.  Current pools use
    # three digits per element (100/500); chunk from the right so the oxygen
    # position and sorted chem_id order are retained.
    if len(amount_part) != 3 * len(symbols):
        explicit = _element_list(explicit_elements)
        cations = [symbol for symbol in explicit if symbol != "O"]
        if cations and set(cations).issubset(symbols) and "O" in symbols:
            fraction = 0.5 / len(cations)
            return {**{symbol: fraction for symbol in cations}, "O": 0.5}
        raise ChemScreenImportError(f"Invalid fraction encoding in ChemScreen chem_id: {chem_id!r}")
    amounts = [int(amount_part[index:index + 3]) / 1000 for index in range(0, len(amount_part), 3)]
    return _composition_from_lists(symbols, amounts)


def parse_prediction_entry(
    entry: Mapping[str, Any],
    *,
    metric_name: str = "ML_Predicted",
    model_name: str = "LOOP screening",
) -> ChemScreenRecord:
    chem_id = str(entry.get("chem_id") or "").strip()
    symbols = entry.get("species")
    amounts = (
        entry.get("Composition")
        or entry.get("composition")
        or entry.get("Compositions")
    )
    if symbols and amounts:
        elements = _composition_from_lists(symbols, amounts)
    else:
        elements = _split_chem_id(chem_id, _element_list(entry.get("Elements")))
    raw_value = entry.get(metric_name)
    if raw_value is None and metric_name != "ML_Predicted":
        raw_value = entry.get("ML_Predicted")
    value = _number(raw_value)
    if value is None:
        raise ChemScreenImportError(
            f"ChemScreen prediction {chem_id or '<unknown>'} has no numeric {metric_name}."
        )
    return ChemScreenRecord(
        chem_id=chem_id,
        elements=elements,
        values={metric_name: value},
        dft=str(entry.get("DFT") or "no").strip().lower(),
        exp=str(entry.get("EXP") or "no").strip().lower(),
        kind="prediction",
        model_name=model_name,
    )


def parse_candidate_entry(entry: Mapping[str, Any]) -> ChemScreenRecord:
    """Normalize one uncalculated composition from ChemScreen's pool."""
    chem_id = str(entry.get("chem_id") or "").strip()
    symbols = entry.get("species")
    amounts = (
        entry.get("Composition")
        or entry.get("composition")
        or entry.get("Compositions")
    )
    if symbols and amounts:
        elements = _composition_from_lists(symbols, amounts)
    else:
        elements = _split_chem_id(chem_id, _element_list(entry.get("Elements")))
    return ChemScreenRecord(
        chem_id=chem_id,
        elements=elements,
        values={},
        dft=str(entry.get("DFT") or "no").strip().lower(),
        exp=str(entry.get("EXP") or "no").strip().lower(),
        kind="candidate",
        single_phase=str(entry.get("single_phase") or "").strip().lower(),
        experimental_source=str(entry.get("source") or "").strip(),
    )


def parse_experimental_outcome(
    entry: Mapping[str, Any],
) -> ChemScreenExperimentalOutcome:
    """Normalize one ChemScreen experimental single-phase truth record."""
    symbols = entry.get("species") or entry.get("Elements")
    amounts = (
        entry.get("Composition")
        or entry.get("composition")
        or entry.get("Compositions")
    )
    elements = _composition_from_lists(symbols or [], amounts or [])
    raw_phase = str(entry.get("single_phase") or "").strip().lower()
    if raw_phase not in {"yes", "no"}:
        raise ChemScreenImportError(
            "ChemScreen experimental record must label single_phase as yes or no."
        )
    return ChemScreenExperimentalOutcome(
        chem_id=str(entry.get("chem_id") or "").strip(),
        elements=elements,
        single_phase=raw_phase == "yes",
        source=str(entry.get("source") or "").strip(),
    )


def _load_json_records(path: Path) -> list[Mapping[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ChemScreenImportError(f"Unable to read ChemScreen JSON {path}: {exc}") from exc
    if isinstance(data, Mapping):
        data = data.get("records") or data.get("predictions") or [data]
    if not isinstance(data, list):
        raise ChemScreenImportError(f"ChemScreen JSON must contain a list of records: {path}")
    return [item for item in data if isinstance(item, Mapping)]


def iter_observed_records(root: str | Path) -> Iterator[ChemScreenRecord]:
    data_dir = Path(root).expanduser().resolve() / "Data"
    found = False
    for filename in _OBSERVED_FILENAMES:
        path = data_dir / filename
        if not path.exists():
            continue
        found = True
        for entry in _load_json_records(path):
            yield parse_observed_entry(entry)
    if not found:
        raise ChemScreenImportError(
            f"No ChemScreen observed datasets found under {data_dir}; expected "
            + ", ".join(_OBSERVED_FILENAMES)
        )


def iter_experimental_outcomes(
    root: str | Path,
) -> Iterator[ChemScreenExperimentalOutcome]:
    """Yield ChemScreen's measured single-/multi-phase experimental labels."""
    data_dir = Path(root).expanduser().resolve() / "Data"
    found = False
    for filename in _EXPERIMENTAL_FILENAMES:
        path = data_dir / filename
        if not path.exists():
            continue
        found = True
        for entry in _load_json_records(path):
            yield parse_experimental_outcome(entry)
    if not found:
        raise ChemScreenImportError(
            f"No ChemScreen experimental datasets found under {data_dir}; expected "
            + ", ".join(_EXPERIMENTAL_FILENAMES)
        )


def iter_candidate_records(
    root: str | Path,
    *,
    allowed_elements: Optional[Iterable[str]] = None,
    cation_count: Optional[int] = None,
) -> Iterator[ChemScreenRecord]:
    """Yield generated-pool candidates, optionally restricted by chemistry."""
    path = Path(root).expanduser().resolve() / "Data" / _CANDIDATE_FILENAME
    if not path.exists():
        raise ChemScreenImportError(
            f"No ChemScreen candidate pool found at {path}."
        )
    allowed = set(allowed_elements or [])
    for entry in _load_json_records(path):
        record = parse_candidate_entry(entry)
        cations = {symbol for symbol in record.elements if symbol != "O"}
        if cation_count is not None and len(cations) != cation_count:
            continue
        if allowed and not cations.issubset(allowed):
            continue
        yield record


def iter_prediction_records(
    path: str | Path,
    *,
    metric_name: str = "ML_Predicted",
    model_name: str = "LOOP screening",
) -> Iterator[ChemScreenRecord]:
    artifact = Path(path).expanduser().resolve()
    if not artifact.exists():
        raise ChemScreenImportError(f"ChemScreen prediction artifact does not exist: {artifact}")
    if artifact.suffix.lower() in (".json", ".jsonl"):
        if artifact.suffix.lower() == ".jsonl":
            entries = [
                json.loads(line)
                for line in artifact.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        else:
            entries = _load_json_records(artifact)
    else:
        with artifact.open("r", encoding="utf-8-sig", newline="") as handle:
            entries = list(csv.DictReader(handle))
    for entry in entries:
        if isinstance(entry, Mapping):
            yield parse_prediction_entry(
                entry,
                metric_name=metric_name,
                model_name=model_name,
            )


__all__ = [
    "ChemScreenExperimentalOutcome",
    "ChemScreenImportError",
    "ChemScreenRecord",
    "calculate_efa_deed",
    "iter_candidate_records",
    "iter_experimental_outcomes",
    "iter_observed_records",
    "iter_prediction_records",
    "parse_candidate_entry",
    "parse_experimental_outcome",
    "parse_observed_entry",
    "parse_prediction_entry",
]
