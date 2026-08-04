from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

import numpy as np

from catalog.gsas_runtime import (
    cleanup_paths,
    clear_sample_scale_refinement,
    configure_gsas,
    new_project,
    prepare_project_path,
    read_project_bytes,
    resolve_instrument_parameter_file,
    set_project_cycles,
    write_temp_xye,
)
from catalog.numpy_compat import trapezoid

from .reporting import dumps_canonical_json, sha256_digest
from .schemas import (
    AnalysisEvidenceComponents,
    DEFAULT_XRD_ANALYSIS_CONFIG,
    CoordinateType,
    ExpectedReflectionRecord,
    InstrumentProfile,
    PhaseRefinementSummary,
    PhaseSupportRegion,
    ProfileParameterBound,
    RankedPhaseCandidate,
    RefinedLatticeParameters,
    RefinementStageRecord,
    ResidualRegion,
    SinglePhaseHypothesisResult,
    SinglePhaseRefinementBatchResult,
    SinglePhaseRefinementRequest,
    SinglePhaseRefinementSettings,
    TwoPhaseHypothesisResult,
    TwoPhasePairProposal,
    TwoPhaseRefinementBatchResult,
    TwoPhaseRefinementRequest,
    UnsupportedPredictedRegion,
    XRDAnalysisConfig,
    XRDAnalysisInput,
    XRDAnalysisWarning,
)


class SinglePhaseRefinementError(RuntimeError):
    """Raised when the single-phase refinement stage cannot be prepared."""


@dataclass(frozen=True)
class _ResolvedInstrumentProfile:
    instrument_parameter_path: Optional[str]
    remove_after_use: bool
    instrument_profile_serialization: Optional[str]
    warnings: tuple[XRDAnalysisWarning, ...]
    provenance: tuple[str, ...]


