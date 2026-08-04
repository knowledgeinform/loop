from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

from django.conf import settings
from mongoengine.errors import NotUniqueError

from catalog import raw_db, xrd_store
from catalog.documents import (
    XRDAnalysisJob,
    _utc_now,
    find_embedded_trial,
    get_material,
    get_recipe,
)

from .persistence import (
    XRDAnalysisPersistenceError,
    compute_analysis_identity,
    load_persisted_xrd_analysis,
    persist_xrd_analysis_result,
    validate_persisted_xrd_analysis,
)
from .pipeline import run_xrd_analysis_pipeline
from .schemas import (
    DEFAULT_XRD_ANALYSIS_CONFIG,
    PersistedXRDAnalysis,
    XRDAnalysisConfig,
    XRDAnalysisInput,
    XRDAnalysisInputError,
    XRDAnalysisWarning,
    assemble_xrd_analysis_input,
    to_jsonable,
)


logger = logging.getLogger(__name__)

PROGRESS_STAGES: tuple[str, ...] = (
    "queued",
    "assembling_input",
    "validating_cache",
    "parsing_pattern",
    "quality_control",
    "generating_candidates",
    "refining_single_phase",
    "generating_two_phase_pairs",
    "refining_two_phase",
    "comparing_models",
    "stability_checks",
    "persisting_result",
    "validating_persistence",
    "succeeded",
    "failed",
)

RETRYABLE_FAILURE_CODES = frozenset(
    {
        "analysis_worker_execution_failed",
        "analysis_persistence_failed",
        "analysis_persistence_validation_failed",
        "analysis_job_claim_failed",
    }
)

TERMINAL_FAILURE_CODES = frozenset(
    {
        "xrd_trial_not_found",
        "xrd_raw_file_not_found",
        "xrd_input_assembly_failed",
        "analysis_identity_unavailable",
        "analysis_identity_mismatch",
    }
)

ALLOWED_ARTIFACT_NAMES = frozenset(
    {
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
        "selected_gsas_project.gpx",
    }
)


@dataclass(frozen=True)
class XRDJobContext:
    material: Any
    recipe: Any
    trial: Any
    raw_file: Any | None
    raw_file_path: str
    analysis_input: XRDAnalysisInput


@dataclass(frozen=True)
class XRDAnalysisJobSubmission:
    job: Any
    analysis_id: str
    cache_hit: bool
    status: str
    progress_stage: str
    warnings: tuple[dict[str, Any], ...] = ()
    reused_active_job: bool = False
    requeued_failed_job: bool = False


@dataclass(frozen=True)
class XRDAnalysisJobRunResult:
    job: Any
    final_status: str
    analysis_id: str
    cache_hit: bool = False
    persisted: PersistedXRDAnalysis | None = None
    warnings: tuple[dict[str, Any], ...] = ()
    failure_codes: tuple[str, ...] = ()


class XRDAnalysisJobError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status_code: int = 500,
        diagnostic_metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.diagnostic_metadata = diagnostic_metadata or {}


