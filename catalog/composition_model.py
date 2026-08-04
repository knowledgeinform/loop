"""In-memory composition k-nearest-neighbours model for synthesis prediction.

The model is rebuilt from every saved :class:`Recipe` whenever recipes change.
It uses normalized element amounts as features, so ``Al0.2Mg0.2...`` is
distinguished from a material with the same elements in different proportions.
Keeping the fitted snapshot in memory makes CSV inference fast while avoiding a
second source of truth beside MongoDB.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import threading
from typing import Any, Dict, Iterable, List, Tuple

from .documents import Recipe, SynthesisPrediction


def _normalized_amounts(elements: Dict[str, Any]) -> Dict[str, float]:
    values: Dict[str, float] = {}
    for symbol, raw_value in (elements or {}).items():
        try:
            amount = float(raw_value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            values[str(symbol)] = amount
    total = sum(values.values())
    return {symbol: amount / total for symbol, amount in values.items()} if total else {}


def _composition_similarity(left: Dict[str, float], right: Dict[str, float]) -> float:
    """Return a 0–1 similarity for normalized, sparse composition vectors."""
    if not left or not right:
        return 0.0
    symbols = set(left) | set(right)
    distance = math.sqrt(sum((left.get(symbol, 0.0) - right.get(symbol, 0.0)) ** 2 for symbol in symbols))
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    cosine = sum(left.get(symbol, 0.0) * right.get(symbol, 0.0) for symbol in symbols) / (left_norm * right_norm)
    # A cosine match captures elemental direction; Euclidean distance makes
    # different fractions of the same elements less similar.
    return max(0.0, min(1.0, 0.65 * cosine + 0.35 * (1.0 - min(distance / math.sqrt(2), 1.0))))


@dataclass(frozen=True)
class TrainingRecord:
    recipe: Any
    amounts: Dict[str, float]
    sample_weight: float = 1.0
    source_type: str = "verified_recipe"


class CompositionKNNModel:
    """A fitted, read-only KNN snapshot built from the recipe collection."""

    def __init__(self, records: Iterable[TrainingRecord]):
        self.records = tuple(records)

    @property
    def training_record_count(self) -> int:
        return len(self.records)

    def nearest(
        self,
        elements: Dict[str, Any],
        structure_family: str | None = None,
        limit: int = 12,
    ) -> List[Tuple[Any, float]]:
        target = _normalized_amounts(elements)
        if not target:
            return []
        target_family = (structure_family or "").strip().lower()
        scored: List[Tuple[Recipe, float]] = []
        for record in self.records:
            score = _composition_similarity(target, record.amounts)
            if target_family and target_family != "unknown" and record.recipe.structure_family == target_family:
                score = min(1.0, score + 0.03)
            score *= max(0.0, min(float(record.sample_weight), 1.0))
            if score > 0.0:
                scored.append((record.recipe, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]


_model_lock = threading.RLock()
_model: CompositionKNNModel | None = None


def _build_model() -> CompositionKNNModel:
    records: List[TrainingRecord] = []
    for recipe in Recipe.objects.only(
        "id", "material_auid", "elements", "element_symbols", "structure_family",
        "synthesis_steps", "trials", "literature",
    ):
        amounts = _normalized_amounts(recipe.elements or {})
        if amounts:
            records.append(TrainingRecord(recipe=recipe, amounts=amounts))
    # Generated routes are explicitly low-weight pseudo-labels.  They let the
    # route model cover new compositions while ensuring one experimental or
    # literature recipe outranks even an exact generated composition match.
    for prediction in SynthesisPrediction.objects(
        training_eligible=True,
        prediction_status__ne="rejected",
    ).only(
        "id", "material_auid", "elements", "element_symbols", "structure_family",
        "route_steps", "temperature", "prediction_status", "training_weight",
        "model_version", "confidence",
    ):
        amounts = _normalized_amounts(prediction.elements or {})
        if amounts:
            records.append(
                TrainingRecord(
                    recipe=prediction,
                    amounts=amounts,
                    sample_weight=float(prediction.training_weight or 0.2),
                    source_type="synthesis_prediction",
                )
            )
    return CompositionKNNModel(records)


def get_composition_model() -> CompositionKNNModel:
    """Return the fitted model, building it once from all saved recipes."""
    global _model
    with _model_lock:
        if _model is None:
            _model = _build_model()
        return _model


def warm_composition_model() -> int:
    """Build the model for a background worker and return its training size."""
    return get_composition_model().training_record_count


def invalidate_composition_model() -> None:
    """Mark the cached model stale after a recipe write or deletion."""
    global _model
    with _model_lock:
        _model = None


__all__ = [
    "CompositionKNNModel",
    "get_composition_model",
    "invalidate_composition_model",
    "warm_composition_model",
]