def build_single_phase_refinement_request(
    analysis_input: XRDAnalysisInput,
    parsed_pattern: Any,
    candidate: RankedPhaseCandidate,
    *,
    reference_snapshot_hash: Optional[str] = None,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> SinglePhaseRefinementRequest:
    two_theta = tuple(float(value) for value in (parsed_pattern.normalized_two_theta or ()))
    intensities = tuple(float(value) for value in parsed_pattern.normalized_intensities)
    pattern_reference = analysis_input.raw_file_reference.locator if analysis_input.raw_file_reference else None
    pattern_hash = sha256_digest(
        {
            "two_theta": two_theta,
            "intensities": intensities,
            "raw_file_hash": analysis_input.raw_file_hash,
        }
    )
    return SinglePhaseRefinementRequest(
        material_auid=analysis_input.material_auid,
        recipe_auid=analysis_input.recipe_auid,
        trial_id=analysis_input.trial_id,
        raw_file_hash=analysis_input.raw_file_hash,
        observed_two_theta=two_theta,
        observed_intensities=intensities,
        original_coordinate_type=parsed_pattern.original_coordinate_type,
        wavelength_angstrom=analysis_input.wavelength_angstrom or parsed_pattern.wavelength_angstrom,
        instrument_profile=analysis_input.instrument_profile,
        candidate=candidate,
        algorithm_version=analysis_input.algorithm_version,
        configuration_version=analysis_input.configuration_version,
        reference_snapshot_hash=reference_snapshot_hash,
        pattern_reference=pattern_reference,
        pattern_hash=pattern_hash,
        initial_phase_scale_factor=None,
        initial_zero_shift=None,
        provenance=(
            f"pattern_parser={parsed_pattern.parser_type}",
            f"pattern_type={parsed_pattern.pattern_type}",
            f"pattern_source={parsed_pattern.provenance.source_label}",
            f"original_coordinate_type={parsed_pattern.original_coordinate_type}",
        ),
    )


def build_two_phase_refinement_request(
    analysis_input: XRDAnalysisInput,
    parsed_pattern: Any,
    primary_candidate: RankedPhaseCandidate,
    secondary_candidate: RankedPhaseCandidate,
    *,
    source_single_phase_hypothesis_id: str,
    proposal_provenance: tuple[str, ...],
    initial_phase_scale_factors: tuple[Optional[float], Optional[float]] = (None, None),
    initial_zero_shift: Optional[float] = None,
) -> TwoPhaseRefinementRequest:
    two_theta = tuple(float(value) for value in (parsed_pattern.normalized_two_theta or ()))
    intensities = tuple(float(value) for value in parsed_pattern.normalized_intensities)
    pattern_reference = analysis_input.raw_file_reference.locator if analysis_input.raw_file_reference else None
    pattern_hash = sha256_digest(
        {
            "two_theta": two_theta,
            "intensities": intensities,
            "raw_file_hash": analysis_input.raw_file_hash,
            "primary_candidate_hash": primary_candidate.cif_hash,
            "secondary_candidate_hash": secondary_candidate.cif_hash,
        }
    )
    return TwoPhaseRefinementRequest(
        material_auid=analysis_input.material_auid,
        recipe_auid=analysis_input.recipe_auid,
        trial_id=analysis_input.trial_id,
        raw_file_hash=analysis_input.raw_file_hash,
        observed_two_theta=two_theta,
        observed_intensities=intensities,
        wavelength_angstrom=analysis_input.wavelength_angstrom or parsed_pattern.wavelength_angstrom,
        instrument_profile=analysis_input.instrument_profile,
        primary_candidate=primary_candidate,
        secondary_candidate=secondary_candidate,
        algorithm_version=analysis_input.algorithm_version,
        configuration_version=analysis_input.configuration_version,
        source_single_phase_hypothesis_id=source_single_phase_hypothesis_id,
        proposal_provenance=proposal_provenance,
        reference_snapshots=(primary_candidate.source_snapshot, secondary_candidate.source_snapshot),
        pattern_reference=pattern_reference,
        pattern_hash=pattern_hash,
        initial_phase_scale_factors=initial_phase_scale_factors,
        initial_zero_shift=initial_zero_shift,
        provenance=(
            f"pattern_parser={parsed_pattern.parser_type}",
            f"pattern_type={parsed_pattern.pattern_type}",
            f"pattern_source={parsed_pattern.provenance.source_label}",
            f"primary_candidate={primary_candidate.candidate_id}",
            f"secondary_candidate={secondary_candidate.candidate_id}",
        ),
    )


def refine_top_single_phase_candidates(
    analysis_input: XRDAnalysisInput,
    parsed_pattern: Any,
    candidate_generation: Any,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> SinglePhaseRefinementBatchResult:
    settings = configuration.single_phase_refinement
    candidates = tuple(candidate_generation.candidates[: settings.maximum_single_phase_candidates_refined])
    if not candidates:
        warning = _refinement_warning(
            "all_single_phase_refinements_failed",
            "candidate_generation",
            "No ranked single-phase candidates were available for refinement.",
        )
        return SinglePhaseRefinementBatchResult(
            status="single-phase refinement failed",
            warnings=(warning,),
            successful_hypotheses=(),
            failed_candidate_results=(),
            algorithm_version=analysis_input.algorithm_version,
            configuration_version=analysis_input.configuration_version,
            provenance=("single_phase_refinement:no_candidates",),
        )

    successful: list[SinglePhaseHypothesisResult] = []
    failed: list[SinglePhaseHypothesisResult] = []
    warnings: list[XRDAnalysisWarning] = []
    reference_snapshot_hash = getattr(candidate_generation.reference_snapshot, "snapshot_hash", None)

    for candidate in candidates:
        request = build_single_phase_refinement_request(
            analysis_input,
            parsed_pattern,
            candidate,
            reference_snapshot_hash=reference_snapshot_hash,
            configuration=configuration,
        )
        result = refine_single_phase_candidate(request, configuration=configuration)
        warnings.extend(result.warnings)
        if result.refinement_status == "completed":
            successful.append(result)
        else:
            failed.append(result)

    if successful:
        return SinglePhaseRefinementBatchResult(
            status="single-phase-refinement-ready",
            warnings=tuple(_unique_warning_objects(warnings)),
            successful_hypotheses=tuple(successful),
            failed_candidate_results=tuple(failed),
            algorithm_version=analysis_input.algorithm_version,
            configuration_version=analysis_input.configuration_version,
            provenance=(
                f"attempted_candidates={len(candidates)}",
                f"successful_candidates={len(successful)}",
            ),
        )

    failure_warning = _refinement_warning(
        "all_single_phase_refinements_failed",
        "candidate_generation",
        "All attempted single-phase refinements failed.",
    )
    warnings.append(failure_warning)
    return SinglePhaseRefinementBatchResult(
        status="single-phase refinement failed",
        warnings=tuple(_unique_warning_objects(warnings)),
        successful_hypotheses=(),
        failed_candidate_results=tuple(failed),
        algorithm_version=analysis_input.algorithm_version,
        configuration_version=analysis_input.configuration_version,
        provenance=(
            f"attempted_candidates={len(candidates)}",
            "single_phase_refinement:all_failed",
        ),
    )


def refine_single_phase_candidate(
    request: SinglePhaseRefinementRequest,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> SinglePhaseHypothesisResult:
    settings = configuration.single_phase_refinement
    start = time.perf_counter()
    warnings: list[XRDAnalysisWarning] = []
    completed_stages: list[RefinementStageRecord] = []
    best_valid_result: Optional[SinglePhaseHypothesisResult] = None
    failed_stage: Optional[str] = None

    hypothesis_id = sha256_digest(
        {
            "trial_id": request.trial_id,
            "candidate_id": request.candidate.candidate_id,
            "candidate_hash": request.candidate.cif_hash,
            "pattern_hash": request.pattern_hash,
            "configuration_version": request.configuration_version,
        }
    )

    if not request.observed_two_theta:
        warning = _refinement_warning(
            "missing_two_theta_pattern",
            "observed_two_theta",
            "No valid two-theta pattern is available for refinement.",
        )
        return _failure_result(
            request,
            warnings=(warning,),
            failure_codes=("missing_two_theta_pattern",),
            exception_summary=None,
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
        )

    if request.wavelength_angstrom is None:
        warning = _refinement_warning(
            "missing_wavelength_for_refinement",
            "wavelength_angstrom",
            "A physical wavelength is required to refine a single-phase GSAS-II model.",
        )
        return _failure_result(
            request,
            warnings=(warning,),
            failure_codes=("missing_wavelength_for_refinement",),
            exception_summary=None,
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
        )

    candidate_cif_path = Path(request.candidate.cif_path or "")
    if not candidate_cif_path.is_file():
        warning = _refinement_warning(
            "candidate_cif_load_failed",
            "candidate.cif_path",
            f"Candidate CIF is not readable: {request.candidate.cif_path}",
        )
        return _failure_result(
            request,
            warnings=(warning,),
            failure_codes=("candidate_cif_load_failed",),
            exception_summary=None,
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
        )

    resolved_profile = _resolve_instrument_profile(request.instrument_profile, settings)
    warnings.extend(resolved_profile.warnings)
    if resolved_profile.instrument_parameter_path is None:
        failure_codes = ("missing_instrument_profile",) if request.instrument_profile is None else ("invalid_instrument_profile",)
        return _failure_result(
            request,
            warnings=tuple(warnings),
            failure_codes=failure_codes,
            exception_summary=None,
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
        )

    G2sc = None
    try:
        G2sc = configure_gsas()
        from GSASII import GSASIIpath  # type: ignore
    except Exception as exc:
        warning = _refinement_warning(
            "gsas_project_creation_failed",
            "gsas_runtime",
            f"GSAS-II could not be configured: {exc}",
        )
        return _failure_result(
            request,
            warnings=tuple(warnings + [warning]),
            failure_codes=("gsas_project_creation_failed",),
            exception_summary=str(exc),
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
        )

    x = np.asarray(request.observed_two_theta, dtype=float)
    y = np.asarray(request.observed_intensities, dtype=float)
    sigma = _sigma_from_intensity(y)
    xye_path = write_temp_xye(x, y, sigma)
    gpx_path, remove_gpx = prepare_project_path()
    instprm_path = resolved_profile.instrument_parameter_path
    remove_instprm = resolved_profile.remove_after_use

    try:
        try:
            project = new_project(G2sc, gpx_path)
        except Exception as exc:
            warning = _refinement_warning(
                "gsas_project_creation_failed",
                "gpx_path",
                f"GSAS-II project creation failed: {exc}",
            )
            return _failure_result(
                request,
                warnings=tuple(warnings + [warning]),
                failure_codes=("gsas_project_creation_failed",),
                exception_summary=str(exc),
                failed_stage=None,
                runtime_seconds=time.perf_counter() - start,
            )

        try:
            histogram = project.add_powder_histogram(xye_path, iparams=instprm_path, fmthint="xye")
        except Exception as exc:
            warning = _refinement_warning(
                "histogram_load_failed",
                "observed_two_theta",
                f"GSAS-II could not load the measured powder histogram: {exc}",
            )
            return _failure_result(
                request,
                warnings=tuple(warnings + [warning]),
                failure_codes=("histogram_load_failed",),
                exception_summary=str(exc),
                failed_stage=None,
                runtime_seconds=time.perf_counter() - start,
            )

        _set_histogram_wavelength(histogram, request.wavelength_angstrom)
        clear_sample_scale_refinement(histogram)

        try:
            phase_obj = project.add_phase(
                str(candidate_cif_path.resolve()),
                phasename=request.candidate.candidate_id,
                histograms=[histogram],
                fmthint="CIF",
            )
        except Exception as exc:
            warning = _refinement_warning(
                "phase_load_failed",
                "candidate.cif_path",
                f"GSAS-II could not load the phase CIF: {exc}",
            )
            return _failure_result(
                request,
                warnings=tuple(warnings + [warning]),
                failure_codes=("phase_load_failed",),
                exception_summary=str(exc),
                failed_stage=None,
                runtime_seconds=time.perf_counter() - start,
            )

        _seed_initial_sample_parameter(histogram, "Shift", request.initial_zero_shift)
        _seed_initial_phase_scale(phase_obj, histogram, request.initial_phase_scale_factor)
        initial_lattice = _extract_lattice_parameters(phase_obj)
        stage_plan = _build_stage_plan(settings, request.instrument_profile)
        for stage_name in stage_plan:
            stage_start = time.perf_counter()
            try:
                _apply_stage(stage_name, settings, project, histogram, phase_obj)
                snapshot = _extract_result_snapshot(
                    request,
                    project,
                    histogram,
                    phase_obj,
                    settings=settings,
                    gsasii_version=_gsas_version(GSASIIpath),
                    warnings=tuple(_unique_warning_objects(warnings)),
                    completed_stages=tuple(completed_stages),
                    failed_stage=None,
                    runtime_seconds=time.perf_counter() - start,
                    instrument_profile_serialization=resolved_profile.instrument_profile_serialization,
                    provenance=resolved_profile.provenance + request.provenance,
                )
                _validate_snapshot(snapshot, initial_lattice, settings)
                completed_stages.append(
                    RefinementStageRecord(
                        stage_name=stage_name,
                        status="completed",
                        runtime_seconds=time.perf_counter() - stage_start,
                    )
                )
                best_valid_result = replace(snapshot, completed_stages=tuple(completed_stages))
            except Exception as exc:
                failed_stage = stage_name
                failure_codes = _failure_codes_for_exception(exc)
                completed_stages.append(
                    RefinementStageRecord(
                        stage_name=stage_name,
                        status="failed",
                        runtime_seconds=time.perf_counter() - stage_start,
                        failure_codes=failure_codes,
                        notes=(str(exc),),
                    )
                )
                warning = _refinement_warning(
                    failure_codes[0],
                    stage_name,
                    f"Refinement stage '{stage_name}' failed: {exc}",
                )
                warnings.append(warning)
                if best_valid_result is not None:
                    return replace(
                        best_valid_result,
                        refinement_status="partial",
                        convergence_status="partial",
                        completed_stages=tuple(completed_stages),
                        failed_stage=failed_stage,
                        runtime_seconds=time.perf_counter() - start,
                        warnings=tuple(_unique_warning_objects(list(best_valid_result.warnings) + warnings)),
                        failure_codes=failure_codes,
                        exception_summary=str(exc),
                    )
                return _failure_result(
                    request,
                    warnings=tuple(_unique_warning_objects(warnings)),
                    failure_codes=failure_codes,
                    exception_summary=str(exc),
                    failed_stage=failed_stage,
                    runtime_seconds=time.perf_counter() - start,
                    completed_stages=tuple(completed_stages),
                    instrument_profile_serialization=resolved_profile.instrument_profile_serialization,
                    provenance=resolved_profile.provenance + request.provenance,
                    gsasii_version=_gsas_version(GSASIIpath),
                )

        if best_valid_result is None:
            warning = _refinement_warning(
                "candidate_refinement_failed",
                "candidate_id",
                "No valid refinement stage completed for this candidate.",
            )
            return _failure_result(
                request,
                warnings=tuple(_unique_warning_objects(warnings + [warning])),
                failure_codes=("candidate_refinement_failed",),
                exception_summary=None,
                failed_stage=failed_stage,
                runtime_seconds=time.perf_counter() - start,
                completed_stages=tuple(completed_stages),
                instrument_profile_serialization=resolved_profile.instrument_profile_serialization,
                provenance=resolved_profile.provenance + request.provenance,
                gsasii_version=_gsas_version(GSASIIpath),
            )

        return replace(
            best_valid_result,
            refinement_status="completed",
            convergence_status="converged",
            completed_stages=tuple(completed_stages),
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
            warnings=tuple(_unique_warning_objects(list(best_valid_result.warnings) + warnings)),
        )
    finally:
        cleanup_paths(
            (xye_path, True),
            (instprm_path, remove_instprm),
            (gpx_path, remove_gpx),
        )


def _resolve_instrument_profile(
    profile: Optional[InstrumentProfile],
    settings: SinglePhaseRefinementSettings,
) -> _ResolvedInstrumentProfile:
    if profile is None:
        if not settings.allow_generic_instrument_fallback:
            return _ResolvedInstrumentProfile(None, False, None, (), ())
        instprm_path, remove_after_use = resolve_instrument_parameter_file(
            instrument_label=settings.generic_instrument_label,
        )
        warning = _refinement_warning(
            "missing_instrument_profile",
            "instrument_profile",
            "Instrument profile is missing; using the configured generic fallback profile.",
        )
        return _ResolvedInstrumentProfile(
            instprm_path,
            remove_after_use,
            None,
            (warning,),
            (f"generic_instrument_profile={settings.generic_instrument_label}",),
        )

    serialization = dumps_canonical_json(profile)
    if profile.instrument_parameter_path:
        path = str(Path(profile.instrument_parameter_path).expanduser().resolve())
        if Path(path).is_file():
            return _ResolvedInstrumentProfile(
                path,
                False,
                serialization,
                (),
                ("instrument_profile_source=explicit_path",),
            )
        warning = _refinement_warning(
            "invalid_instrument_profile",
            "instrument_profile.instrument_parameter_path",
            f"Instrument parameter path is not readable: {profile.instrument_parameter_path}",
        )
        return _ResolvedInstrumentProfile(None, False, serialization, (warning,), ())

    if settings.allow_generic_instrument_fallback:
        label = profile.instrument_label or settings.generic_instrument_label
        instprm_path, remove_after_use = resolve_instrument_parameter_file(instrument_label=label)
        warning = _refinement_warning(
            "invalid_instrument_profile",
            "instrument_profile",
            f"Instrument profile is incomplete; using generic fallback profile '{label}'.",
        )
        return _ResolvedInstrumentProfile(
            instprm_path,
            remove_after_use,
            serialization,
            (warning,),
            (f"generic_instrument_profile={label}",),
        )

    warning = _refinement_warning(
        "invalid_instrument_profile",
        "instrument_profile",
        "Instrument profile is present but does not include a usable instrument parameter path.",
    )
    return _ResolvedInstrumentProfile(None, False, serialization, (warning,), ())


def _build_stage_plan(
    settings: SinglePhaseRefinementSettings,
    instrument_profile: Optional[InstrumentProfile],
) -> tuple[str, ...]:
    stages = ["background_scale"]
    if settings.refine_zero_shift:
        stages.append("zero_shift")
    if settings.refine_sample_displacement:
        stages.append("sample_displacement")
    if settings.refine_lattice_parameters:
        stages.append("lattice")
    if settings.allowed_profile_terms:
        stages.append("profile_terms")
    if settings.refine_crystallite_size:
        stages.append("crystallite_size")
    if settings.refine_microstrain:
        stages.append("microstrain")
    return tuple(stages)


def _apply_stage(stage_name: str, settings: SinglePhaseRefinementSettings, project: Any, histogram: Any, phase_obj: Any) -> None:
    if stage_name == "background_scale":
        histogram.set_refinements(
            {
                "Limits": [float(np.min(histogram.getdata("X"))), float(np.max(histogram.getdata("X")))],
                "Background": {
                    "type": settings.background_model_type,
                    "no. coeffs": int(settings.background_coefficient_count),
                    "refine": True,
                },
            }
        )
        clear_sample_scale_refinement(histogram)
        if settings.refine_scale:
            phase_obj.set_HAP_refinements({"Scale": True}, [histogram])
        set_project_cycles(project, settings.initial_stage_cycles)
        project.refine(makeBack=True)
        return
    if stage_name == "zero_shift":
        histogram.set_refinements({"Sample Parameters": ["Shift"]})
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    if stage_name == "sample_displacement":
        histogram.set_refinements({"Sample Parameters": ["DisplaceX"]})
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    if stage_name == "lattice":
        phase_obj.set_refinements({"Cell": True})
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    if stage_name == "profile_terms":
        histogram.set_refinements({"Instrument Parameters": list(settings.allowed_profile_terms)})
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    if stage_name == "crystallite_size":
        phase_obj.set_HAP_refinements({"Size": {"type": "isotropic", "refine": True}}, [histogram])
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    if stage_name == "microstrain":
        phase_obj.set_HAP_refinements({"Mustrain": {"type": "isotropic", "refine": True}}, [histogram])
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    raise SinglePhaseRefinementError(f"Unknown refinement stage: {stage_name}")


def refine_candidate_pair(
    request: TwoPhaseRefinementRequest,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> TwoPhaseHypothesisResult:
    settings = configuration.single_phase_refinement
    decision_settings = configuration.decision
    start = time.perf_counter()
    warnings: list[XRDAnalysisWarning] = []
    completed_stages: list[RefinementStageRecord] = []
    best_valid_result: Optional[TwoPhaseHypothesisResult] = None
    failed_stage: Optional[str] = None

    if not request.observed_two_theta:
        warning = _refinement_warning(
            "missing_two_theta_pattern",
            "observed_two_theta",
            "No valid two-theta pattern is available for two-phase refinement.",
            stage="two_phase_refinement",
        )
        return _two_phase_failure_result(
            request,
            warnings=(warning,),
            failure_codes=("missing_two_theta_pattern",),
            exception_summary=None,
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
        )
    if request.wavelength_angstrom is None:
        warning = _refinement_warning(
            "missing_wavelength_for_refinement",
            "wavelength_angstrom",
            "A physical wavelength is required to refine a two-phase GSAS-II model.",
            stage="two_phase_refinement",
        )
        return _two_phase_failure_result(
            request,
            warnings=(warning,),
            failure_codes=("missing_wavelength_for_refinement",),
            exception_summary=None,
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
        )

    candidate_paths = [
        Path(request.primary_candidate.cif_path or ""),
        Path(request.secondary_candidate.cif_path or ""),
    ]
    for index, candidate_path in enumerate(candidate_paths):
        if not candidate_path.is_file():
            warning = _refinement_warning(
                "candidate_cif_load_failed",
                f"candidate_{index}.cif_path",
                f"Candidate CIF is not readable: {candidate_path}",
                stage="two_phase_refinement",
            )
            return _two_phase_failure_result(
                request,
                warnings=(warning,),
                failure_codes=("candidate_cif_load_failed",),
                exception_summary=None,
                failed_stage=None,
                runtime_seconds=time.perf_counter() - start,
            )

    resolved_profile = _resolve_instrument_profile(request.instrument_profile, settings)
    warnings.extend(resolved_profile.warnings)
    if resolved_profile.instrument_parameter_path is None:
        failure_codes = ("missing_instrument_profile",) if request.instrument_profile is None else ("invalid_instrument_profile",)
        return _two_phase_failure_result(
            request,
            warnings=tuple(warnings),
            failure_codes=failure_codes,
            exception_summary=None,
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
        )

    try:
        G2sc = configure_gsas()
        from GSASII import GSASIIpath  # type: ignore
    except Exception as exc:
        warning = _refinement_warning(
            "gsas_project_creation_failed",
            "gsas_runtime",
            f"GSAS-II could not be configured: {exc}",
            stage="two_phase_refinement",
        )
        return _two_phase_failure_result(
            request,
            warnings=tuple(warnings + [warning]),
            failure_codes=("gsas_project_creation_failed",),
            exception_summary=str(exc),
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
        )

    x = np.asarray(request.observed_two_theta, dtype=float)
    y = np.asarray(request.observed_intensities, dtype=float)
    sigma = _sigma_from_intensity(y)
    xye_path = write_temp_xye(x, y, sigma)
    gpx_path, remove_gpx = prepare_project_path()
    instprm_path = resolved_profile.instrument_parameter_path
    remove_instprm = resolved_profile.remove_after_use

    try:
        try:
            project = new_project(G2sc, gpx_path)
            histogram = project.add_powder_histogram(xye_path, iparams=instprm_path, fmthint="xye")
        except Exception as exc:
            warning = _refinement_warning(
                "histogram_load_failed",
                "observed_two_theta",
                f"GSAS-II could not load the measured histogram: {exc}",
                stage="two_phase_refinement",
            )
            return _two_phase_failure_result(
                request,
                warnings=tuple(warnings + [warning]),
                failure_codes=("histogram_load_failed",),
                exception_summary=str(exc),
                failed_stage=None,
                runtime_seconds=time.perf_counter() - start,
            )

        _set_histogram_wavelength(histogram, request.wavelength_angstrom)
        clear_sample_scale_refinement(histogram)

        try:
            primary_phase = project.add_phase(
                str(candidate_paths[0].resolve()),
                phasename=request.primary_candidate.candidate_id,
                histograms=[histogram],
                fmthint="CIF",
            )
            secondary_phase = project.add_phase(
                str(candidate_paths[1].resolve()),
                phasename=request.secondary_candidate.candidate_id,
                histograms=[histogram],
                fmthint="CIF",
            )
        except Exception as exc:
            warning = _refinement_warning(
                "phase_load_failed",
                "candidate_pair",
                f"GSAS-II could not load the two-phase CIF pair: {exc}",
                stage="two_phase_refinement",
            )
            return _two_phase_failure_result(
                request,
                warnings=tuple(warnings + [warning]),
                failure_codes=("phase_load_failed",),
                exception_summary=str(exc),
                failed_stage=None,
                runtime_seconds=time.perf_counter() - start,
            )

        _seed_initial_sample_parameter(histogram, "Shift", request.initial_zero_shift)
        _seed_initial_phase_scale(primary_phase, histogram, request.initial_phase_scale_factors[0])
        _seed_initial_phase_scale(secondary_phase, histogram, request.initial_phase_scale_factors[1])

        initial_lattices = (
            _extract_lattice_parameters(primary_phase),
            _extract_lattice_parameters(secondary_phase),
        )
        stage_plan = _build_stage_plan(settings, request.instrument_profile)
        for stage_name in stage_plan:
            stage_start = time.perf_counter()
            try:
                _apply_two_phase_stage(stage_name, settings, project, histogram, (primary_phase, secondary_phase))
                snapshot = _extract_two_phase_result_snapshot(
                    request,
                    project,
                    histogram,
                    (primary_phase, secondary_phase),
                    settings=settings,
                    gsasii_version=_gsas_version(GSASIIpath),
                    warnings=tuple(_unique_warning_objects(warnings)),
                    completed_stages=tuple(completed_stages),
                    failed_stage=None,
                    runtime_seconds=time.perf_counter() - start,
                    instrument_profile_serialization=resolved_profile.instrument_profile_serialization,
                    provenance=resolved_profile.provenance + request.provenance,
                )
                _validate_two_phase_snapshot(snapshot, initial_lattices, settings, decision_settings)
                completed_stages.append(
                    RefinementStageRecord(
                        stage_name=stage_name,
                        status="completed",
                        runtime_seconds=time.perf_counter() - stage_start,
                    )
                )
                best_valid_result = replace(snapshot, completed_stages=tuple(completed_stages))
            except Exception as exc:
                failed_stage = stage_name
                failure_codes = _failure_codes_for_exception(exc)
                completed_stages.append(
                    RefinementStageRecord(
                        stage_name=stage_name,
                        status="failed",
                        runtime_seconds=time.perf_counter() - stage_start,
                        failure_codes=failure_codes,
                        notes=(str(exc),),
                    )
                )
                warning = _refinement_warning(
                    failure_codes[0],
                    stage_name,
                    f"Two-phase refinement stage '{stage_name}' failed: {exc}",
                    stage="two_phase_refinement",
                )
                warnings.append(warning)
                if best_valid_result is not None:
                    return replace(
                        best_valid_result,
                        refinement_status="partial",
                        convergence_status="partial",
                        completed_stages=tuple(completed_stages),
                        failed_stage=failed_stage,
                        runtime_seconds=time.perf_counter() - start,
                        warnings=tuple(_unique_warning_objects(list(best_valid_result.warnings) + warnings)),
                        failure_codes=failure_codes,
                        exception_summary=str(exc),
                    )
                return _two_phase_failure_result(
                    request,
                    warnings=tuple(_unique_warning_objects(warnings)),
                    failure_codes=failure_codes,
                    exception_summary=str(exc),
                    failed_stage=failed_stage,
                    runtime_seconds=time.perf_counter() - start,
                    completed_stages=tuple(completed_stages),
                    instrument_profile_serialization=resolved_profile.instrument_profile_serialization,
                    provenance=resolved_profile.provenance + request.provenance,
                    gsasii_version=_gsas_version(GSASIIpath),
                )

        if best_valid_result is None:
            warning = _refinement_warning(
                "two_phase_refinement_failed",
                "candidate_pair",
                "No valid two-phase refinement stage completed for this candidate pair.",
                stage="two_phase_refinement",
            )
            return _two_phase_failure_result(
                request,
                warnings=tuple(_unique_warning_objects(warnings + [warning])),
                failure_codes=("two_phase_refinement_failed",),
                exception_summary=None,
                failed_stage=failed_stage,
                runtime_seconds=time.perf_counter() - start,
                completed_stages=tuple(completed_stages),
                instrument_profile_serialization=resolved_profile.instrument_profile_serialization,
                provenance=resolved_profile.provenance + request.provenance,
                gsasii_version=_gsas_version(GSASIIpath),
            )

        return replace(
            best_valid_result,
            refinement_status="completed",
            convergence_status="converged",
            completed_stages=tuple(completed_stages),
            failed_stage=None,
            runtime_seconds=time.perf_counter() - start,
            warnings=tuple(_unique_warning_objects(list(best_valid_result.warnings) + warnings)),
        )
    finally:
        cleanup_paths(
            (xye_path, True),
            (instprm_path, remove_instprm),
            (gpx_path, remove_gpx),
        )


def refine_candidate_pairs(
    requests: tuple[TwoPhaseRefinementRequest, ...],
    *,
    pair_proposals: tuple[TwoPhasePairProposal, ...],
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> TwoPhaseRefinementBatchResult:
    warnings: list[XRDAnalysisWarning] = []
    successful: list[TwoPhaseHypothesisResult] = []
    failed: list[TwoPhaseHypothesisResult] = []
    for request in requests:
        result = refine_candidate_pair(request, configuration=configuration)
        warnings.extend(result.warnings)
        if result.refinement_status == "completed":
            successful.append(result)
        else:
            failed.append(result)
    if successful:
        status: str = "two-phase-hypotheses-ready"
    else:
        status = "two-phase refinement failed"
        warnings.append(
            _refinement_warning(
                "all_two_phase_refinements_failed",
                "candidate_pair",
                "All attempted two-phase refinements failed.",
                stage="two_phase_refinement",
            )
        )
    return TwoPhaseRefinementBatchResult(
        status=status,  # type: ignore[arg-type]
        warnings=tuple(_unique_warning_objects(warnings)),
        pair_proposals=pair_proposals,
        successful_hypotheses=tuple(successful),
        failed_hypotheses=tuple(failed),
        algorithm_version=configuration.algorithm_version,
        configuration_version=configuration.configuration_version,
        provenance=(f"attempted_pairs={len(requests)}", f"successful_pairs={len(successful)}"),
    )


def _apply_two_phase_stage(
    stage_name: str,
    settings: SinglePhaseRefinementSettings,
    project: Any,
    histogram: Any,
    phase_objects: tuple[Any, Any],
) -> None:
    if stage_name == "background_scale":
        histogram.set_refinements(
            {
                "Limits": [float(np.min(histogram.getdata("X"))), float(np.max(histogram.getdata("X")))],
                "Background": {
                    "type": settings.background_model_type,
                    "no. coeffs": int(settings.background_coefficient_count),
                    "refine": True,
                },
            }
        )
        clear_sample_scale_refinement(histogram)
        if settings.refine_scale:
            for phase_obj in phase_objects:
                phase_obj.set_HAP_refinements({"Scale": True}, [histogram])
        set_project_cycles(project, settings.initial_stage_cycles)
        project.refine(makeBack=True)
        return
    if stage_name == "zero_shift":
        histogram.set_refinements({"Sample Parameters": ["Shift"]})
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    if stage_name == "sample_displacement":
        histogram.set_refinements({"Sample Parameters": ["DisplaceX"]})
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    if stage_name == "lattice":
        for phase_obj in phase_objects:
            phase_obj.set_refinements({"Cell": True})
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    if stage_name == "profile_terms":
        histogram.set_refinements({"Instrument Parameters": list(settings.allowed_profile_terms)})
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    if stage_name == "crystallite_size":
        for phase_obj in phase_objects:
            phase_obj.set_HAP_refinements({"Size": {"type": "isotropic", "refine": True}}, [histogram])
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    if stage_name == "microstrain":
        for phase_obj in phase_objects:
            phase_obj.set_HAP_refinements({"Mustrain": {"type": "isotropic", "refine": True}}, [histogram])
        set_project_cycles(project, settings.maximum_iterations)
        project.refine(makeBack=True)
        return
    raise SinglePhaseRefinementError(f"Unknown refinement stage: {stage_name}")


def _extract_result_snapshot(
    request: SinglePhaseRefinementRequest,
    project: Any,
    histogram: Any,
    phase_obj: Any,
    *,
    settings: SinglePhaseRefinementSettings,
    gsasii_version: Optional[str],
    warnings: tuple[XRDAnalysisWarning, ...],
    completed_stages: tuple[RefinementStageRecord, ...],
    failed_stage: Optional[str],
    runtime_seconds: float,
    instrument_profile_serialization: Optional[str],
    provenance: tuple[str, ...],
) -> SinglePhaseHypothesisResult:
    observed_two_theta = tuple(float(value) for value in histogram.getdata("X"))
    observed_intensities = tuple(float(value) for value in histogram.getdata("Yobs"))
    calculated_total = tuple(float(value) for value in histogram.getdata("Ycalc"))
    calculated_background = tuple(float(value) for value in histogram.getdata("Background"))
    difference_pattern = tuple(float(value) for value in histogram.getdata("Residual"))
    if not calculated_total or len(calculated_total) != len(observed_two_theta):
        raise SinglePhaseRefinementError("calculated pattern is missing or invalid")

    reflections = tuple(_extract_expected_reflections(histogram, request.candidate.candidate_id))
    residuals = histogram.residuals if isinstance(histogram.residuals, dict) else {}
    raw_covariance = project.data.get("Covariance", {}) if hasattr(project, "data") else {}
    covariance_data = raw_covariance.get("data", {}) if isinstance(raw_covariance, dict) else {}
    refined_parameter_count = len(project.get_VaryList()) if hasattr(project, "get_VaryList") else 0
    observation_count = len(observed_two_theta)
    degrees_of_freedom = observation_count - refined_parameter_count if observation_count >= refined_parameter_count else None
    scale_factor = _extract_scale_factor(phase_obj, histogram)
    lattice = _extract_lattice_parameters(phase_obj)
    zero_shift = _sample_parameter_value(histogram, "Shift")
    sample_displacement = _sample_parameter_value(histogram, "DisplaceX")
    profile_terms = _extract_profile_terms(histogram, settings.allowed_profile_terms)
    crystallite_size = _extract_hap_scalar(phase_obj, histogram, "Size")
    microstrain = _extract_hap_scalar(phase_obj, histogram, "Mustrain")
    positive_regions = tuple(
        _extract_positive_residual_regions(
            observed_two_theta,
            difference_pattern,
            observed_intensities,
            reflections,
            settings=settings,
        )
    )
    unsupported_regions = tuple(
        _extract_unsupported_predicted_regions(
            observed_two_theta,
            observed_intensities,
            reflections,
            settings=settings,
        )
    )
    raw_metrics = {
        "residuals": residuals,
        "covariance_data": covariance_data,
        "project_size_bytes": len(read_project_bytes(project.filename)) if getattr(project, "filename", None) else 0,
    }
    w_r = _safe_float(residuals.get("wR"))
    goodness_of_fit = _safe_float(covariance_data.get("GOF"))
    if goodness_of_fit is None:
        w_r_min = _safe_float(residuals.get("wRmin"))
        if w_r is not None and w_r_min not in (None, 0.0):
            goodness_of_fit = w_r / w_r_min
    return SinglePhaseHypothesisResult(
        hypothesis_id=sha256_digest(
            {
                "candidate_id": request.candidate.candidate_id,
                "pattern_hash": request.pattern_hash,
                "candidate_hash": request.candidate.cif_hash,
                "configuration_version": request.configuration_version,
            }
        ),
        candidate_id=request.candidate.candidate_id,
        candidate_source=request.candidate.source,
        candidate_source_identifier=request.candidate.source_identifier,
        candidate_cif_hash=request.candidate.cif_hash,
        reference_snapshot=request.reference_snapshot_hash,
        algorithm_version=request.algorithm_version,
        configuration_version=request.configuration_version,
        gsasii_version=gsasii_version,
        instrument_profile_serialization=instrument_profile_serialization,
        screening_pre_rank_score=float(request.candidate.combined_pre_rank_score),
        refinement_status="partial",
        convergence_status="partial",
        completed_stages=completed_stages,
        failed_stage=failed_stage,
        runtime_seconds=runtime_seconds,
        warnings=warnings,
        failure_codes=(),
        exception_summary=None,
        rwp=w_r,
        rp=_safe_float(residuals.get("R")),
        goodness_of_fit=goodness_of_fit,
        chi_squared=_safe_float(covariance_data.get("chisq")),
        weighted_residual=_safe_float(residuals.get("wRb")),
        observation_count=observation_count,
        refined_parameter_count=refined_parameter_count,
        degrees_of_freedom=degrees_of_freedom,
        phase_scale_factor=scale_factor,
        refined_lattice_parameters=lattice,
        zero_shift=zero_shift,
        sample_displacement=sample_displacement,
        refined_profile_terms=profile_terms,
        crystallite_size=crystallite_size,
        microstrain=microstrain,
        observed_two_theta=observed_two_theta,
        observed_intensities=observed_intensities,
        calculated_total_pattern=calculated_total,
        calculated_background=calculated_background,
        difference_pattern=difference_pattern,
        expected_reflections=reflections,
        significant_positive_residual_regions=positive_regions,
        unsupported_strong_predicted_regions=unsupported_regions,
        raw_residual_metrics=raw_metrics,
        provenance=provenance + (
            f"pattern_hash={request.pattern_hash}",
            f"candidate_hash={request.candidate.cif_hash}",
            f"stage_order={','.join(stage.stage_name for stage in completed_stages)}",
            f"parameter_bounds={dumps_canonical_json(settings.profile_parameter_bounds)}",
        ),
    )


def _extract_two_phase_result_snapshot(
    request: TwoPhaseRefinementRequest,
    project: Any,
    histogram: Any,
    phase_objects: tuple[Any, Any],
    *,
    settings: SinglePhaseRefinementSettings,
    gsasii_version: Optional[str],
    warnings: tuple[XRDAnalysisWarning, ...],
    completed_stages: tuple[RefinementStageRecord, ...],
    failed_stage: Optional[str],
    runtime_seconds: float,
    instrument_profile_serialization: Optional[str],
    provenance: tuple[str, ...],
) -> TwoPhaseHypothesisResult:
    observed_two_theta = tuple(float(value) for value in histogram.getdata("X"))
    observed_intensities = tuple(float(value) for value in histogram.getdata("Yobs"))
    calculated_total = tuple(float(value) for value in histogram.getdata("Ycalc"))
    calculated_background = tuple(float(value) for value in histogram.getdata("Background"))
    difference_pattern = tuple(float(value) for value in histogram.getdata("Residual"))
    if not calculated_total or len(calculated_total) != len(observed_two_theta):
        raise SinglePhaseRefinementError("calculated pattern is missing or invalid")

    residuals = histogram.residuals if isinstance(histogram.residuals, dict) else {}
    raw_covariance = project.data.get("Covariance", {}) if hasattr(project, "data") else {}
    covariance_data = raw_covariance.get("data", {}) if isinstance(raw_covariance, dict) else {}
    refined_parameter_count = len(project.get_VaryList()) if hasattr(project, "get_VaryList") else 0
    observation_count = len(observed_two_theta)
    degrees_of_freedom = observation_count - refined_parameter_count if observation_count >= refined_parameter_count else None
    zero_shift = _sample_parameter_value(histogram, "Shift")
    sample_displacement = _sample_parameter_value(histogram, "DisplaceX")
    profile_terms = _extract_profile_terms(histogram, settings.allowed_profile_terms)

    phase_summaries: list[PhaseRefinementSummary] = []
    combined_reflections: list[ExpectedReflectionRecord] = []
    combined_unsupported: list[UnsupportedPredictedRegion] = []
    for phase_obj, candidate in zip(phase_objects, (request.primary_candidate, request.secondary_candidate)):
        reflections = tuple(_extract_expected_reflections(histogram, candidate.candidate_id))
        unsupported = tuple(
            _extract_unsupported_predicted_regions(
                observed_two_theta,
                observed_intensities,
                reflections,
                settings=settings,
            )
        )
        phase_summaries.append(
            PhaseRefinementSummary(
                candidate_id=candidate.candidate_id,
                candidate_source=candidate.source,
                candidate_source_identifier=candidate.source_identifier,
                candidate_cif_hash=candidate.cif_hash,
                scale_factor=_extract_scale_factor(phase_obj, histogram),
                refined_lattice_parameters=_extract_lattice_parameters(phase_obj),
                expected_reflections=reflections,
                unsupported_predicted_regions=unsupported,
            )
        )
        combined_reflections.extend(reflections)
        combined_unsupported.extend(unsupported)

    all_reflections = tuple(sorted(combined_reflections, key=lambda reflection: reflection.two_theta))
    positive_regions = tuple(
        _extract_positive_residual_regions(
            observed_two_theta,
            difference_pattern,
            observed_intensities,
            all_reflections,
            settings=settings,
        )
    )
    shared_regions, primary_regions = _extract_phase_support_regions(
        observed_two_theta,
        observed_intensities,
        tuple(phase_summaries),
        settings=settings,
    )
    weighted_residual_sum = _weighted_residual_sum(observed_intensities, calculated_total)
    raw_metrics = {
        "residuals": residuals,
        "covariance_data": covariance_data,
        "project_size_bytes": len(read_project_bytes(project.filename)) if getattr(project, "filename", None) else 0,
        "weighted_residual_sum": weighted_residual_sum,
    }
    w_r = _safe_float(residuals.get("wR"))
    goodness_of_fit = _safe_float(covariance_data.get("GOF"))
    if goodness_of_fit is None:
        w_r_min = _safe_float(residuals.get("wRmin"))
        if w_r is not None and w_r_min not in (None, 0.0):
            goodness_of_fit = w_r / w_r_min
    return TwoPhaseHypothesisResult(
        hypothesis_id=sha256_digest(
            {
                "candidate_ids": sorted((request.primary_candidate.candidate_id, request.secondary_candidate.candidate_id)),
                "pattern_hash": request.pattern_hash,
                "configuration_version": request.configuration_version,
                "source_single_phase_hypothesis_id": request.source_single_phase_hypothesis_id,
            }
        ),
        candidate_ids=(request.primary_candidate.candidate_id, request.secondary_candidate.candidate_id),
        candidate_sources=(request.primary_candidate.source, request.secondary_candidate.source),
        candidate_source_identifiers=(request.primary_candidate.source_identifier, request.secondary_candidate.source_identifier),
        candidate_cif_hashes=(request.primary_candidate.cif_hash, request.secondary_candidate.cif_hash),
        reference_snapshots=request.reference_snapshots,
        algorithm_version=request.algorithm_version,
        configuration_version=request.configuration_version,
        gsasii_version=gsasii_version,
        instrument_profile_serialization=instrument_profile_serialization,
        proposal_provenance=request.proposal_provenance,
        source_single_phase_hypothesis_id=request.source_single_phase_hypothesis_id,
        screening_pre_rank_score=float(request.primary_candidate.combined_pre_rank_score + request.secondary_candidate.combined_pre_rank_score),
        refinement_status="partial",
        convergence_status="partial",
        completed_stages=completed_stages,
        failed_stage=failed_stage,
        runtime_seconds=runtime_seconds,
        warnings=warnings,
        failure_codes=(),
        exception_summary=None,
        rwp=w_r,
        rp=_safe_float(residuals.get("R")),
        goodness_of_fit=goodness_of_fit,
        chi_squared=_safe_float(covariance_data.get("chisq")),
        weighted_residual=_safe_float(residuals.get("wRb")),
        observation_count=observation_count,
        refined_parameter_count=refined_parameter_count,
        degrees_of_freedom=degrees_of_freedom,
        phase_results=(phase_summaries[0], phase_summaries[1]),
        zero_shift=zero_shift,
        sample_displacement=sample_displacement,
        refined_profile_terms=profile_terms,
        observed_two_theta=observed_two_theta,
        observed_intensities=observed_intensities,
        calculated_total_pattern=calculated_total,
        calculated_background=calculated_background,
        difference_pattern=difference_pattern,
        significant_positive_residual_regions=positive_regions,
        unsupported_strong_predicted_regions=tuple(combined_unsupported),
        regions_supported_by_both_phases=shared_regions,
        regions_primarily_supported_by_one_phase=primary_regions,
        raw_residual_metrics=raw_metrics,
        provenance=provenance + (
            f"pattern_hash={request.pattern_hash}",
            f"primary_candidate_hash={request.primary_candidate.cif_hash}",
            f"secondary_candidate_hash={request.secondary_candidate.cif_hash}",
            f"stage_order={','.join(stage.stage_name for stage in completed_stages)}",
        ),
    )


def _validate_snapshot(
    snapshot: SinglePhaseHypothesisResult,
    initial_lattice: RefinedLatticeParameters,
    settings: SinglePhaseRefinementSettings,
) -> None:
    if snapshot.phase_scale_factor is None or not math.isfinite(snapshot.phase_scale_factor) or snapshot.phase_scale_factor <= 0:
        raise SinglePhaseRefinementError("invalid scale factor")
    if snapshot.zero_shift is not None and abs(snapshot.zero_shift) > settings.maximum_absolute_zero_shift_degrees:
        raise SinglePhaseRefinementError("zero shift exceeded bound")
    if snapshot.sample_displacement is not None and abs(snapshot.sample_displacement) > settings.maximum_absolute_sample_displacement:
        raise SinglePhaseRefinementError("sample displacement exceeded bound")
    initial_values = (
        initial_lattice.length_a,
        initial_lattice.length_b,
        initial_lattice.length_c,
    )
    current_values = (
        snapshot.refined_lattice_parameters.length_a,
        snapshot.refined_lattice_parameters.length_b,
        snapshot.refined_lattice_parameters.length_c,
    )
    for initial, current in zip(initial_values, current_values):
        if initial in (None, 0.0) or current is None:
            continue
        relative_change = abs((current - initial) / initial)
        if relative_change > settings.maximum_relative_lattice_parameter_change:
            raise SinglePhaseRefinementError("lattice change exceeded bound")


def _validate_two_phase_snapshot(
    snapshot: TwoPhaseHypothesisResult,
    initial_lattices: tuple[RefinedLatticeParameters, RefinedLatticeParameters],
    settings: SinglePhaseRefinementSettings,
    decision_settings: Any,
) -> None:
    scale_factors = [phase.scale_factor for phase in snapshot.phase_results]
    if any(scale is None or not math.isfinite(scale) or scale <= 0 for scale in scale_factors):
        raise SinglePhaseRefinementError("invalid phase scale factor")
    if scale_factors[1] is not None and scale_factors[1] < decision_settings.minimum_second_phase_scale_factor:
        raise SinglePhaseRefinementError("second phase scale below threshold")
    if snapshot.zero_shift is not None and abs(snapshot.zero_shift) > settings.maximum_absolute_zero_shift_degrees:
        raise SinglePhaseRefinementError("zero shift exceeded bound")
    if snapshot.sample_displacement is not None and abs(snapshot.sample_displacement) > settings.maximum_absolute_sample_displacement:
        raise SinglePhaseRefinementError("sample displacement exceeded bound")
    for initial, phase in zip(initial_lattices, snapshot.phase_results):
        for initial_value, current_value in zip(
            (initial.length_a, initial.length_b, initial.length_c),
            (
                phase.refined_lattice_parameters.length_a,
                phase.refined_lattice_parameters.length_b,
                phase.refined_lattice_parameters.length_c,
            ),
        ):
            if initial_value in (None, 0.0) or current_value is None:
                continue
            relative_change = abs((current_value - initial_value) / initial_value)
            if relative_change > settings.maximum_relative_lattice_parameter_change:
                raise SinglePhaseRefinementError("lattice change exceeded bound")


def _failure_result(
    request: SinglePhaseRefinementRequest,
    *,
    warnings: tuple[XRDAnalysisWarning, ...],
    failure_codes: tuple[str, ...],
    exception_summary: Optional[str],
    failed_stage: Optional[str],
    runtime_seconds: float,
    completed_stages: tuple[RefinementStageRecord, ...] = (),
    instrument_profile_serialization: Optional[str] = None,
    provenance: tuple[str, ...] = (),
    gsasii_version: Optional[str] = None,
) -> SinglePhaseHypothesisResult:
    return SinglePhaseHypothesisResult(
        hypothesis_id=sha256_digest(
            {
                "candidate_id": request.candidate.candidate_id,
                "pattern_hash": request.pattern_hash,
                "candidate_hash": request.candidate.cif_hash,
                "failure_codes": failure_codes,
            }
        ),
        candidate_id=request.candidate.candidate_id,
        candidate_source=request.candidate.source,
        candidate_source_identifier=request.candidate.source_identifier,
        candidate_cif_hash=request.candidate.cif_hash,
        reference_snapshot=request.reference_snapshot_hash,
        algorithm_version=request.algorithm_version,
        configuration_version=request.configuration_version,
        gsasii_version=gsasii_version,
        instrument_profile_serialization=instrument_profile_serialization,
        screening_pre_rank_score=float(request.candidate.combined_pre_rank_score),
        refinement_status="failed",
        convergence_status="failed",
        completed_stages=completed_stages,
        failed_stage=failed_stage,
        runtime_seconds=runtime_seconds,
        warnings=warnings,
        failure_codes=failure_codes,
        exception_summary=exception_summary,
        rwp=None,
        rp=None,
        goodness_of_fit=None,
        chi_squared=None,
        weighted_residual=None,
        observation_count=len(request.observed_two_theta),
        refined_parameter_count=0,
        degrees_of_freedom=None,
        phase_scale_factor=None,
        refined_lattice_parameters=RefinedLatticeParameters(),
        zero_shift=None,
        sample_displacement=None,
        observed_two_theta=request.observed_two_theta,
        observed_intensities=request.observed_intensities,
        provenance=provenance or request.provenance,
    )


def _two_phase_failure_result(
    request: TwoPhaseRefinementRequest,
    *,
    warnings: tuple[XRDAnalysisWarning, ...],
    failure_codes: tuple[str, ...],
    exception_summary: Optional[str],
    failed_stage: Optional[str],
    runtime_seconds: float,
    completed_stages: tuple[RefinementStageRecord, ...] = (),
    instrument_profile_serialization: Optional[str] = None,
    provenance: tuple[str, ...] = (),
    gsasii_version: Optional[str] = None,
) -> TwoPhaseHypothesisResult:
    empty_lattice = RefinedLatticeParameters()
    empty_phase = PhaseRefinementSummary(
        candidate_id=request.primary_candidate.candidate_id,
        candidate_source=request.primary_candidate.source,
        candidate_source_identifier=request.primary_candidate.source_identifier,
        candidate_cif_hash=request.primary_candidate.cif_hash,
        scale_factor=None,
        refined_lattice_parameters=empty_lattice,
    )
    empty_phase_b = PhaseRefinementSummary(
        candidate_id=request.secondary_candidate.candidate_id,
        candidate_source=request.secondary_candidate.source,
        candidate_source_identifier=request.secondary_candidate.source_identifier,
        candidate_cif_hash=request.secondary_candidate.cif_hash,
        scale_factor=None,
        refined_lattice_parameters=empty_lattice,
    )
    return TwoPhaseHypothesisResult(
        hypothesis_id=sha256_digest(
            {
                "candidate_ids": sorted((request.primary_candidate.candidate_id, request.secondary_candidate.candidate_id)),
                "pattern_hash": request.pattern_hash,
                "failure_codes": failure_codes,
            }
        ),
        candidate_ids=(request.primary_candidate.candidate_id, request.secondary_candidate.candidate_id),
        candidate_sources=(request.primary_candidate.source, request.secondary_candidate.source),
        candidate_source_identifiers=(request.primary_candidate.source_identifier, request.secondary_candidate.source_identifier),
        candidate_cif_hashes=(request.primary_candidate.cif_hash, request.secondary_candidate.cif_hash),
        reference_snapshots=request.reference_snapshots,
        algorithm_version=request.algorithm_version,
        configuration_version=request.configuration_version,
        gsasii_version=gsasii_version,
        instrument_profile_serialization=instrument_profile_serialization,
        proposal_provenance=request.proposal_provenance,
        source_single_phase_hypothesis_id=request.source_single_phase_hypothesis_id,
        screening_pre_rank_score=float(request.primary_candidate.combined_pre_rank_score + request.secondary_candidate.combined_pre_rank_score),
        refinement_status="failed",
        convergence_status="failed",
        completed_stages=completed_stages,
        failed_stage=failed_stage,
        runtime_seconds=runtime_seconds,
        warnings=warnings,
        failure_codes=failure_codes,
        exception_summary=exception_summary,
        rwp=None,
        rp=None,
        goodness_of_fit=None,
        chi_squared=None,
        weighted_residual=None,
        observation_count=len(request.observed_two_theta),
        refined_parameter_count=0,
        degrees_of_freedom=None,
        phase_results=(empty_phase, empty_phase_b),
        zero_shift=None,
        sample_displacement=None,
        observed_two_theta=request.observed_two_theta,
        observed_intensities=request.observed_intensities,
        provenance=provenance or request.provenance,
    )


def _extract_expected_reflections(histogram: Any, phase_name: str) -> list[ExpectedReflectionRecord]:
    ref_list = histogram.data["Reflection Lists"].get(phase_name, {}).get("RefList")
    if ref_list is None:
        return []
    reflections: list[ExpectedReflectionRecord] = []
    for row in ref_list:
        reflections.append(
            ExpectedReflectionRecord(
                h=int(round(float(row[0]))),
                k=int(round(float(row[1]))),
                l=int(round(float(row[2]))),
                multiplicity=int(round(float(row[3]))),
                d_spacing=float(row[4]),
                two_theta=float(row[5]),
                predicted_intensity=float(row[9] * row[11]),
            )
        )
    return reflections


def _extract_positive_residual_regions(
    observed_two_theta: tuple[float, ...],
    residuals: tuple[float, ...],
    observed_intensities: tuple[float, ...],
    expected_reflections: tuple[ExpectedReflectionRecord, ...],
    *,
    settings: SinglePhaseRefinementSettings,
) -> list[ResidualRegion]:
    x = np.asarray(observed_two_theta, dtype=float)
    y = np.asarray(residuals, dtype=float)
    obs = np.asarray(observed_intensities, dtype=float)
    if x.size == 0 or y.size == 0:
        return []
    positive = np.clip(y, 0.0, None)
    threshold = float(np.nanmax(positive) * settings.residual_region_threshold_fraction_of_max) if positive.size else 0.0
    mask = positive > threshold
    regions: list[ResidualRegion] = []
    start_index: Optional[int] = None
    for index, flagged in enumerate(mask):
        if flagged and start_index is None:
            start_index = index
        if start_index is not None and (not flagged or index == len(mask) - 1):
            end_index = index if flagged and index == len(mask) - 1 else index - 1
            if end_index - start_index + 1 >= settings.residual_region_min_points:
                sl = slice(start_index, end_index + 1)
                local_obs_index = start_index + int(np.argmax(obs[sl]))
                expected_positions = tuple(
                    reflection.two_theta
                    for reflection in expected_reflections
                    if x[start_index] - settings.residual_expected_reflection_window_degrees
                    <= reflection.two_theta
                    <= x[end_index] + settings.residual_expected_reflection_window_degrees
                )
                regions.append(
                    ResidualRegion(
                        start_two_theta=float(x[start_index]),
                        end_two_theta=float(x[end_index]),
                        maximum_residual=float(np.max(positive[sl])),
                        integrated_positive_residual=float(trapezoid(positive[sl], x[sl])),
                        nearby_observed_peak_two_theta=float(x[local_obs_index]),
                        nearby_observed_peak_intensity=float(obs[local_obs_index]),
                        nearby_expected_reflection_positions=expected_positions,
                    )
                )
            start_index = None
    return regions


def _extract_unsupported_predicted_regions(
    observed_two_theta: tuple[float, ...],
    observed_intensities: tuple[float, ...],
    expected_reflections: tuple[ExpectedReflectionRecord, ...],
    *,
    settings: SinglePhaseRefinementSettings,
) -> list[UnsupportedPredictedRegion]:
    if not expected_reflections:
        return []
    x = np.asarray(observed_two_theta, dtype=float)
    y = np.asarray(observed_intensities, dtype=float)
    if x.size == 0 or y.size == 0:
        return []
    max_predicted = max(reflection.predicted_intensity for reflection in expected_reflections) or 1.0
    max_observed = float(np.max(y)) or 1.0
    unsupported: list[UnsupportedPredictedRegion] = []
    for reflection in expected_reflections:
        relative_predicted = reflection.predicted_intensity / max_predicted
        if relative_predicted < settings.unsupported_predicted_region_strong_fraction:
            continue
        window = np.abs(x - reflection.two_theta) <= settings.unsupported_predicted_region_window_degrees
        local_observed = float(np.max(y[window])) if np.any(window) else 0.0
        support_score = local_observed / max_observed
        if support_score >= settings.unsupported_predicted_region_support_ratio_threshold:
            continue
        unsupported.append(
            UnsupportedPredictedRegion(
                predicted_two_theta=reflection.two_theta,
                predicted_intensity=reflection.predicted_intensity,
                observed_local_intensity=local_observed,
                support_score=support_score,
                reason="predicted peak remains strong while local observed support is weak",
            )
        )
    return unsupported


def _set_histogram_wavelength(histogram: Any, wavelength_angstrom: float) -> None:
    instrument = histogram.data["Instrument Parameters"][0]
    if "Lam" in instrument:
        instrument["Lam"][0] = wavelength_angstrom
        instrument["Lam"][1] = wavelength_angstrom
    if "Lam1" in instrument:
        instrument["Lam1"][0] = wavelength_angstrom
        instrument["Lam1"][1] = wavelength_angstrom
    if "Lam2" in instrument:
        instrument["Lam2"][0] = wavelength_angstrom
        instrument["Lam2"][1] = wavelength_angstrom
    if "I(L2)/I(L1)" in instrument:
        instrument["I(L2)/I(L1)"][0] = 0.0
        instrument["I(L2)/I(L1)"][1] = 0.0


def _extract_scale_factor(phase_obj: Any, histogram: Any) -> Optional[float]:
    try:
        return _safe_float(phase_obj.data["Histograms"][histogram.name]["Scale"][0])
    except Exception:
        return None


def _extract_lattice_parameters(phase_obj: Any) -> RefinedLatticeParameters:
    cell = phase_obj.get_cell() if hasattr(phase_obj, "get_cell") else {}
    return RefinedLatticeParameters(
        length_a=_safe_float(cell.get("length_a") or cell.get("a")),
        length_b=_safe_float(cell.get("length_b") or cell.get("b")),
        length_c=_safe_float(cell.get("length_c") or cell.get("c")),
        angle_alpha=_safe_float(cell.get("angle_alpha") or cell.get("alpha")),
        angle_beta=_safe_float(cell.get("angle_beta") or cell.get("beta")),
        angle_gamma=_safe_float(cell.get("angle_gamma") or cell.get("gamma")),
        volume=_safe_float(cell.get("volume")),
    )


def _extract_profile_terms(histogram: Any, allowed_terms: tuple[str, ...]) -> dict[str, float]:
    instrument = histogram.data["Instrument Parameters"][0]
    profile_terms: dict[str, float] = {}
    for term in allowed_terms:
        if term in instrument:
            value = _parameter_value(instrument[term])
            if value is not None:
                profile_terms[term] = value
    return profile_terms


def _extract_hap_scalar(phase_obj: Any, histogram: Any, key: str) -> Optional[float]:
    try:
        value = phase_obj.data["Histograms"][histogram.name][key]
    except Exception:
        return None
    if isinstance(value, (list, tuple)) and value:
        return _safe_float(value[0])
    if isinstance(value, dict):
        for item in value.values():
            scalar = _safe_float(item)
            if scalar is not None:
                return scalar
    return _safe_float(value)


def _sample_parameter_value(histogram: Any, key: str) -> Optional[float]:
    try:
        return _parameter_value(histogram.data["Sample Parameters"][key])
    except Exception:
        return None


def _parameter_value(value: Any) -> Optional[float]:
    if isinstance(value, (list, tuple)):
        if len(value) > 1 and isinstance(value[1], bool):
            return _safe_float(value[0])
        for index in (1, 0):
            if len(value) > index and isinstance(value[index], (int, float)):
                return _safe_float(value[index])
        return None
    return _safe_float(value)


def _seed_initial_phase_scale(phase_obj: Any, histogram: Any, value: Optional[float]) -> None:
    if value is None:
        return
    try:
        phase_obj.data["Histograms"][histogram.name]["Scale"][0] = float(value)
    except Exception:
        pass


def _seed_initial_sample_parameter(histogram: Any, key: str, value: Optional[float]) -> None:
    if value is None:
        return
    try:
        histogram.data["Sample Parameters"][key][0] = float(value)
    except Exception:
        pass


def _extract_phase_support_regions(
    observed_two_theta: tuple[float, ...],
    observed_intensities: tuple[float, ...],
    phase_results: tuple[PhaseRefinementSummary, PhaseRefinementSummary],
    *,
    settings: SinglePhaseRefinementSettings,
) -> tuple[tuple[PhaseSupportRegion, ...], tuple[PhaseSupportRegion, ...]]:
    x = np.asarray(observed_two_theta, dtype=float)
    y = np.asarray(observed_intensities, dtype=float)
    if x.size == 0 or y.size == 0:
        return (), ()
    threshold = settings.unsupported_predicted_region_strong_fraction
    strong_sets: list[list[ExpectedReflectionRecord]] = []
    for phase in phase_results:
        if not phase.expected_reflections:
            strong_sets.append([])
            continue
        max_intensity = max(ref.predicted_intensity for ref in phase.expected_reflections) or 1.0
        strong_sets.append(
            [
                reflection
                for reflection in phase.expected_reflections
                if (reflection.predicted_intensity / max_intensity) >= threshold
            ]
        )
    shared: list[PhaseSupportRegion] = []
    primary: list[PhaseSupportRegion] = []
    for phase_index, reflections in enumerate(strong_sets):
        other_index = 1 - phase_index
        for reflection in reflections:
            local_window = np.abs(x - reflection.two_theta) <= settings.unsupported_predicted_region_window_degrees
            if not np.any(local_window):
                continue
            overlapping = [
                other
                for other in strong_sets[other_index]
                if abs(other.two_theta - reflection.two_theta) <= settings.unsupported_predicted_region_window_degrees
            ]
            local_peak_index = int(np.argmax(y[local_window]))
            window_indices = np.where(local_window)[0]
            peak_global_index = int(window_indices[local_peak_index])
            start_two_theta = float(x[window_indices[0]])
            end_two_theta = float(x[window_indices[-1]])
            predicted_positions = [reflection.two_theta] + [item.two_theta for item in overlapping]
            if overlapping:
                shared.append(
                    PhaseSupportRegion(
                        start_two_theta=start_two_theta,
                        end_two_theta=end_two_theta,
                        support_kind="shared",
                        supporting_candidate_ids=(phase_results[phase_index].candidate_id, phase_results[other_index].candidate_id),
                        nearby_observed_peak_two_theta=float(x[peak_global_index]),
                        nearby_observed_peak_intensity=float(y[peak_global_index]),
                        predicted_reflection_positions=tuple(sorted(predicted_positions)),
                    )
                )
            else:
                primary.append(
                    PhaseSupportRegion(
                        start_two_theta=start_two_theta,
                        end_two_theta=end_two_theta,
                        support_kind="primary",
                        supporting_candidate_ids=(phase_results[phase_index].candidate_id,),
                        primarily_supported_candidate_id=phase_results[phase_index].candidate_id,
                        nearby_observed_peak_two_theta=float(x[peak_global_index]),
                        nearby_observed_peak_intensity=float(y[peak_global_index]),
                        predicted_reflection_positions=(reflection.two_theta,),
                    )
                )
    return tuple(shared), tuple(primary)


def _weighted_residual_sum(
    observed_intensities: tuple[float, ...] | np.ndarray,
    calculated_intensities: tuple[float, ...] | np.ndarray,
) -> float:
    observed = np.asarray(observed_intensities, dtype=float)
    calculated = np.asarray(calculated_intensities, dtype=float)
    sigma = _sigma_from_intensity(observed)
    residual = (observed - calculated) / sigma
    residual = residual[np.isfinite(residual)]
    if residual.size == 0:
        return 0.0
    return float(np.sum(residual ** 2))


def _sigma_from_intensity(intensity: np.ndarray) -> np.ndarray:
    sigma = np.sqrt(np.clip(np.abs(intensity), 1.0, None))
    sigma[~np.isfinite(sigma)] = 1.0
    return sigma


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _failure_codes_for_exception(exc: Exception) -> tuple[str, ...]:
    message = str(exc).lower()
    if "second phase scale below threshold" in message:
        return ("second_phase_scale_below_threshold",)
    if "invalid phase scale factor" in message:
        return ("invalid_phase_scale_factor",)
    if "zero shift exceeded bound" in message:
        return ("zero_shift_exceeded_bound",)
    if "sample displacement exceeded bound" in message:
        return ("sample_displacement_exceeded_bound",)
    if "lattice change exceeded bound" in message:
        return ("lattice_change_exceeded_bound",)
    if "invalid scale factor" in message:
        return ("invalid_scale_factor",)
    if "calculated pattern" in message:
        return ("invalid_calculated_pattern",)
    if "svd" in message or "singular" in message:
        return ("singular_refinement",)
    return ("refinement_stage_failed",)


def _refinement_warning(
    code: str,
    field_name: str,
    message: str,
    *,
    stage: str = "single_phase_refinement",
) -> XRDAnalysisWarning:
    return XRDAnalysisWarning(
        code=code,
        message=message,
        severity="warning",
        field=field_name,
        stage=stage,  # type: ignore[arg-type]
    )


def _gsas_version(GSASIIpath: Any) -> Optional[str]:
    getter = getattr(GSASIIpath, "GetVersionNumber", None)
    if callable(getter):
        try:
            return str(getter())
        except Exception:
            return None
    return None


def _unique_warning_objects(warnings: list[XRDAnalysisWarning] | tuple[XRDAnalysisWarning, ...]) -> list[XRDAnalysisWarning]:
    seen: set[tuple[str, str, str, str, str]] = set()
    unique: list[XRDAnalysisWarning] = []
    for warning in warnings:
        key = (warning.code, warning.message, warning.severity, warning.field, warning.stage)
        if key in seen:
            continue
        seen.add(key)
        unique.append(warning)
    return unique


__all__ = [
    "SinglePhaseRefinementError",
    "build_single_phase_refinement_request",
    "refine_single_phase_candidate",
    "refine_top_single_phase_candidates",
    "_build_stage_plan",
    "_resolve_instrument_profile",
]