def xrd_worker_identifier() -> str:
    configured = str(getattr(settings, "XRD_ANALYSIS_WORKER_ID", "") or "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}:{os.getpid()}"


def xrd_poll_seconds() -> float:
    return float(getattr(settings, "XRD_ANALYSIS_POLL_SECONDS", 15.0))


def xrd_claim_lease_seconds() -> int:
    return int(getattr(settings, "XRD_ANALYSIS_CLAIM_LEASE_SECONDS", 300))


def xrd_stale_running_seconds() -> int:
    return int(
        getattr(
            settings,
            "XRD_ANALYSIS_STALE_RUNNING_SECONDS",
            max(xrd_claim_lease_seconds(), 300),
        )
    )


def xrd_max_attempts() -> int:
    return int(getattr(settings, "XRD_ANALYSIS_MAX_ATTEMPTS", 3))


def xrd_heartbeat_seconds() -> float:
    return float(getattr(settings, "XRD_ANALYSIS_HEARTBEAT_SECONDS", 30.0))


def retryable_failure_codes() -> frozenset[str]:
    configured = getattr(settings, "XRD_ANALYSIS_RETRYABLE_FAILURE_CODES", None)
    if configured:
        return frozenset(str(code) for code in configured)
    return RETRYABLE_FAILURE_CODES


def terminal_failure_codes() -> frozenset[str]:
    configured = getattr(settings, "XRD_ANALYSIS_TERMINAL_FAILURE_CODES", None)
    if configured:
        return frozenset(str(code) for code in configured)
    return TERMINAL_FAILURE_CODES


def assemble_repository_xrd_input(
    recipe_auid: str,
    trial_id: str,
    *,
    recipe: Any | None = None,
    trial: Any | None = None,
    require_accessible_raw_file: bool = True,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> XRDJobContext:
    fetched_recipe = None
    recipe_record = recipe or get_recipe(recipe_auid)
    if recipe_record is None:
        raise XRDAnalysisJobError(
            "xrd_trial_not_found",
            f"Recipe {recipe_auid} was not found.",
            retryable=False,
            status_code=404,
        )
    if not getattr(recipe_record, "material_auid", None):
        fetched_recipe = get_recipe(recipe_auid)
        if fetched_recipe is not None:
            recipe_record = fetched_recipe
    trial_record = trial or find_embedded_trial(recipe_record, trial_id)
    if trial_record is None:
        raise XRDAnalysisJobError(
            "xrd_trial_not_found",
            f"Trial {trial_id} was not found in recipe {recipe_auid}.",
            retryable=False,
            status_code=404,
        )
    material = get_material(recipe_record.material_auid)
    if material is None:
        raise XRDAnalysisJobError(
            "xrd_input_assembly_failed",
            f"Material {recipe_record.material_auid} was not found.",
            retryable=False,
        )

    raw_file_path = xrd_store.resolve_raw_path(recipe_record.id, trial_record.trial_id)
    if require_accessible_raw_file and (not raw_file_path or not Path(raw_file_path).is_file()):
        raise XRDAnalysisJobError(
            "xrd_raw_file_not_found",
            f"Raw XRD file for {recipe_record.id}/{trial_record.trial_id} was not found.",
            retryable=False,
            status_code=404,
        )
    if raw_file_path and not Path(raw_file_path).is_file():
        raw_file_path = None

    raw_file_hash = str(
        getattr(trial_record, "file_hash", None)
        or (
            getattr(getattr(trial_record, "exp_condition", None), "additional_params", {}) or {}
        ).get("file_hash")
        or ""
    ).strip()
    raw_file = raw_db.RawFile.objects(id=raw_file_hash).first() if raw_file_hash else None

    try:
        analysis_input = assemble_xrd_analysis_input(
            material=material,
            recipe=recipe_record,
            trial=trial_record,
            raw_file=raw_file,
            raw_file_path=raw_file_path,
            configuration=configuration,
        )
    except XRDAnalysisInputError as exc:
        raise XRDAnalysisJobError(
            "xrd_input_assembly_failed",
            str(exc),
            retryable=False,
        ) from exc

    return XRDJobContext(
        material=material,
        recipe=recipe_record,
        trial=trial_record,
        raw_file=raw_file,
        raw_file_path=raw_file_path,
        analysis_input=analysis_input,
    )


def submit_xrd_analysis_job(
    recipe_auid: str,
    trial_id: str,
    *,
    recipe: Any | None = None,
    trial: Any | None = None,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> XRDAnalysisJobSubmission:
    context = assemble_repository_xrd_input(
        recipe_auid,
        trial_id,
        recipe=recipe,
        trial=trial,
        configuration=configuration,
    )
    try:
        analysis_id, _, reference_snapshot, linked_snapshots, gsasii_version, candidate_simulation_versions = (
            compute_analysis_identity(context.analysis_input, configuration=configuration)
        )
    except Exception as exc:
        raise XRDAnalysisJobError(
            "analysis_identity_unavailable",
            f"Unable to compute an analysis identity: {exc}",
            retryable=False,
        ) from exc

    cache_validation = validate_persisted_xrd_analysis(
        context.analysis_input,
        configuration=configuration,
        analysis_id=analysis_id,
        reference_snapshot=reference_snapshot,
        linked_structure_snapshots=linked_snapshots,
        gsasii_version=gsasii_version,
        candidate_simulation_versions=candidate_simulation_versions,
    )
    if cache_validation.valid:
        persisted = load_persisted_xrd_analysis(
            context.analysis_input,
            configuration=configuration,
            analysis_id=analysis_id,
            cache_validation=cache_validation,
        )
        job = _upsert_job_for_cache_hit(
            analysis_id=analysis_id,
            context=context,
            persisted=persisted,
            configuration=configuration,
        )
        return XRDAnalysisJobSubmission(
            job=job,
            analysis_id=analysis_id,
            cache_hit=True,
            status="succeeded",
            progress_stage="succeeded",
            warnings=_job_warning_payloads(job),
        )

    existing = XRDAnalysisJob.objects(analysis_id=analysis_id).first()
    if existing is not None:
        if existing.status in {"queued", "running"}:
            return XRDAnalysisJobSubmission(
                job=existing,
                analysis_id=analysis_id,
                cache_hit=False,
                status=existing.status,
                progress_stage=str(existing.progress_stage or existing.status),
                warnings=_job_warning_payloads(existing, extra_codes=("analysis_job_already_active",)),
                reused_active_job=True,
            )
        if existing.status == "failed":
            if int(existing.attempt_count or 0) >= int(existing.maximum_attempts or xrd_max_attempts()):
                raise XRDAnalysisJobError(
                    "analysis_job_retry_exhausted",
                    "The analysis job has exhausted its retry budget.",
                    retryable=False,
                    status_code=409,
                )
            existing.status = "queued"
            existing.cache_hit = False
            existing.progress_stage = "queued"
            existing.progress_message = "Queued for retry."
            existing.completed_at = None
            existing.worker_identifier = None
            existing.lease_claimed_at = None
            existing.lease_expires_at = None
            existing.last_heartbeat_at = None
            existing.error_summary = None
            existing.failure_codes = []
            existing.warnings = list(_analysis_input_warnings(context.analysis_input))
            existing.diagnostic_metadata = {}
            existing.raw_file_hash = context.analysis_input.raw_file_hash
            existing.save()
            return XRDAnalysisJobSubmission(
                job=existing,
                analysis_id=analysis_id,
                cache_hit=False,
                status="queued",
                progress_stage="queued",
                warnings=_job_warning_payloads(existing),
                requeued_failed_job=True,
            )
        if existing.status == "succeeded":
            existing.status = "queued"
            existing.cache_hit = False
            existing.progress_stage = "queued"
            existing.progress_message = "Queued because no valid persisted cache was found."
            existing.completed_at = None
            existing.failure_codes = []
            existing.error_summary = None
            existing.worker_identifier = None
            existing.lease_claimed_at = None
            existing.lease_expires_at = None
            existing.last_heartbeat_at = None
            existing.warnings = list(_analysis_input_warnings(context.analysis_input))
            existing.diagnostic_metadata = {}
            existing.save()
            return XRDAnalysisJobSubmission(
                job=existing,
                analysis_id=analysis_id,
                cache_hit=False,
                status="queued",
                progress_stage="queued",
                warnings=_job_warning_payloads(existing),
            )

    return XRDAnalysisJobSubmission(
        job=_create_new_job(analysis_id=analysis_id, context=context, configuration=configuration),
        analysis_id=analysis_id,
        cache_hit=False,
        status="queued",
        progress_stage="queued",
        warnings=tuple(_analysis_input_warnings(context.analysis_input)),
    )


def claim_next_xrd_analysis_job(
    *,
    worker_identifier: str | None = None,
) -> Any | None:
    now = _utc_now()
    worker_id = worker_identifier or xrd_worker_identifier()
    lease_seconds = xrd_claim_lease_seconds()
    stale_cutoff = now - timedelta(seconds=xrd_stale_running_seconds())
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    query = {
        "$or": [
            {"status": "queued"},
            {
                "status": "running",
                "$or": [
                    {"lease_expires_at": {"$lte": now}},
                    {"last_heartbeat_at": {"$lte": stale_cutoff}},
                ],
            },
        ]
    }
    return XRDAnalysisJob.objects(__raw__=query).order_by("queued_at").modify(
        new=True,
        set__status="running",
        set__worker_identifier=worker_id,
        set__lease_claimed_at=now,
        set__lease_expires_at=lease_expires_at,
        set__last_heartbeat_at=now,
        set__started_at=now,
        set__progress_stage="assembling_input",
        set__progress_message="Assembling typed XRD analysis input.",
        unset__completed_at=1,
        inc__attempt_count=1,
    )


def process_one_xrd_analysis_job(
    job: Any | None = None,
    *,
    worker_identifier: str | None = None,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
    scientific_runner: Callable[..., Any] | None = None,
    persistence_runner: Callable[..., PersistedXRDAnalysis] | None = None,
) -> XRDAnalysisJobRunResult | None:
    worker_id = worker_identifier or xrd_worker_identifier()
    claimed_job = job or claim_next_xrd_analysis_job(worker_identifier=worker_id)
    if claimed_job is None:
        return None

    scientific_runner = scientific_runner or run_xrd_analysis_pipeline
    persistence_runner = persistence_runner or persist_xrd_analysis_result
    warnings: list[dict[str, Any]] = list(getattr(claimed_job, "warnings", []) or [])

    def progress(stage: str, message: str) -> None:
        _heartbeat_job(claimed_job, stage=stage, message=message)

    try:
        progress("assembling_input", "Assembling typed XRD analysis input.")
        context = assemble_repository_xrd_input(
            claimed_job.recipe_auid,
            claimed_job.trial_id,
            configuration=configuration,
        )
        try:
            analysis_id, identity_payload, reference_snapshot, linked_snapshots, gsasii_version, candidate_simulation_versions = (
                compute_analysis_identity(context.analysis_input, configuration=configuration)
            )
        except Exception as exc:
            raise XRDAnalysisJobError(
                "analysis_identity_unavailable",
                f"Unable to compute an analysis identity: {exc}",
                retryable=False,
            ) from exc
        if analysis_id != claimed_job.analysis_id:
            raise XRDAnalysisJobError(
                "analysis_identity_mismatch",
                "The recomputed analysis identity does not match the queued job.",
                retryable=False,
                status_code=409,
                diagnostic_metadata={"recomputed_analysis_id": analysis_id},
            )

        progress("validating_cache", "Checking for an existing validated persisted analysis.")
        cache_validation = validate_persisted_xrd_analysis(
            context.analysis_input,
            configuration=configuration,
            analysis_id=analysis_id,
            reference_snapshot=reference_snapshot,
            linked_structure_snapshots=linked_snapshots,
            gsasii_version=gsasii_version,
            candidate_simulation_versions=candidate_simulation_versions,
        )
        if cache_validation.valid:
            persisted = load_persisted_xrd_analysis(
                context.analysis_input,
                configuration=configuration,
                analysis_id=analysis_id,
                cache_validation=cache_validation,
            )
            warnings.extend(_persistence_warning_payloads(persisted))
            _mark_job_succeeded(
                claimed_job,
                persisted=persisted,
                cache_hit=True,
                warnings=warnings,
            )
            return XRDAnalysisJobRunResult(
                job=claimed_job,
                final_status="succeeded",
                analysis_id=analysis_id,
                cache_hit=True,
                persisted=persisted,
                warnings=tuple(warnings),
            )

        result = scientific_runner(
            context.analysis_input,
            configuration=configuration,
            progress_callback=progress,
        )
        progress("persisting_result", "Persisting XRD analysis artifacts.")
        try:
            persisted = persistence_runner(
                context.analysis_input,
                result,
                configuration=configuration,
                analysis_id=analysis_id,
                identity_payload=identity_payload,
                reference_snapshot=reference_snapshot,
                linked_structure_snapshots=linked_snapshots,
                gsasii_version=gsasii_version,
                candidate_simulation_versions=candidate_simulation_versions,
            )
        except XRDAnalysisPersistenceError as exc:
            raise XRDAnalysisJobError(
                exc.code,
                exc.message,
                retryable=exc.code != "analysis_identity_mismatch",
                status_code=409 if exc.code == "analysis_identity_mismatch" else 500,
            ) from exc
        progress("validating_persistence", "Validating persisted XRD analysis artifacts.")
        validation = validate_persisted_xrd_analysis(
            context.analysis_input,
            configuration=configuration,
            analysis_id=analysis_id,
            reference_snapshot=reference_snapshot,
            linked_structure_snapshots=linked_snapshots,
            gsasii_version=gsasii_version,
            candidate_simulation_versions=candidate_simulation_versions,
        )
        if not validation.valid:
            raise XRDAnalysisJobError(
                "analysis_persistence_validation_failed",
                validation.detail or "Persisted XRD analysis failed validation.",
                retryable=True,
                diagnostic_metadata={"validation_failure_code": validation.failure_code},
            )

        warnings.extend(_persistence_warning_payloads(persisted))
        _mark_job_succeeded(
            claimed_job,
            persisted=persisted,
            cache_hit=False,
            warnings=warnings,
        )
        return XRDAnalysisJobRunResult(
            job=claimed_job,
            final_status="succeeded",
            analysis_id=analysis_id,
            cache_hit=False,
            persisted=persisted,
            warnings=tuple(warnings),
        )
    except XRDAnalysisJobError as exc:
        result = _mark_job_failed(claimed_job, exc, warnings=warnings)
        return XRDAnalysisJobRunResult(
            job=claimed_job,
            final_status=result.status,
            analysis_id=str(claimed_job.analysis_id),
            warnings=tuple(result.warnings),
            failure_codes=tuple(result.failure_codes),
        )
    except Exception as exc:  # pragma: no cover - safety net
        result = _mark_job_failed(
            claimed_job,
            XRDAnalysisJobError(
                "analysis_worker_execution_failed",
                f"Unexpected XRD analysis worker error: {exc}",
                retryable=True,
            ),
            warnings=warnings,
        )
        logger.exception("xrd analysis worker failed for job %s", getattr(claimed_job, "id", None))
        return XRDAnalysisJobRunResult(
            job=claimed_job,
            final_status=result.status,
            analysis_id=str(claimed_job.analysis_id),
            warnings=tuple(result.warnings),
            failure_codes=tuple(result.failure_codes),
        )


def process_pending_xrd_analysis_jobs(
    limit: int = 25,
    *,
    worker_identifier: str | None = None,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
    scientific_runner: Callable[..., Any] | None = None,
    persistence_runner: Callable[..., PersistedXRDAnalysis] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {"succeeded": 0, "failed": 0, "queued": 0, "running": 0}
    for _ in range(max(1, limit)):
        result = process_one_xrd_analysis_job(
            None,
            worker_identifier=worker_identifier,
            configuration=configuration,
            scientific_runner=scientific_runner,
            persistence_runner=persistence_runner,
        )
        if result is None:
            break
        counts[result.final_status] = counts.get(result.final_status, 0) + 1
    return counts


def run_xrd_analysis_worker_loop() -> None:
    poll = xrd_poll_seconds()
    print("[catalog] XRD analysis worker started.", flush=True)
    while True:
        try:
            counts = process_pending_xrd_analysis_jobs()
            if any(counts.values()):
                print(f"[catalog] xrd analysis worker processed {counts}", flush=True)
        except Exception as exc:  # pragma: no cover - loop guard
            logger.warning("xrd analysis worker loop error: %s", exc)
        time.sleep(poll)


def allowed_analysis_artifact_names() -> frozenset[str]:
    return ALLOWED_ARTIFACT_NAMES


def normalize_artifact_name(name: str) -> str:
    cleaned = str(name or "").strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or cleaned.startswith(".") or ".." in cleaned:
        raise XRDAnalysisJobError(
            "analysis_result_not_found",
            "Artifact name is not allowed.",
            retryable=False,
            status_code=404,
        )
    return cleaned


def _analysis_input_warnings(analysis_input: XRDAnalysisInput) -> tuple[dict[str, Any], ...]:
    return tuple(_warning_payload(item) for item in (analysis_input.warnings or ()))


def _warning_payload(item: XRDAnalysisWarning | dict[str, Any] | Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    return {
        "code": getattr(item, "code", None),
        "message": getattr(item, "message", None),
        "severity": getattr(item, "severity", "warning"),
        "field": getattr(item, "field", None),
        "stage": getattr(item, "stage", None),
    }


def _warning_from_code(code: str, message: str, *, stage: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "severity": "warning",
        "field": None,
        "stage": stage,
    }


def _job_warning_payloads(job: Any, extra_codes: tuple[str, ...] = ()) -> tuple[dict[str, Any], ...]:
    warnings = [dict(item) for item in (getattr(job, "warnings", []) or [])]
    for code in extra_codes:
        warnings.append(
            _warning_from_code(
                code,
                "An existing XRD analysis job is already active for this analysis identity.",
                stage="job_submission",
            )
        )
    return tuple(warnings)


def _persistence_warning_payloads(persisted: PersistedXRDAnalysis) -> tuple[dict[str, Any], ...]:
    warnings = []
    codes = tuple(getattr(persisted, "persistence_warning_codes", ()) or ())
    for code in codes:
        if code == "selected_gsas_project_unavailable":
            message = "No selected GSAS project artifact was available for this analysis."
        elif code == "upload_archive_backfill_failed":
            message = "The analysis completed, but archive backfill failed."
        elif code == "raw_file_manifest_update_failed":
            message = "The analysis completed, but raw-file manifest registration failed."
        else:
            message = code.replace("_", " ")
        warnings.append(_warning_from_code(code, message, stage="persistence"))
    return tuple(warnings)


def _create_new_job(
    *,
    analysis_id: str,
    context: XRDJobContext,
    configuration: XRDAnalysisConfig,
) -> Any:
    job = XRDAnalysisJob(
        analysis_id=analysis_id,
        material_auid=context.analysis_input.material_auid,
        recipe_auid=context.analysis_input.recipe_auid,
        trial_id=context.analysis_input.trial_id,
        raw_file_hash=context.analysis_input.raw_file_hash,
        algorithm_version=configuration.algorithm_version,
        configuration_version=configuration.configuration_version,
        status="queued",
        cache_hit=False,
        attempt_count=0,
        maximum_attempts=xrd_max_attempts(),
        progress_stage="queued",
        progress_message="Queued for background XRD analysis.",
        warnings=list(_analysis_input_warnings(context.analysis_input)),
    )
    try:
        job.save()
        return job
    except NotUniqueError:
        existing = XRDAnalysisJob.objects(analysis_id=analysis_id).first()
        if existing is None:
            raise
        return existing


def _upsert_job_for_cache_hit(
    *,
    analysis_id: str,
    context: XRDJobContext,
    persisted: PersistedXRDAnalysis,
    configuration: XRDAnalysisConfig,
) -> Any:
    warning_payloads = list(_analysis_input_warnings(context.analysis_input))
    warning_payloads.extend(_persistence_warning_payloads(persisted))
    job = XRDAnalysisJob.objects(analysis_id=analysis_id).first()
    if job is None:
        job = XRDAnalysisJob(
            analysis_id=analysis_id,
            material_auid=context.analysis_input.material_auid,
            recipe_auid=context.analysis_input.recipe_auid,
            trial_id=context.analysis_input.trial_id,
            raw_file_hash=context.analysis_input.raw_file_hash,
            algorithm_version=configuration.algorithm_version,
            configuration_version=configuration.configuration_version,
        )
    job.status = "succeeded"
    job.cache_hit = True
    job.progress_stage = "succeeded"
    job.progress_message = "Validated persisted XRD analysis reused."
    job.completed_at = _utc_now()
    job.result_manifest_relative_path = _summary_result_manifest_path(persisted.summary)
    job.automated_summary = dict(to_jsonable(persisted.summary))
    job.warnings = warning_payloads
    job.failure_codes = []
    job.error_summary = None
    job.diagnostic_metadata = {}
    job.maximum_attempts = int(getattr(job, "maximum_attempts", 0) or xrd_max_attempts())
    try:
        job.save()
    except NotUniqueError:
        job = XRDAnalysisJob.objects(analysis_id=analysis_id).first()
    return job


def _heartbeat_job(job: Any, *, stage: str, message: str) -> None:
    now = _utc_now()
    job.progress_stage = stage
    job.progress_message = message
    job.last_heartbeat_at = now
    job.lease_expires_at = now + timedelta(seconds=xrd_claim_lease_seconds())
    job.save()


def _mark_job_succeeded(
    job: Any,
    *,
    persisted: PersistedXRDAnalysis,
    cache_hit: bool,
    warnings: list[dict[str, Any]],
) -> None:
    job.status = "succeeded"
    job.cache_hit = cache_hit
    job.progress_stage = "succeeded"
    job.progress_message = "XRD analysis completed successfully."
    job.completed_at = _utc_now()
    job.last_heartbeat_at = job.completed_at
    job.result_manifest_relative_path = _summary_result_manifest_path(persisted.summary)
    job.automated_summary = dict(
        to_jsonable(persisted.summary)
    )
    job.warnings = warnings
    job.failure_codes = []
    job.error_summary = None
    job.diagnostic_metadata = {}
    job.save()


def _mark_job_failed(
    job: Any,
    exc: XRDAnalysisJobError,
    *,
    warnings: list[dict[str, Any]],
) -> SimpleNamespace:
    codes = [exc.code]
    status = "failed"
    if exc.retryable and exc.code in retryable_failure_codes():
        if int(job.attempt_count or 0) < int(job.maximum_attempts or xrd_max_attempts()):
            status = "queued"
            job.progress_stage = "queued"
            job.progress_message = "Queued for retry after a retryable failure."
        else:
            codes.append("analysis_job_retry_exhausted")
    if exc.code in terminal_failure_codes():
        status = "failed"
    job.status = status
    job.completed_at = _utc_now() if status == "failed" else None
    job.last_heartbeat_at = _utc_now()
    job.warnings = warnings
    job.failure_codes = codes
    job.error_summary = _sanitize_error_summary(exc.message)
    job.diagnostic_metadata = dict(exc.diagnostic_metadata)
    job.worker_identifier = getattr(job, "worker_identifier", None)
    job.progress_stage = "failed" if status == "failed" else getattr(job, "progress_stage", "queued")
    if status == "failed":
        job.progress_message = "XRD analysis failed."
    job.save()
    return SimpleNamespace(status=status, warnings=job.warnings, failure_codes=tuple(codes))


def _sanitize_error_summary(message: str) -> str:
    text = str(message or "").strip().replace("\n", " ")
    return text[:500]


def _summary_result_manifest_path(summary: Any) -> str:
    if isinstance(summary, dict):
        return str(summary.get("result_manifest_path") or "")
    return str(getattr(summary, "result_manifest_path", "") or "")


__all__ = [
    "ALLOWED_ARTIFACT_NAMES",
    "PROGRESS_STAGES",
    "XRDAnalysisJobError",
    "XRDAnalysisJobRunResult",
    "XRDAnalysisJobSubmission",
    "allowed_analysis_artifact_names",
    "assemble_repository_xrd_input",
    "claim_next_xrd_analysis_job",
    "normalize_artifact_name",
    "process_one_xrd_analysis_job",
    "process_pending_xrd_analysis_jobs",
    "run_xrd_analysis_worker_loop",
    "submit_xrd_analysis_job",
    "xrd_worker_identifier",
]
