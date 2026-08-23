from __future__ import annotations

import csv
import json
import mimetypes
import os
import platform
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from catalog import raw_db, xrd_store
from catalog.canonical import (
    apply_artifact_mode,
    apply_artifact_mode_tree,
    atomic_write_bytes,
)
from catalog.gsas_runtime import import_gsas_modules
from catalog.upload_archive import add_files

from .candidates import load_reference_phase_snapshot
from .reporting import (
    canonical_json_bytes,
    detected_package_versions,
    python_version_string,
    sha256_digest,
    sha256_file,
)
from .schemas import (
    CacheValidationResult,
    CompactAnalysisSummary,
    DEFAULT_XRD_ANALYSIS_CONFIG,
    LinkedStructureReference,
    LinkedStructureSnapshotRecord,
    PersistedArtifactRecord,
    PersistedXRDAnalysis,
    RankedPhaseCandidate,
    ReproducibilityManifest,
    SinglePhaseHypothesisResult,
    TwoPhaseHypothesisResult,
    XRDAnalysisConfig,
    XRDAnalysisInput,
    XRDAnalysisResult,
    to_jsonable,
)


INPUT_SCHEMA_VERSION = "loop-xrd-analysis-input-v2"
RESULT_SCHEMA_VERSION = "loop-xrd-analysis-result-v1"
PARSING_METHOD = "catalog.utils.parse_xrd_file+catalog.xrd_analysis.pattern.normalize_table_pattern"
CANDIDATE_SIMULATION_METHOD = "gsasii_screening_reflection_adapter_v1"
REFINEMENT_METHOD = "gsasii_conservative_refinement_schedule_v1"
MODEL_COMPARISON_FORMULAS: tuple[str, ...] = (
    "wrss=sum(((y_obs-y_calc)/sqrt(max(abs(y_obs),1.0)))^2)",
    "aic=n*ln(max(wrss/n,1e-12))+2k",
    "aicc=aic+2k(k+1)/(n-k-1) when n>k+1",
    "bic=n*ln(max(wrss/n,1e-12))+k*ln(n)",
)
IDENTITY_FIELDS: tuple[str, ...] = (
    "raw_file_hash",
    "measurement_metadata",
    "nominal_composition",
    "elements",
    "stoichiometric_amounts",
    "intended_structure_metadata",
    "instrument_profile",
    "synthesis_context",
    "algorithm_version",
    "configuration_version",
    "configuration_hash",
    "parsing_method",
    "reference_phase_snapshot",
    "linked_structure_snapshots",
    "gsasii_version",
    "candidate_simulation_method",
    "candidate_simulation_versions",
    "refinement_method",
)
PROVENANCE_ONLY_FIELDS: tuple[str, ...] = (
    "material_auid",
    "recipe_auid",
    "trial_id",
    "input_warnings",
    "analysis_warnings",
    "result_hash",
    "artifact_hashes",
    "started_at",
    "completed_at",
    "execution_environment_notes",
)
PACKAGE_VERSION_NAMES: tuple[str, ...] = (
    "numpy",
    "pandas",
    "scipy",
    "Django",
    "mongoengine",
)
REQUIRED_ARTIFACT_FILENAMES: tuple[str, ...] = (
    "result.json",
    "reproducibility.json",
    "candidates.json",
    "single_phase_hypotheses.json",
    "two_phase_hypotheses.json",
    "model_comparison.json",
    "pattern_observed.csv",
    "pattern_best_model.csv",
    "reflections.json",
    "selected_cifs.json",
)


class XRDAnalysisPersistenceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class EstablishedAnalysisIdentity:
    analysis_id: str
    identity_payload: dict[str, Any]
    reference_snapshot: dict[str, Optional[str]]
    linked_structure_snapshots: tuple[LinkedStructureSnapshotRecord, ...]
    gsasii_version: Optional[str]
    candidate_simulation_versions: tuple[str, ...]


