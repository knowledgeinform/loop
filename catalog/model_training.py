"""Versioned EFA/DEED retraining with reward/flag feedback.

Every write is reduced to a lightweight queue request.  The worker waits for
the upload burst to settle, evaluates the previous active model against any new
ground truth, and then trains a fresh Random Forest from the full labeled
corpus.  Candidate artifacts are immutable; only candidates whose validation
score is at least as good as the active model are promoted.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import pickle
import subprocess
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from django.conf import settings

from .documents import (
    Material,
    ModelFeedback,
    ModelRetrainJob,
    ModelVersion,
    _utc_now,
)

logger = logging.getLogger(__name__)

TARGETS = ("efa", "deed")
_3D_METALS = ("Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn")
_ATOMIC_NUMBERS = {
    "Sc": 21,
    "Ti": 22,
    "V": 23,
    "Cr": 24,
    "Mn": 25,
    "Fe": 26,
    "Co": 27,
    "Ni": 28,
    "Cu": 29,
    "Zn": 30,
}
FEATURE_NAMES = (
    *(f"fraction_{symbol}" for symbol in _3D_METALS),
    "oxygen_to_cation_ratio",
    "cation_count",
    "atomic_number_mean",
    "atomic_number_std",
    "atomic_number_range",
    "formation_mean",
    "formation_std",
    "formation_min",
    "formation_max",
    "formation_count",
    "electronic_entropy_mean",
    "electronic_entropy_std",
    "aflow_match_count",
)

_active_artifact_cache: Tuple[str, Dict[str, Any]] | None = None


def _safe_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalized_key(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def _mapping_number(payload: Mapping[str, Any], names: Iterable[str]) -> Optional[float]:
    wanted = {_normalized_key(name) for name in names}
    for key, value in (payload or {}).items():
        if _normalized_key(key) in wanted:
            number = _safe_number(value)
            if number is not None:
                return number
    return None


def _truth_for_material(material: Any) -> Tuple[Dict[str, float], str]:
    """Return observed EFA/DEED labels, never recycling model predictions."""
    collected: Dict[str, List[float]] = {target: [] for target in TARGETS}
    comp_auid = ""
    for dft in getattr(material, "dft_calculations", None) or []:
        source = str(getattr(dft, "dft_source", "") or "").lower()
        if "model" in source or "candidate pool" in source:
            continue
        payload = dict(getattr(dft, "extended_data", {}) or {})
        efa = _mapping_number(payload, ("EFA", "entropy_forming_ability"))
        deed = _mapping_number(payload, ("DEED",))
        if efa is not None:
            collected["efa"].append(efa)
            comp_auid = comp_auid or str(getattr(dft, "comp_auid", "") or "")
        if deed is not None:
            collected["deed"].append(deed)
            comp_auid = comp_auid or str(getattr(dft, "comp_auid", "") or "")
    return (
        {
            target: sum(values) / len(values)
            for target, values in collected.items()
            if values
        },
        comp_auid,
    )


def material_has_training_labels(material: Any) -> bool:
    labels, _ = _truth_for_material(material)
    return bool(labels)


def _stats(values: Sequence[float]) -> Tuple[float, float, float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return mean, math.sqrt(variance), min(values), max(values), float(len(values))


def material_features(material: Any) -> List[float]:
    """Stable composition + AFLOW-derived feature vector."""
    elements = dict(getattr(material, "elements", {}) or {})
    cations = {
        symbol: (_safe_number(value) or 0.0)
        for symbol, value in elements.items()
        if symbol != "O" and (_safe_number(value) or 0.0) > 0
    }
    cation_total = sum(cations.values())
    oxygen = _safe_number(elements.get("O")) or 0.0
    fractions = [
        cations.get(symbol, 0.0) / cation_total if cation_total else 0.0
        for symbol in _3D_METALS
    ]

    z_values = [
        (_ATOMIC_NUMBERS[symbol], amount / cation_total)
        for symbol, amount in cations.items()
        if symbol in _ATOMIC_NUMBERS and cation_total
    ]
    z_mean = sum(number * weight for number, weight in z_values) if z_values else 0.0
    z_variance = (
        sum(weight * (number - z_mean) ** 2 for number, weight in z_values)
        if z_values
        else 0.0
    )
    z_numbers = [number for number, _ in z_values]
    z_range = max(z_numbers) - min(z_numbers) if z_numbers else 0.0

    formation: List[float] = []
    electronic_entropy: List[float] = []
    for dft in getattr(material, "dft_calculations", None) or []:
        typed_formation = _safe_number(getattr(dft, "dft_formation_energy_ev", None))
        payload = dict(getattr(dft, "extended_data", {}) or {})
        extended_formation = _mapping_number(
            payload,
            ("enthalpy_formation_atom", "formation_energy", "formation_enthalpy"),
        )
        if typed_formation is not None:
            formation.append(typed_formation)
        elif extended_formation is not None:
            formation.append(extended_formation)
        entropy = _mapping_number(payload, ("eentropy_atom", "electronic_entropy_atom"))
        if entropy is not None:
            electronic_entropy.append(entropy)

    from .aflow_client import cached_aflow_summary

    aflow = cached_aflow_summary(elements)
    if not formation and _safe_number(aflow.get("formation_mean")) is not None:
        formation.append(float(aflow["formation_mean"]))
    if (
        not electronic_entropy
        and _safe_number(aflow.get("electronic_entropy_mean")) is not None
    ):
        electronic_entropy.append(float(aflow["electronic_entropy_mean"]))

    formation_stats = _stats(formation)
    entropy_stats = _stats(electronic_entropy)
    return [
        *fractions,
        oxygen / cation_total if cation_total else 0.0,
        float(len(cations)),
        z_mean,
        math.sqrt(z_variance),
        float(z_range),
        *formation_stats,
        entropy_stats[0],
        entropy_stats[1],
        _safe_number(aflow.get("match_count")) or 0.0,
    ]


def _latest_feedback_weights(material_ids: Sequence[str]) -> Dict[str, float]:
    weights: Dict[str, float] = {}
    if not material_ids:
        return weights
    for feedback in ModelFeedback.objects(
        material_auid__in=list(material_ids),
        outcome__in=["reward", "flag"],
    ).order_by("-created_at"):
        if feedback.material_auid not in weights:
            weights[feedback.material_auid] = float(feedback.sample_weight or 1.0)
    return weights


def collect_training_data() -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    materials = Material.objects(dft_calculations__exists=True).only(
        "id", "elements", "dft_calculations"
    )
    for material in materials:
        labels, comp_auid = _truth_for_material(material)
        if not labels:
            continue
        rows.append(
            {
                "material_auid": str(material.id),
                "comp_auid": comp_auid,
                "features": material_features(material),
                "labels": labels,
            }
        )

    rows.sort(key=lambda row: row["material_auid"])
    weights = _latest_feedback_weights([row["material_auid"] for row in rows])
    for row in rows:
        row["sample_weight"] = weights.get(row["material_auid"], 1.0)
    digest_payload = [
        {
            "material_auid": row["material_auid"],
            "labels": row["labels"],
            "features": row["features"],
            "sample_weight": row["sample_weight"],
        }
        for row in rows
    ]
    source_hash = hashlib.sha256(
        json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"rows": rows, "source_data_hash": source_hash}


def _artifact_root() -> Path:
    root = Path(
        getattr(
            settings,
            "CHEMSCREEN_MODEL_DIR",
            Path(settings.BASE_DIR) / "var" / "chemscreen-models",
        )
    )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _chemscreen_commit() -> str:
    root = str(getattr(settings, "CHEMSCREEN_ROOT", "") or "")
    if not root or not Path(root).exists():
        return ""
    try:
        return subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip()
    except Exception:
        return ""


def _train_target(rows: Sequence[Mapping[str, Any]], target: str) -> Tuple[Any, Dict[str, Any]]:
    import numpy as np
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import KFold, cross_val_predict

    selected = [row for row in rows if target in row["labels"]]
    minimum = int(getattr(settings, "CHEMSCREEN_MIN_TRAINING_ROWS", 20))
    if len(selected) < minimum:
        raise ValueError(
            f"{target.upper()} needs at least {minimum} labeled rows; found {len(selected)}."
        )
    x = np.asarray([row["features"] for row in selected], dtype=float)
    y = np.asarray([row["labels"][target] for row in selected], dtype=float)
    weights = np.asarray([row["sample_weight"] for row in selected], dtype=float)
    params = {
        "n_estimators": int(getattr(settings, "CHEMSCREEN_RF_ESTIMATORS", 100)),
        "max_depth": int(getattr(settings, "CHEMSCREEN_RF_MAX_DEPTH", 10)),
        "min_samples_leaf": int(
            getattr(settings, "CHEMSCREEN_RF_MIN_SAMPLES_LEAF", 2)
        ),
        "random_state": 21,
        "n_jobs": int(getattr(settings, "CHEMSCREEN_RF_N_JOBS", -1)),
    }
    estimator = RandomForestRegressor(**params)
    folds = min(5, len(selected))
    splitter = KFold(n_splits=folds, shuffle=True, random_state=21)
    predicted = cross_val_predict(
        estimator,
        x,
        y,
        cv=splitter,
        params={"sample_weight": weights},
        n_jobs=1,
    )
    std = float(np.std(y))
    metrics = {
        "count": len(selected),
        "mae": float(mean_absolute_error(y, predicted)),
        "rmse": float(math.sqrt(mean_squared_error(y, predicted))),
        "r2": float(r2_score(y, predicted)) if std > 0 else 0.0,
        "target_std": std,
    }
    metrics["normalized_mae"] = metrics["mae"] / std if std > 0 else metrics["mae"]
    estimator.fit(x, y, sample_weight=weights)
    return estimator, metrics


def _write_artifact(version: str, artifact: Dict[str, Any]) -> Path:
    destination = _artifact_root() / f"{version}.pkl"
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=destination.parent,
        prefix=f".{version}-",
        suffix=".tmp",
        delete=False,
    ) as handle:
        pickle.dump(artifact, handle)
        temporary = Path(handle.name)
    os.replace(temporary, destination)
    return destination


def _promotion_score(metrics: Mapping[str, Any]) -> float:
    values = [
        float(payload.get("normalized_mae"))
        for target, payload in metrics.items()
        if target in TARGETS and payload.get("normalized_mae") is not None
    ]
    return sum(values) / len(values) if values else float("inf")


def train_and_maybe_promote(*, reason: str = "") -> Dict[str, Any]:
    global _active_artifact_cache
    dataset = collect_training_data()
    rows = dataset["rows"]
    source_hash = dataset["source_data_hash"]
    latest_same = ModelVersion.objects(source_data_hash=source_hash).first()
    if latest_same is not None:
        return {
            "status": "skipped",
            "reason": "No labeled EFA/DEED training data changed.",
            "model_version": str(latest_same.id),
        }

    models: Dict[str, Any] = {}
    metrics: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for target in TARGETS:
        try:
            model, target_metrics = _train_target(rows, target)
            models[target] = model
            metrics[target] = target_metrics
        except ValueError as exc:
            errors[target] = str(exc)
    if set(models) != set(TARGETS):
        return {
            "status": "skipped",
            "reason": "Both EFA and DEED require enough labeled data.",
            "errors": errors,
        }

    now = _utc_now()
    version = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{source_hash[:8]}"
    artifact = {
        "version": version,
        "model_name": "LOOP ChemScreen RF",
        "feature_names": list(FEATURE_NAMES),
        "models": models,
        "metrics": metrics,
        "source_data_hash": source_hash,
        "chemscreen_commit": _chemscreen_commit(),
    }
    artifact_path = _write_artifact(version, artifact)

    active = ModelVersion.objects(active=True).first()
    candidate_score = _promotion_score(metrics)
    active_score = _promotion_score(active.metrics or {}) if active else float("inf")
    allowed_regression = float(
        getattr(settings, "CHEMSCREEN_PROMOTION_MAE_TOLERANCE", 0.0)
    )
    promoted = active is None or candidate_score <= active_score + allowed_regression
    promotion_reason = (
        "First complete EFA/DEED model."
        if active is None
        else (
            f"Validation normalized MAE improved or held: "
            f"{candidate_score:.6f} <= {active_score + allowed_regression:.6f}."
            if promoted
            else (
                f"Rejected: validation normalized MAE {candidate_score:.6f} "
                f"exceeded active {active_score:.6f}."
            )
        )
    )
    version_doc = ModelVersion(
        id=version,
        artifact_path=str(artifact_path),
        targets=list(TARGETS),
        feature_names=list(FEATURE_NAMES),
        training_counts={
            target: int(metrics[target]["count"])
            for target in TARGETS
        },
        metrics=metrics,
        source_data_hash=source_hash,
        chemscreen_commit=artifact["chemscreen_commit"],
        aflow_enabled=bool(getattr(settings, "AFLOW_API_ENABLED", True)),
        active=promoted,
        promoted=promoted,
        promotion_reason=promotion_reason,
        created_at=now,
    )
    if promoted:
        ModelVersion.objects(active=True).update(set__active=False)
    version_doc.save()
    if promoted:
        _active_artifact_cache = (version, artifact)
    return {
        "status": "done",
        "model_version": version,
        "promoted": promoted,
        "metrics": metrics,
        "promotion_reason": promotion_reason,
        "reason": reason,
    }


def _load_active_artifact() -> Tuple[Optional[ModelVersion], Optional[Dict[str, Any]]]:
    global _active_artifact_cache
    version = ModelVersion.objects(active=True).first()
    if version is None:
        return None, None
    if _active_artifact_cache and _active_artifact_cache[0] == str(version.id):
        return version, _active_artifact_cache[1]
    try:
        with Path(version.artifact_path).open("rb") as handle:
            artifact = pickle.load(handle)
    except Exception as exc:
        logger.warning("Unable to load model artifact %s: %s", version.artifact_path, exc)
        return version, None
    _active_artifact_cache = (str(version.id), artifact)
    return version, artifact


def predict_material(material: Any) -> Dict[str, Any]:
    material_id = str(getattr(material, "id", "") or "")
    return predict_materials([material]).get(material_id, {})


def predict_materials(materials: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """Predict both targets for a material batch with one forest traversal."""
    version, artifact = _load_active_artifact()
    if version is None or artifact is None:
        return {}
    import numpy as np

    material_list = list(materials)
    if not material_list:
        return {}
    features = np.asarray(
        [material_features(material) for material in material_list],
        dtype=float,
    )
    target_predictions = {
        target: model.predict(features)
        for target, model in (artifact.get("models") or {}).items()
        if target in TARGETS
    }
    return {
        str(getattr(material, "id", "") or ""): {
            **{
                target: float(values[index])
                for target, values in target_predictions.items()
            },
            "model_version": str(version.id),
            "model_name": version.model_name,
        }
        for index, material in enumerate(material_list)
    }


def _feedback_id(
    material_auid: str,
    comp_auid: str,
    model_version: str,
    labels: Mapping[str, float],
) -> str:
    payload = json.dumps(
        {
            "material": material_auid,
            "comp": comp_auid,
            "version": model_version,
            "labels": labels,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_feedback(material: Any) -> Optional[ModelFeedback]:
    labels, comp_auid = _truth_for_material(material)
    if not labels:
        return None
    prediction = predict_material(material)
    version = str(prediction.get("model_version") or "")
    feedback_id = _feedback_id(str(material.id), comp_auid, version, labels)
    existing = ModelFeedback.objects(id=feedback_id).first()
    if existing is not None:
        return existing

    tolerances = {
        "efa": float(getattr(settings, "CHEMSCREEN_EFA_REWARD_TOLERANCE", 5.0)),
        "deed": float(getattr(settings, "CHEMSCREEN_DEED_REWARD_TOLERANCE", 2.0)),
    }
    results: Dict[str, Any] = {}
    scored: List[bool] = []
    for target, actual in labels.items():
        predicted = _safe_number(prediction.get(target))
        if predicted is None:
            continue
        error = abs(actual - predicted)
        correct = error <= tolerances[target]
        scored.append(correct)
        results[target] = {
            "predicted": predicted,
            "actual": actual,
            "absolute_error": error,
            "tolerance": tolerances[target],
            "correct": correct,
        }
    if not scored:
        outcome = "unscored"
        weight = 1.0
        reason = "No active model existed before this ground-truth record."
    elif all(scored):
        outcome = "reward"
        weight = float(getattr(settings, "CHEMSCREEN_REWARD_WEIGHT", 1.25))
        reason = "All available predictions were within their error tolerances."
    else:
        outcome = "flag"
        weight = float(getattr(settings, "CHEMSCREEN_FLAG_WEIGHT", 2.0))
        reason = "At least one prediction exceeded its error tolerance."
    feedback = ModelFeedback(
        id=feedback_id,
        material_auid=str(material.id),
        comp_auid=comp_auid,
        model_version=version,
        outcome=outcome,
        results=results,
        sample_weight=weight,
        reason=reason,
    )
    feedback.save()
    return feedback


def enqueue_model_retraining(
    *,
    material_auids: Optional[Iterable[str]] = None,
    reason: str = "Data changed",
    force: bool = False,
) -> Optional[ModelRetrainJob]:
    if not force and not bool(getattr(settings, "CHEMSCREEN_AUTOTRAIN_ENABLED", True)):
        return None
    ids = sorted({str(value) for value in (material_auids or []) if value})
    pending = ModelRetrainJob.objects(status="pending").order_by("-requested_at").first()
    if pending is not None:
        update: Dict[str, Any] = {
            "$inc": {"trigger_count": 1},
            "$set": {
                "reason": reason,
                "requested_at": _utc_now(),
                "updated_at": _utc_now(),
            },
        }
        if ids:
            update["$addToSet"] = {"material_auids": {"$each": ids}}
        ModelRetrainJob._get_collection().update_one({"_id": pending.id}, update)
        return ModelRetrainJob.objects(id=pending.id).first()
    job = ModelRetrainJob(
        reason=reason,
        material_auids=ids,
        trigger_count=1,
        status="pending",
    )
    job.save()
    return job


def _claim_next_job() -> Optional[ModelRetrainJob]:
    debounce = float(getattr(settings, "CHEMSCREEN_RETRAIN_DEBOUNCE_SECONDS", 15))
    cutoff = _utc_now() - timedelta(seconds=max(0.0, debounce))
    return ModelRetrainJob.objects(
        status="pending",
        requested_at__lte=cutoff,
    ).order_by("requested_at").modify(
        new=True,
        set__status="processing",
        set__started_at=_utc_now(),
        set__updated_at=_utc_now(),
        inc__attempts=1,
    )


def process_one_training_job(job: ModelRetrainJob) -> str:
    try:
        labeled_materials: List[Any] = []
        for material_id in job.material_auids or []:
            material = Material.objects(id=material_id).first()
            if material is not None and material_has_training_labels(material):
                labeled_materials.append(material)
                record_feedback(material)

        if bool(getattr(settings, "AFLOW_API_ENABLED", True)):
            from .aflow_client import fetch_aflow_records

            refresh_limit = int(getattr(settings, "AFLOW_REFRESH_PER_TRAINING_JOB", 10))
            for material in labeled_materials[:max(0, refresh_limit)]:
                fetch_aflow_records(material.elements)

        result = train_and_maybe_promote(reason=job.reason or "")
        job.model_version = str(result.get("model_version") or "")
        job.result_summary = result
        job.status = "skipped" if result.get("status") == "skipped" else "done"
        job.last_error = ""
    except Exception as exc:
        job.status = "failed"
        job.last_error = repr(exc)[:4000]
        logger.exception("ChemScreen model retraining failed for job %s", job.id)
    job.completed_at = _utc_now()
    job.save()
    return job.status


def process_pending_training_jobs(limit: int = 1) -> Dict[str, int]:
    counts = {"done": 0, "skipped": 0, "failed": 0}
    for _ in range(max(1, int(limit))):
        job = _claim_next_job()
        if job is None:
            break
        status = process_one_training_job(job)
        counts[status] = counts.get(status, 0) + 1
    return counts


def model_status() -> Dict[str, Any]:
    active = ModelVersion.objects(active=True).first()
    latest_job = ModelRetrainJob.objects.order_by("-created_at").first()
    rewards = ModelFeedback.objects(outcome="reward").count()
    flags = ModelFeedback.objects(outcome="flag").count()
    return {
        "active": active,
        "latest_job": latest_job,
        "reward_count": rewards,
        "flag_count": flags,
    }


__all__ = [
    "FEATURE_NAMES",
    "TARGETS",
    "collect_training_data",
    "enqueue_model_retraining",
    "material_features",
    "material_has_training_labels",
    "model_status",
    "predict_material",
    "predict_materials",
    "process_pending_training_jobs",
    "record_feedback",
    "train_and_maybe_promote",
]