def run_and_persist_xrd_analysis(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> PersistedXRDAnalysis:
    from .pipeline import run_xrd_analysis_pipeline

    identity = _resolve_established_identity(
        analysis_input,
        configuration=configuration,
    )
    cache_validation = validate_persisted_xrd_analysis(
        analysis_input,
        configuration=configuration,
        analysis_id=identity.analysis_id,
        reference_snapshot=identity.reference_snapshot,
        linked_structure_snapshots=identity.linked_structure_snapshots,
        gsasii_version=identity.gsasii_version,
        candidate_simulation_versions=identity.candidate_simulation_versions,
    )
    if cache_validation.valid:
        return load_persisted_xrd_analysis(
            analysis_input,
            configuration=configuration,
            analysis_id=identity.analysis_id,
            cache_validation=cache_validation,
        )
    if cache_validation.failure_code not in (None, "analysis_cache_not_found"):
        _quarantine_analysis_directory(Path(cache_validation.analysis_directory))

    started_at = _utc_now()
    result = run_xrd_analysis_pipeline(analysis_input, configuration=configuration)
    completed_at = _utc_now()
    return persist_xrd_analysis_result(
        analysis_input,
        result,
        configuration=configuration,
        analysis_id=identity.analysis_id,
        identity_payload=identity.identity_payload,
        reference_snapshot=identity.reference_snapshot,
        linked_structure_snapshots=identity.linked_structure_snapshots,
        gsasii_version=identity.gsasii_version,
        candidate_simulation_versions=identity.candidate_simulation_versions,
        started_at=started_at,
        completed_at=completed_at,
    )


def compute_analysis_identity(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> tuple[
    str,
    dict[str, Any],
    dict[str, Optional[str]],
    tuple[LinkedStructureSnapshotRecord, ...],
    Optional[str],
    tuple[str, ...],
]:
    reference_snapshot = _current_reference_snapshot()
    linked_snapshots = _linked_structure_snapshots(analysis_input.linked_structure_references)
    gsasii_version = _detect_gsasii_version()
    candidate_simulation_versions = (configuration.configuration_version,)
    payload = build_analysis_identity_payload(
        analysis_input,
        configuration=configuration,
        configuration_hash=configuration.content_hash(),
        reference_snapshot=reference_snapshot,
        linked_structure_snapshots=linked_snapshots,
        gsasii_version=gsasii_version,
        candidate_simulation_versions=candidate_simulation_versions,
    )
    return (
        sha256_digest(payload),
        payload,
        reference_snapshot,
        linked_snapshots,
        gsasii_version,
        candidate_simulation_versions,
    )


def _resolve_established_identity(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig,
    analysis_id: str | None = None,
    identity_payload: dict[str, Any] | None = None,
    reference_snapshot: Optional[dict[str, Optional[str]]] = None,
    linked_structure_snapshots: Optional[tuple[LinkedStructureSnapshotRecord, ...]] = None,
    gsasii_version: Optional[str] = None,
    candidate_simulation_versions: Optional[tuple[str, ...]] = None,
) -> EstablishedAnalysisIdentity:
    computed_payload = None
    if (
        analysis_id is None
        or identity_payload is None
        or reference_snapshot is None
        or linked_structure_snapshots is None
        or candidate_simulation_versions is None
    ):
        (
            computed_analysis_id,
            computed_payload,
            computed_reference_snapshot,
            computed_linked_snapshots,
            computed_gsasii_version,
            computed_candidate_versions,
        ) = compute_analysis_identity(analysis_input, configuration=configuration)
        if analysis_id is None:
            analysis_id = computed_analysis_id
        if identity_payload is None:
            identity_payload = computed_payload
        if reference_snapshot is None:
            reference_snapshot = computed_reference_snapshot
        if linked_structure_snapshots is None:
            linked_structure_snapshots = computed_linked_snapshots
        if gsasii_version is None:
            gsasii_version = computed_gsasii_version
        if candidate_simulation_versions is None:
            candidate_simulation_versions = computed_candidate_versions

    assert analysis_id is not None
    assert identity_payload is not None
    assert reference_snapshot is not None
    assert linked_structure_snapshots is not None
    assert candidate_simulation_versions is not None

    canonical_payload = build_analysis_identity_payload(
        analysis_input,
        configuration=configuration,
        configuration_hash=configuration.content_hash(),
        reference_snapshot=reference_snapshot,
        linked_structure_snapshots=linked_structure_snapshots,
        gsasii_version=gsasii_version,
        candidate_simulation_versions=candidate_simulation_versions,
    )
    canonical_analysis_id = sha256_digest(canonical_payload)
    if analysis_id != canonical_analysis_id:
        raise XRDAnalysisPersistenceError(
            "analysis_identity_mismatch",
            "Established analysis identity does not match the canonical input identity payload.",
        )
    if identity_payload != canonical_payload:
        raise XRDAnalysisPersistenceError(
            "analysis_identity_mismatch",
            "Established analysis identity payload does not match the canonical input identity payload.",
        )
    return EstablishedAnalysisIdentity(
        analysis_id=analysis_id,
        identity_payload=canonical_payload,
        reference_snapshot=reference_snapshot,
        linked_structure_snapshots=linked_structure_snapshots,
        gsasii_version=gsasii_version,
        candidate_simulation_versions=candidate_simulation_versions,
    )


def persist_xrd_analysis_result(
    analysis_input: XRDAnalysisInput,
    result: XRDAnalysisResult,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
    analysis_id: str | None = None,
    identity_payload: dict[str, Any] | None = None,
    reference_snapshot: Optional[dict[str, Optional[str]]] = None,
    linked_structure_snapshots: Optional[tuple[LinkedStructureSnapshotRecord, ...]] = None,
    gsasii_version: Optional[str] = None,
    candidate_simulation_versions: Optional[tuple[str, ...]] = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    execution_environment_notes: Iterable[str] = (),
) -> PersistedXRDAnalysis:
    completed_time = completed_at or _utc_now()
    identity = _resolve_established_identity(
        analysis_input,
        configuration=configuration,
        analysis_id=analysis_id,
        identity_payload=identity_payload,
        reference_snapshot=reference_snapshot,
        linked_structure_snapshots=linked_structure_snapshots,
        gsasii_version=gsasii_version,
        candidate_simulation_versions=candidate_simulation_versions,
    )
    configuration_hash = configuration.content_hash()
    analysis_id = identity.analysis_id
    identity_payload = identity.identity_payload
    reference_snapshot = identity.reference_snapshot
    linked_snapshots = identity.linked_structure_snapshots
    gsasii_version = identity.gsasii_version
    candidate_simulation_versions = identity.candidate_simulation_versions
    result_with_id = replace(result, analysis_id=analysis_id)

    analysis_root = xrd_store.analysis_path(
        analysis_input.recipe_auid,
        analysis_input.trial_id,
        analysis_id,
    )
    cache_validation = validate_persisted_xrd_analysis(
        analysis_input,
        configuration=configuration,
        analysis_id=analysis_id,
        reference_snapshot=reference_snapshot,
        linked_structure_snapshots=linked_snapshots,
        gsasii_version=gsasii_version,
        candidate_simulation_versions=candidate_simulation_versions,
    )
    if cache_validation.valid:
        return load_persisted_xrd_analysis(
            analysis_input,
            configuration=configuration,
            analysis_id=analysis_id,
            cache_validation=cache_validation,
        )
    if analysis_root.exists() and cache_validation.failure_code not in (
        None,
        "analysis_cache_not_found",
    ):
        _quarantine_analysis_directory(analysis_root)
    analysis_root = xrd_store.analysis_dir(
        analysis_input.recipe_auid,
        analysis_input.trial_id,
        analysis_id,
    )
    reused_existing = False

    selected_model = _selected_model_hypothesis(result_with_id)
    candidate_lookup = {
        candidate.candidate_id: candidate
        for candidate in result_with_id.ranked_candidate_shortlist
    }

    try:
        artifact_specs = _build_artifact_specs(
            result_with_id,
            analysis_root=analysis_root,
            selected_model=selected_model,
            candidate_lookup=candidate_lookup,
        )
        created_artifacts = tuple(
            _write_artifact_spec(
                spec,
                parent_raw_file_hash=analysis_input.raw_file_hash,
                analysis_id=analysis_id,
                algorithm_version=result_with_id.algorithm_version,
            )
            for spec in artifact_specs
        )
    except Exception as exc:
        raise XRDAnalysisPersistenceError(
            "artifact_write_failed",
            f"Unable to write persisted XRD analysis artifacts: {exc}",
        ) from exc

    try:
        result_hash = next(
            artifact.sha256
            for artifact in created_artifacts
            if artifact.artifact_type == "result_json"
        )
    except StopIteration as exc:
        raise XRDAnalysisPersistenceError(
            "result_serialization_failed",
            "Persisted XRD analysis did not produce a result.json artifact.",
        ) from exc
    manifest = build_reproducibility_manifest(
        analysis_input,
        result_with_id,
        configuration=configuration,
        configuration_hash=configuration_hash,
        reference_snapshot=reference_snapshot,
        linked_structure_snapshots=linked_snapshots,
        gsasii_version=gsasii_version,
        candidate_simulation_versions=candidate_simulation_versions,
        result_hash=result_hash,
        created_artifacts=created_artifacts,
        started_at=started_at,
        completed_at=completed_time,
        execution_environment_notes=tuple(execution_environment_notes)
        or _default_environment_notes(),
    )
    try:
        reproducibility_artifact = _write_json_artifact(
            analysis_root / "reproducibility.json",
            manifest,
            artifact_type="reproducibility_json",
            parent_raw_file_hash=analysis_input.raw_file_hash,
            analysis_id=analysis_id,
            algorithm_version=result_with_id.algorithm_version,
        )
    except Exception as exc:
        raise XRDAnalysisPersistenceError(
            "reproducibility_manifest_write_failed",
            f"Unable to write reproducibility manifest: {exc}",
        ) from exc
    all_artifacts = created_artifacts + (reproducibility_artifact,)
    persistence_warning_codes: list[str] = []
    if not any(str(artifact.relative_path).endswith(".gpx") for artifact in all_artifacts):
        persistence_warning_codes.append("selected_gsas_project_unavailable")

    summary = CompactAnalysisSummary(
        analysis_id=analysis_id,
        analysis_status="completed",
        phase_state=result_with_id.phase_state,
        evidence_score=result_with_id.evidence_score,
        warning_count=len(result_with_id.warnings),
        algorithm_version=result_with_id.algorithm_version,
        configuration_version=result_with_id.configuration_version,
        selected_model_type=(
            result_with_id.selected_best_model.model_type
            if result_with_id.selected_best_model is not None
            else None
        ),
        selected_candidate_ids=(
            result_with_id.selected_best_model.candidate_ids
            if result_with_id.selected_best_model is not None
            else ()
        ),
        failure_code_count=len(result_with_id.failure_codes),
        reference_snapshot_identity=reference_snapshot.get("hash") or reference_snapshot.get("version"),
        completion_time=completed_time.isoformat(),
        result_manifest_path=f"{_analysis_relative_root(analysis_input, analysis_id)}/reproducibility.json",
        best_hypothesis_id=(
            result_with_id.best_hypothesis.hypothesis_id
            if result_with_id.best_hypothesis is not None
            else None
        ),
    )
    try:
        xrd_store.write_analysis_summary(
            analysis_input.recipe_auid,
            analysis_input.trial_id,
            analysis_id,
            to_jsonable(summary),
        )
    except Exception as exc:
        raise XRDAnalysisPersistenceError(
            "analysis_persistence_failed",
            f"Unable to write XRD analysis summary indexes: {exc}",
        ) from exc

    try:
        _register_artifacts_with_raw_db(
            raw_file_hash=analysis_input.raw_file_hash,
            artifacts=all_artifacts,
            analysis_id=analysis_id,
            algorithm_version=result_with_id.algorithm_version,
            configuration_version=result_with_id.configuration_version,
        )
    except Exception:
        persistence_warning_codes.append("raw_file_manifest_update_failed")
    # Sweep the finished analysis before it is copied anywhere. Per-write-site
    # chmods only cover the paths we know about, and the archive backfill below
    # uses shutil.copy2, which preserves the source mode -- so one artifact
    # written 0600 by any path would propagate into raw-uploads as well. Both
    # locations are rsynced offsite nightly by an unrelated account, and that
    # job fails outright on a single unreadable file.
    apply_artifact_mode_tree(analysis_root)

    try:
        _backfill_archive(
            raw_file_hash=analysis_input.raw_file_hash,
            trial_root=xrd_store.trial_dir(analysis_input.recipe_auid, analysis_input.trial_id),
            analysis_root=analysis_root,
        )
    except Exception:
        persistence_warning_codes.append("upload_archive_backfill_failed")

    return PersistedXRDAnalysis(
        analysis_id=analysis_id,
        analysis_directory=str(analysis_root),
        result=result_with_id,
        summary=summary,
        reproducibility_manifest=manifest,
        persisted_artifacts=all_artifacts,
        reused_existing=reused_existing,
        persistence_warning_codes=tuple(dict.fromkeys(persistence_warning_codes)),
    )


def validate_persisted_xrd_analysis(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
    analysis_id: Optional[str] = None,
    reference_snapshot: Optional[dict[str, Optional[str]]] = None,
    linked_structure_snapshots: Optional[tuple[LinkedStructureSnapshotRecord, ...]] = None,
    gsasii_version: Optional[str] = None,
    candidate_simulation_versions: Optional[tuple[str, ...]] = None,
) -> CacheValidationResult:
    if analysis_id is None:
        (
            analysis_id,
            _,
            reference_snapshot,
            linked_structure_snapshots,
            gsasii_version,
            candidate_simulation_versions,
        ) = compute_analysis_identity(analysis_input, configuration=configuration)
    del linked_structure_snapshots, gsasii_version, candidate_simulation_versions

    analysis_root = xrd_store.analysis_path(
        analysis_input.recipe_auid,
        analysis_input.trial_id,
        analysis_id,
    )
    if not analysis_root.is_dir():
        return CacheValidationResult(
            analysis_id=analysis_id,
            analysis_directory=str(analysis_root),
            valid=False,
            status="cache_miss",
            failure_code="analysis_cache_not_found",
        )

    missing_artifacts = tuple(
        name for name in REQUIRED_ARTIFACT_FILENAMES if not (analysis_root / name).is_file()
    )
    if missing_artifacts:
        return CacheValidationResult(
            analysis_id=analysis_id,
            analysis_directory=str(analysis_root),
            valid=False,
            status="cache_invalid",
            failure_code="analysis_cache_incomplete",
            missing_artifacts=missing_artifacts,
            detail="Required persisted artifacts are missing.",
        )

    try:
        manifest_payload = _read_json_file(analysis_root / "reproducibility.json")
    except Exception as exc:
        return CacheValidationResult(
            analysis_id=analysis_id,
            analysis_directory=str(analysis_root),
            valid=False,
            status="cache_invalid",
            failure_code="persisted_analysis_load_failed",
            detail=str(exc),
        )

    if manifest_payload.get("analysis_id") != analysis_id:
        return CacheValidationResult(
            analysis_id=analysis_id,
            analysis_directory=str(analysis_root),
            valid=False,
            status="cache_invalid",
            failure_code="analysis_cache_hash_mismatch",
            detail="Stored manifest analysis_id does not match the requested analysis.",
        )
    if manifest_payload.get("input_schema_version") != INPUT_SCHEMA_VERSION:
        return CacheValidationResult(
            analysis_id=analysis_id,
            analysis_directory=str(analysis_root),
            valid=False,
            status="cache_invalid",
            failure_code="analysis_cache_schema_mismatch",
            detail="Input schema version mismatch.",
        )
    if manifest_payload.get("result_schema_version") != RESULT_SCHEMA_VERSION:
        return CacheValidationResult(
            analysis_id=analysis_id,
            analysis_directory=str(analysis_root),
            valid=False,
            status="cache_invalid",
            failure_code="analysis_cache_schema_mismatch",
            detail="Result schema version mismatch.",
        )
    if manifest_payload.get("configuration_version") != configuration.configuration_version:
        return CacheValidationResult(
            analysis_id=analysis_id,
            analysis_directory=str(analysis_root),
            valid=False,
            status="cache_invalid",
            failure_code="analysis_cache_configuration_mismatch",
            detail="Configuration version mismatch.",
        )
    if manifest_payload.get("configuration_hash") != configuration.content_hash():
        return CacheValidationResult(
            analysis_id=analysis_id,
            analysis_directory=str(analysis_root),
            valid=False,
            status="cache_invalid",
            failure_code="analysis_cache_configuration_mismatch",
            detail="Configuration hash mismatch.",
        )

    expected_reference_snapshot = reference_snapshot or _current_reference_snapshot()
    if (
        manifest_payload.get("reference_phase_snapshot_version")
        != expected_reference_snapshot.get("version")
        or manifest_payload.get("reference_phase_snapshot_hash")
        != expected_reference_snapshot.get("hash")
    ):
        return CacheValidationResult(
            analysis_id=analysis_id,
            analysis_directory=str(analysis_root),
            valid=False,
            status="cache_invalid",
            failure_code="analysis_cache_reference_snapshot_mismatch",
            detail="Reference snapshot identity mismatch.",
        )

    result_path = analysis_root / "result.json"
    if manifest_payload.get("result_hash") != sha256_file(result_path):
        return CacheValidationResult(
            analysis_id=analysis_id,
            analysis_directory=str(analysis_root),
            valid=False,
            status="cache_invalid",
            failure_code="analysis_cache_hash_mismatch",
            detail="Stored result hash does not match result.json.",
        )

    for artifact in manifest_payload.get("created_artifacts", []):
        relative_path = artifact.get("relative_path")
        expected_hash = artifact.get("sha256")
        if not relative_path:
            return CacheValidationResult(
                analysis_id=analysis_id,
                analysis_directory=str(analysis_root),
                valid=False,
                status="cache_invalid",
                failure_code="analysis_cache_incomplete",
                detail="Artifact manifest entry is missing a relative path.",
            )
        artifact_path = _media_root() / relative_path
        if not artifact_path.is_file():
            return CacheValidationResult(
                analysis_id=analysis_id,
                analysis_directory=str(analysis_root),
                valid=False,
                status="cache_invalid",
                failure_code="analysis_cache_incomplete",
                missing_artifacts=(relative_path,),
                detail="A persisted artifact listed in the manifest is missing.",
            )
        if expected_hash and sha256_file(artifact_path) != expected_hash:
            return CacheValidationResult(
                analysis_id=analysis_id,
                analysis_directory=str(analysis_root),
                valid=False,
                status="cache_invalid",
                failure_code="analysis_cache_hash_mismatch",
                missing_artifacts=(relative_path,),
                detail="A persisted artifact hash does not match the manifest.",
            )

    return CacheValidationResult(
        analysis_id=analysis_id,
        analysis_directory=str(analysis_root),
        valid=True,
        status="cache_valid",
    )


def load_persisted_xrd_analysis(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
    analysis_id: Optional[str] = None,
    cache_validation: Optional[CacheValidationResult] = None,
) -> PersistedXRDAnalysis:
    validation = cache_validation or validate_persisted_xrd_analysis(
        analysis_input,
        configuration=configuration,
        analysis_id=analysis_id,
    )
    if not validation.valid:
        raise FileNotFoundError(
            validation.detail or validation.failure_code or "analysis cache is not valid"
        )

    analysis_root = Path(validation.analysis_directory)
    result_payload = _read_json_file(analysis_root / "result.json")
    manifest_payload = _read_json_file(analysis_root / "reproducibility.json")
    summary_payload = _load_summary_payload(
        analysis_input.recipe_auid,
        analysis_input.trial_id,
        validation.analysis_id,
    )
    reproducibility_artifact = _artifact_record(
        analysis_root / "reproducibility.json",
        artifact_type="reproducibility_json",
        parent_raw_file_hash=analysis_input.raw_file_hash,
        analysis_id=validation.analysis_id,
        algorithm_version=str(
            manifest_payload.get("algorithm_version") or configuration.algorithm_version
        ),
        content_type="application/json",
    )
    persisted_artifacts = tuple(manifest_payload.get("created_artifacts", [])) + (
        reproducibility_artifact,
    )
    return PersistedXRDAnalysis(
        analysis_id=validation.analysis_id,
        analysis_directory=str(analysis_root),
        result=result_payload,
        summary=summary_payload,
        reproducibility_manifest=manifest_payload,
        persisted_artifacts=persisted_artifacts,
        reused_existing=True,
        persistence_warning_codes=_persisted_warning_codes_from_artifacts(persisted_artifacts),
    )


def build_analysis_identity_payload(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig,
    configuration_hash: str,
    reference_snapshot: dict[str, Optional[str]],
    linked_structure_snapshots: tuple[LinkedStructureSnapshotRecord, ...],
    gsasii_version: Optional[str],
    candidate_simulation_versions: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "raw_file_hash": analysis_input.raw_file_hash,
        "measurement_metadata": {
            "radiation_source": analysis_input.radiation_source,
            "wavelength_angstrom": analysis_input.wavelength_angstrom,
            "coordinate_type": analysis_input.coordinate_type,
            "coordinate_column": analysis_input.coordinate_column,
            "intensity_column": analysis_input.intensity_column,
            "scan_min": analysis_input.scan_min,
            "scan_max": analysis_input.scan_max,
            "step_size": analysis_input.step_size,
            "scan_speed": analysis_input.scan_speed,
        },
        "nominal_composition": analysis_input.nominal_composition,
        "elements": sorted(analysis_input.elements),
        "stoichiometric_amounts": [
            {"element": item.element, "amount": item.amount}
            for item in sorted(
                analysis_input.stoichiometric_amounts,
                key=lambda item: item.element,
            )
        ],
        "intended_structure_metadata": {
            "structure_family": analysis_input.structure_family,
            "expected_space_group": analysis_input.expected_space_group,
            "expected_site_assignments": [
                {"element": item.element, "site_label": item.site_label}
                for item in sorted(
                    analysis_input.expected_site_assignments,
                    key=lambda item: (item.site_label, item.element),
                )
            ],
        },
        "instrument_profile": _normalized_instrument_profile(analysis_input),
        "synthesis_context": _normalized_synthesis_context(analysis_input),
        "algorithm_version": analysis_input.algorithm_version,
        "configuration_version": analysis_input.configuration_version,
        "configuration_hash": configuration_hash,
        "parsing_method": PARSING_METHOD,
        "reference_phase_snapshot": reference_snapshot,
        "linked_structure_snapshots": linked_structure_snapshots,
        "gsasii_version": gsasii_version,
        "candidate_simulation_method": CANDIDATE_SIMULATION_METHOD,
        "candidate_simulation_versions": list(candidate_simulation_versions),
        "refinement_method": REFINEMENT_METHOD,
        "configuration_serialization": configuration.canonical_json(),
    }


def build_reproducibility_manifest(
    analysis_input: XRDAnalysisInput,
    result: XRDAnalysisResult,
    *,
    configuration: XRDAnalysisConfig,
    configuration_hash: str,
    reference_snapshot: dict[str, Optional[str]],
    linked_structure_snapshots: tuple[LinkedStructureSnapshotRecord, ...],
    gsasii_version: Optional[str],
    candidate_simulation_versions: tuple[str, ...],
    result_hash: str,
    created_artifacts: tuple[PersistedArtifactRecord, ...],
    started_at: datetime | None,
    completed_at: datetime,
    execution_environment_notes: tuple[str, ...],
) -> ReproducibilityManifest:
    return ReproducibilityManifest(
        analysis_id=result.analysis_id or "",
        raw_file_hash=analysis_input.raw_file_hash,
        material_auid=analysis_input.material_auid,
        recipe_auid=analysis_input.recipe_auid,
        trial_id=analysis_input.trial_id,
        input_schema_version=INPUT_SCHEMA_VERSION,
        result_schema_version=RESULT_SCHEMA_VERSION,
        algorithm_version=result.algorithm_version,
        configuration_version=result.configuration_version,
        configuration_hash=configuration_hash,
        configuration_serialization=configuration.canonical_json(),
        reference_phase_snapshot_version=reference_snapshot.get("version"),
        reference_phase_snapshot_hash=reference_snapshot.get("hash"),
        linked_structure_snapshots=linked_structure_snapshots,
        gsasii_version=gsasii_version,
        python_version=python_version_string(),
        package_versions=detected_package_versions(*PACKAGE_VERSION_NAMES),
        parsing_method=PARSING_METHOD,
        candidate_simulation_method=_candidate_simulation_method_label(
            candidate_simulation_versions
        ),
        refinement_method=REFINEMENT_METHOD,
        model_comparison_formulas=MODEL_COMPARISON_FORMULAS,
        classification_thresholds={
            "single_phase_refinement": to_jsonable(configuration.single_phase_refinement),
            "decision": to_jsonable(configuration.decision),
        },
        identity_fields=IDENTITY_FIELDS,
        provenance_only_fields=PROVENANCE_ONLY_FIELDS,
        input_warnings=analysis_input.warnings,
        analysis_warnings=result.warnings,
        created_artifacts=created_artifacts,
        result_hash=result_hash,
        started_at=started_at.isoformat() if started_at else None,
        completed_at=completed_at.isoformat(),
        execution_environment_notes=execution_environment_notes,
    )


def _build_artifact_specs(
    result: XRDAnalysisResult,
    *,
    analysis_root: Path,
    selected_model: tuple[str, Any] | None,
    candidate_lookup: dict[str, RankedPhaseCandidate],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = [
        {
            "path": analysis_root / "result.json",
            "writer": "json",
            "artifact_type": "result_json",
            "content": result,
        },
        {
            "path": analysis_root / "candidates.json",
            "writer": "json",
            "artifact_type": "candidates_json",
            "content": {
                "ranked_candidate_shortlist": result.ranked_candidate_shortlist,
                "phase_candidates": result.phase_candidates,
                "analysis_provenance_notes": result.analysis_provenance_notes,
            },
        },
        {
            "path": analysis_root / "single_phase_hypotheses.json",
            "writer": "json",
            "artifact_type": "single_phase_hypotheses_json",
            "content": {
                "successful": result.successful_single_phase_hypotheses,
                "failed": result.failed_single_phase_attempts,
            },
        },
        {
            "path": analysis_root / "two_phase_hypotheses.json",
            "writer": "json",
            "artifact_type": "two_phase_hypotheses_json",
            "content": {
                "successful": result.successful_two_phase_hypotheses,
                "failed": result.failed_two_phase_attempts,
            },
        },
        {
            "path": analysis_root / "model_comparison.json",
            "writer": "json",
            "artifact_type": "model_comparison_json",
            "content": {
                "model_comparison": result.model_comparison,
                "decision_criteria": result.decision_criteria,
                "stability_results": result.stability_results,
                "evidence_components": result.evidence_components,
                "selected_best_model": result.selected_best_model,
            },
        },
        {
            "path": analysis_root / "pattern_observed.csv",
            "writer": "csv",
            "artifact_type": "pattern_observed_csv",
            "fieldnames": ("two_theta", "observed_intensity"),
            "rows": _observed_pattern_rows(result, selected_model),
        },
        {
            "path": analysis_root / "pattern_best_model.csv",
            "writer": "csv",
            "artifact_type": "pattern_best_model_csv",
            "fieldnames": (
                "two_theta",
                "observed_intensity",
                "calculated_total",
                "background",
                "difference",
            ),
            "rows": _best_model_pattern_rows(result, selected_model),
        },
        {
            "path": analysis_root / "reflections.json",
            "writer": "json",
            "artifact_type": "reflections_json",
            "content": _reflections_payload(selected_model),
        },
        {
            "path": analysis_root / "selected_cifs.json",
            "writer": "json",
            "artifact_type": "selected_cifs_json",
            "content": _selected_cif_payload(selected_model, candidate_lookup),
        },
    ]
    specs.extend(
        _selected_cif_copy_specs(
            selected_model,
            analysis_root=analysis_root,
            candidate_lookup=candidate_lookup,
        )
    )
    return specs


def _write_artifact_spec(
    spec: dict[str, Any],
    *,
    parent_raw_file_hash: Optional[str],
    analysis_id: str,
    algorithm_version: str,
) -> PersistedArtifactRecord:
    writer = str(spec["writer"])
    path = Path(spec["path"])
    artifact_type = str(spec["artifact_type"])
    if writer == "json":
        return _write_json_artifact(
            path,
            spec["content"],
            artifact_type=artifact_type,
            parent_raw_file_hash=parent_raw_file_hash,
            analysis_id=analysis_id,
            algorithm_version=algorithm_version,
        )
    if writer == "csv":
        return _write_csv_artifact(
            path,
            fieldnames=spec["fieldnames"],
            rows=spec["rows"],
            artifact_type=artifact_type,
            parent_raw_file_hash=parent_raw_file_hash,
            analysis_id=analysis_id,
            algorithm_version=algorithm_version,
        )
    if writer == "copy":
        return _copy_artifact(
            Path(spec["source"]),
            path,
            artifact_type=artifact_type,
            parent_raw_file_hash=parent_raw_file_hash,
            analysis_id=analysis_id,
            algorithm_version=algorithm_version,
            content_type=str(spec["content_type"]),
        )
    raise ValueError(f"Unsupported artifact writer: {writer}")


def _write_json_artifact(
    path: Path,
    value: Any,
    *,
    artifact_type: str,
    parent_raw_file_hash: Optional[str],
    analysis_id: str,
    algorithm_version: str,
) -> PersistedArtifactRecord:
    _atomic_write_bytes(path, canonical_json_bytes(value))
    return _artifact_record(
        path,
        artifact_type=artifact_type,
        parent_raw_file_hash=parent_raw_file_hash,
        analysis_id=analysis_id,
        algorithm_version=algorithm_version,
        content_type="application/json",
    )


def _write_csv_artifact(
    path: Path,
    *,
    fieldnames: Iterable[str],
    rows: Iterable[dict[str, Any]],
    artifact_type: str,
    parent_raw_file_hash: Optional[str],
    analysis_id: str,
    algorithm_version: str,
) -> PersistedArtifactRecord:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        delete=False,
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    apply_artifact_mode(temp_name)
    os.replace(temp_name, path)
    return _artifact_record(
        path,
        artifact_type=artifact_type,
        parent_raw_file_hash=parent_raw_file_hash,
        analysis_id=analysis_id,
        algorithm_version=algorithm_version,
        content_type="text/csv",
    )


def _copy_artifact(
    source: Path,
    destination: Path,
    *,
    artifact_type: str,
    parent_raw_file_hash: Optional[str],
    analysis_id: str,
    algorithm_version: str,
    content_type: str,
) -> PersistedArtifactRecord:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=destination.parent, delete=False) as handle:
        with open(source, "rb") as src_handle:
            shutil.copyfileobj(src_handle, handle)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    apply_artifact_mode(temp_name)
    os.replace(temp_name, destination)
    return _artifact_record(
        destination,
        artifact_type=artifact_type,
        parent_raw_file_hash=parent_raw_file_hash,
        analysis_id=analysis_id,
        algorithm_version=algorithm_version,
        content_type=content_type,
    )


def _artifact_record(
    path: Path,
    *,
    artifact_type: str,
    parent_raw_file_hash: Optional[str],
    analysis_id: str,
    algorithm_version: str,
    content_type: str,
) -> PersistedArtifactRecord:
    from django.conf import settings

    media_root = Path(settings.MEDIA_ROOT).resolve()
    relative_path = str(path.resolve().relative_to(media_root)).replace(os.sep, "/")
    return PersistedArtifactRecord(
        relative_path=relative_path,
        sha256=sha256_file(path),
        content_type=content_type,
        size_bytes=path.stat().st_size,
        artifact_type=artifact_type,
        parent_raw_file_hash=parent_raw_file_hash,
        analysis_id=analysis_id,
        algorithm_version=algorithm_version,
    )


def _register_artifacts_with_raw_db(
    *,
    raw_file_hash: Optional[str],
    artifacts: tuple[PersistedArtifactRecord, ...],
    analysis_id: str,
    algorithm_version: str,
    configuration_version: str,
) -> None:
    if not raw_file_hash:
        return
    for artifact in artifacts:
        raw_db.record_derived_file(
            file_hash=raw_file_hash,
            kind="analysis",
            variant=artifact.artifact_type,
            stored_path=artifact.relative_path,
            url=_media_url(artifact.relative_path),
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            generated_at=_utc_now().isoformat(),
            content_type=artifact.content_type,
            artifact_type=artifact.artifact_type,
            parent_file_hash=raw_file_hash,
            analysis_id=analysis_id,
            algorithm_version=algorithm_version,
            configuration_version=configuration_version,
            content_hash=artifact.sha256,
            relative_path=artifact.relative_path,
        )


def _backfill_archive(*, raw_file_hash: Optional[str], trial_root: Path, analysis_root: Path) -> None:
    if not raw_file_hash:
        return
    row = raw_db.RawFile.objects(id=raw_file_hash).first()
    archive_folder = getattr(row, "archive_folder", None) if row else None
    if archive_folder:
        file_paths = [str(path) for path in sorted(analysis_root.rglob("*")) if path.is_file()]
        add_files(archive_folder, file_paths, relative_to=str(trial_root))


def _current_reference_snapshot() -> dict[str, Optional[str]]:
    try:
        snapshot = load_reference_phase_snapshot(validate_hashes=False)
    except Exception:
        return {"version": None, "hash": None}
    return {"version": snapshot.snapshot_version, "hash": snapshot.snapshot_hash}


def _analysis_relative_root(analysis_input: XRDAnalysisInput, analysis_id: str) -> str:
    return f"{xrd_store._trial_rel(analysis_input.recipe_auid, analysis_input.trial_id)}/analyses/{analysis_id}"


def _persisted_warning_codes_from_artifacts(
    artifacts: Iterable[PersistedArtifactRecord | dict[str, Any]],
) -> tuple[str, ...]:
    relative_paths: list[str] = []
    for artifact in artifacts:
        if isinstance(artifact, dict):
            relative_paths.append(str(artifact.get("relative_path") or ""))
        else:
            relative_paths.append(str(getattr(artifact, "relative_path", "")))
    if any(path.endswith(".gpx") for path in relative_paths):
        return ()
    return ("selected_gsas_project_unavailable",)


def _load_summary_payload(recipe_auid: str, trial_id: str, analysis_id: str) -> Any:
    summary_path = xrd_store.trial_path(recipe_auid, trial_id) / "analyses" / "index.json"
    if not summary_path.is_file():
        return None
    return (_read_json_file(summary_path) or {}).get(analysis_id)


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _quarantine_analysis_directory(analysis_root: Path) -> Path:
    timestamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    quarantine_path = analysis_root.with_name(f"{analysis_root.name}.corrupt-{timestamp}")
    if analysis_root.exists():
        analysis_root.rename(quarantine_path)
    return quarantine_path


def _media_root() -> Path:
    from django.conf import settings

    return Path(settings.MEDIA_ROOT).resolve()


def _reference_snapshot_for_result(result: XRDAnalysisResult) -> dict[str, Optional[str]]:
    del result
    return _current_reference_snapshot()


def _linked_structure_snapshots(
    references: tuple[LinkedStructureReference, ...],
) -> tuple[LinkedStructureSnapshotRecord, ...]:
    snapshots: list[LinkedStructureSnapshotRecord] = []
    for reference in sorted(
        references,
        key=lambda item: (
            item.source_kind,
            item.reference_id,
            item.source_identifier or "",
        ),
    ):
        cif_hash = None
        snapshot_hash = None
        if reference.cif_path and Path(reference.cif_path).is_file():
            cif_hash = sha256_file(reference.cif_path)
            snapshot_hash = sha256_digest(
                {
                    "reference_id": reference.reference_id,
                    "source_kind": reference.source_kind,
                    "source_identifier": reference.source_identifier,
                    "cif_hash": cif_hash,
                }
            )
        snapshots.append(
            LinkedStructureSnapshotRecord(
                reference_id=reference.reference_id,
                source_kind=reference.source_kind,
                source_identifier=reference.source_identifier,
                cif_hash=cif_hash,
                snapshot_hash=snapshot_hash,
            )
        )
    return tuple(snapshots)


def _extract_gsasii_version(result: XRDAnalysisResult) -> Optional[str]:
    hypotheses = (
        list(result.successful_two_phase_hypotheses)
        + list(result.successful_single_phase_hypotheses)
        + ([result.best_two_phase_hypothesis] if result.best_two_phase_hypothesis else [])
        + ([result.best_single_phase_hypothesis] if result.best_single_phase_hypothesis else [])
    )
    for hypothesis in hypotheses:
        if hypothesis is not None and hypothesis.gsasii_version:
            return hypothesis.gsasii_version
    return None


def _detect_gsasii_version() -> Optional[str]:
    try:
        GSASIIpath, _ = import_gsas_modules()
    except Exception:
        return None
    getter = getattr(GSASIIpath, "GetVersionNumber", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            return None
    return None


def _candidate_simulation_versions(result: XRDAnalysisResult) -> tuple[str, ...]:
    versions = {
        candidate.simulation.settings_version
        for candidate in result.ranked_candidate_shortlist
        if candidate.simulation is not None
    }
    return tuple(sorted(versions))


def _candidate_simulation_method_label(versions: tuple[str, ...]) -> str:
    if not versions:
        return CANDIDATE_SIMULATION_METHOD
    return f"{CANDIDATE_SIMULATION_METHOD}:{','.join(versions)}"


def _normalized_instrument_profile(analysis_input: XRDAnalysisInput) -> dict[str, Any]:
    profile = analysis_input.instrument_profile
    if profile is None:
        return {
            "instrument_label": None,
            "geometry": None,
            "sample_holder": None,
            "metadata_items": [],
            "instrument_parameter_path_present": False,
            "instrument_parameter_file_hash": None,
        }
    instrument_parameter_file_hash = None
    if profile.instrument_parameter_path:
        parameter_path = Path(profile.instrument_parameter_path).expanduser()
        if parameter_path.is_file():
            instrument_parameter_file_hash = sha256_file(parameter_path)
    return {
        "instrument_label": profile.instrument_label,
        "geometry": profile.geometry,
        "sample_holder": profile.sample_holder,
        "metadata_items": [
            {"key": item.key, "value": item.value}
            for item in sorted(
                profile.metadata_items,
                key=lambda item: (item.key, item.value or ""),
            )
        ],
        "instrument_parameter_path_present": bool(profile.instrument_parameter_path),
        "instrument_parameter_file_hash": instrument_parameter_file_hash,
    }


def _normalized_synthesis_context(analysis_input: XRDAnalysisInput) -> dict[str, Any]:
    context = analysis_input.synthesis_context
    return {
        "ordered_steps": [
            {
                "step_number": step.step_number,
                "step_type": step.step_type,
                "notes": step.notes,
                "atmosphere": step.atmosphere,
                "furnace_type": step.furnace_type,
                "temperature_c": step.temperature_c,
                "max_temp_c": step.max_temp_c,
                "ramp_rate_c_min": step.ramp_rate_c_min,
                "hold_time_hours": step.hold_time_hours,
                "hold_time_min": step.hold_time_min,
                "scan_speed_deg_min": step.scan_speed_deg_min,
                "step_size_deg": step.step_size_deg,
                "radiation": step.radiation,
                "two_theta_range": step.two_theta_range,
                "precursors": [
                    {
                        "name": precursor.name,
                        "formula": precursor.formula,
                        "cas_number": precursor.cas_number,
                        "purity": precursor.purity,
                        "supplier": precursor.supplier,
                        "notes": precursor.notes,
                    }
                    for precursor in sorted(
                        step.precursors,
                        key=lambda item: (
                            item.formula or "",
                            item.name or "",
                            item.cas_number or "",
                        ),
                    )
                ],
                "extra_fields": {
                    str(key): step.extra_fields[key]
                    for key in sorted(step.extra_fields)
                },
            }
            for step in context.ordered_steps
        ],
        "precursor_records": [
            {
                "name": precursor.name,
                "formula": precursor.formula,
                "cas_number": precursor.cas_number,
                "purity": precursor.purity,
                "supplier": precursor.supplier,
                "notes": precursor.notes,
            }
            for precursor in sorted(
                context.precursor_records,
                key=lambda item: (
                    item.formula or "",
                    item.name or "",
                    item.cas_number or "",
                ),
            )
        ],
        "temperatures_c": list(context.temperatures_c),
        "ramp_rates_c_min": list(context.ramp_rates_c_min),
        "hold_times_hours": list(context.hold_times_hours),
        "atmospheres": sorted(context.atmospheres),
        "furnace_types": sorted(context.furnace_types),
        "preparation_notes": list(context.preparation_notes),
    }


def _selected_model_hypothesis(
    result: XRDAnalysisResult,
) -> tuple[str, SinglePhaseHypothesisResult | TwoPhaseHypothesisResult] | None:
    selected = result.selected_best_model
    if (
        selected is not None
        and selected.model_type == "two_phase"
        and result.best_two_phase_hypothesis is not None
    ):
        return ("two_phase", result.best_two_phase_hypothesis)
    if (
        selected is not None
        and selected.model_type == "single_phase"
        and result.best_single_phase_hypothesis is not None
    ):
        return ("single_phase", result.best_single_phase_hypothesis)
    if result.best_two_phase_hypothesis is not None:
        return ("two_phase", result.best_two_phase_hypothesis)
    if result.best_single_phase_hypothesis is not None:
        return ("single_phase", result.best_single_phase_hypothesis)
    return None


def _observed_pattern_rows(
    result: XRDAnalysisResult,
    selected_model: tuple[str, Any] | None,
) -> list[dict[str, Any]]:
    if selected_model is not None:
        _, hypothesis = selected_model
        return [
            {"two_theta": two_theta, "observed_intensity": intensity}
            for two_theta, intensity in zip(
                hypothesis.observed_two_theta,
                hypothesis.observed_intensities,
            )
        ]
    parsed = result.parsed_pattern
    if parsed is None or parsed.normalized_two_theta is None:
        return []
    if len(parsed.original_intensities) == len(parsed.normalized_two_theta):
        intensities = parsed.original_intensities
    else:
        intensities = parsed.normalized_intensities
    rows: list[dict[str, Any]] = []
    for two_theta, intensity in zip(parsed.normalized_two_theta, intensities):
        if intensity is None:
            continue
        rows.append(
            {
                "two_theta": two_theta,
                "observed_intensity": float(intensity),
            }
        )
    return rows


def _best_model_pattern_rows(
    result: XRDAnalysisResult,
    selected_model: tuple[str, Any] | None,
) -> list[dict[str, Any]]:
    if selected_model is None:
        return [
            {
                "two_theta": row["two_theta"],
                "observed_intensity": row["observed_intensity"],
                "calculated_total": None,
                "background": None,
                "difference": None,
            }
            for row in _observed_pattern_rows(result, None)
        ]
    _, hypothesis = selected_model
    return [
        {
            "two_theta": two_theta,
            "observed_intensity": observed,
            "calculated_total": calculated,
            "background": background,
            "difference": difference,
        }
        for two_theta, observed, calculated, background, difference in zip(
            hypothesis.observed_two_theta,
            hypothesis.observed_intensities,
            hypothesis.calculated_total_pattern,
            hypothesis.calculated_background,
            hypothesis.difference_pattern,
        )
    ]


def _reflections_payload(
    selected_model: tuple[str, Any] | None,
) -> dict[str, Any]:
    if selected_model is None:
        return {"model_type": "none", "phases": []}
    model_type, hypothesis = selected_model
    if model_type == "single_phase":
        return {
            "model_type": "single_phase",
            "phases": [
                {
                    "candidate_id": hypothesis.candidate_id,
                    "candidate_source": hypothesis.candidate_source,
                    "candidate_source_identifier": hypothesis.candidate_source_identifier,
                    "candidate_cif_hash": hypothesis.candidate_cif_hash,
                    "reflections": hypothesis.expected_reflections,
                }
            ],
        }
    return {
        "model_type": "two_phase",
        "phases": [
            {
                "candidate_id": phase.candidate_id,
                "candidate_source": phase.candidate_source,
                "candidate_source_identifier": phase.candidate_source_identifier,
                "candidate_cif_hash": phase.candidate_cif_hash,
                "reflections": phase.expected_reflections,
            }
            for phase in hypothesis.phase_results
        ],
    }


def _selected_cif_payload(
    selected_model: tuple[str, Any] | None,
    candidate_lookup: dict[str, RankedPhaseCandidate],
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for candidate in _selected_candidates(selected_model, candidate_lookup):
        candidates.append(
            {
                "candidate_id": candidate.candidate_id,
                "candidate_source": candidate.source,
                "candidate_source_identifier": candidate.source_identifier,
                "candidate_snapshot": candidate.source_snapshot,
                "candidate_cif_hash": candidate.cif_hash,
                "candidate_cif_path": candidate.cif_path,
            }
        )
    return {
        "selected_model": selected_model[0] if selected_model is not None else None,
        "candidates": candidates,
    }


def _selected_cif_copy_specs(
    selected_model: tuple[str, Any] | None,
    *,
    analysis_root: Path,
    candidate_lookup: dict[str, RankedPhaseCandidate],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for index, candidate in enumerate(
        _selected_candidates(selected_model, candidate_lookup),
        start=1,
    ):
        if not candidate.cif_path:
            continue
        source = Path(candidate.cif_path)
        if not source.is_file():
            continue
        specs.append(
            {
                "path": analysis_root
                / "selected_cifs"
                / f"{index:02d}_{_safe_filename(candidate.candidate_id)}.cif",
                "writer": "copy",
                "artifact_type": f"selected_cif_copy_{index}",
                "source": str(source),
                "content_type": "chemical/x-cif",
            }
        )
    return specs


def _selected_candidates(
    selected_model: tuple[str, Any] | None,
    candidate_lookup: dict[str, RankedPhaseCandidate],
) -> list[RankedPhaseCandidate]:
    if selected_model is None:
        return []
    model_type, hypothesis = selected_model
    candidate_ids: list[str]
    if model_type == "single_phase":
        candidate_ids = [hypothesis.candidate_id]
    else:
        candidate_ids = [phase.candidate_id for phase in hypothesis.phase_results]
    candidates: list[RankedPhaseCandidate] = []
    for candidate_id in candidate_ids:
        candidate = candidate_lookup.get(candidate_id)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Alias for the shared writer in :mod:`catalog.canonical`.

    Kept as a module-level name because tests patch it to simulate disk
    failures, and because it reads better at the call sites here.

    ``shared=True``: these artifacts are rsynced offsite nightly by an
    unrelated account, and that job fails outright on one unreadable file.
    """
    atomic_write_bytes(path, payload, shared=True)


def _default_environment_notes() -> tuple[str, ...]:
    return (
        f"platform={platform.platform()}",
        f"python_impl={platform.python_implementation()}",
    )


def _media_url(relative_path: str) -> str:
    from django.conf import settings

    base = settings.MEDIA_URL
    if not base.endswith("/"):
        base += "/"
    return base + relative_path.lstrip("/")


def _safe_filename(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in {"-", "_", "."} else "_"
        for char in value
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _guess_content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


__all__ = [
    "CANDIDATE_SIMULATION_METHOD",
    "IDENTITY_FIELDS",
    "INPUT_SCHEMA_VERSION",
    "MODEL_COMPARISON_FORMULAS",
    "PARSING_METHOD",
    "PROVENANCE_ONLY_FIELDS",
    "REFINEMENT_METHOD",
    "XRDAnalysisPersistenceError",
    "build_analysis_identity_payload",
    "build_reproducibility_manifest",
    "persist_xrd_analysis_result",
    "run_and_persist_xrd_analysis",
    "load_persisted_xrd_analysis",
    "compute_analysis_identity",
    "validate_persisted_xrd_analysis",
]
