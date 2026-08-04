from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Optional

from .candidates import build_ranked_phase_candidates
from .decision import run_final_phase_decision
from .pattern import parse_and_qc_input_pattern
from .refinement import refine_top_single_phase_candidates
from .schemas import (
    CandidateGenerationResult,
    DEFAULT_XRD_ANALYSIS_CONFIG,
    ParsedPatternMetadata,
    PatternQualityControlResult,
    SinglePhaseRefinementBatchResult,
    XRDAnalysisResult,
    XRDAnalysisConfig,
    XRDAnalysisInput,
    validate_xrd_analysis_input,
)

FUTURE_PIPELINE_STAGES: tuple[str, ...] = (
    "input_validation",
    "pattern_parsing",
    "quality_control",
    "candidate_search",
    "single_phase_refinement",
    "two_phase_hypothesis_search",
    "decision",
    "reporting",
)

ProgressCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class PreparedAnalysisStage:
    status: Literal["input_validated", "awaiting_pattern_parsing"]
    next_stage: Literal["pattern_parsing"]
    input_data: XRDAnalysisInput
    algorithm_version: str
    configuration_version: str
    stage_order: tuple[str, ...] = FUTURE_PIPELINE_STAGES


@dataclass(frozen=True)
class PatternAnalysisStageResult:
    status: Literal["pattern accepted for later analysis", "insufficient-quality data"]
    next_stage: Optional[Literal["candidate_generation"]]
    input_data: XRDAnalysisInput
    parsed_pattern: ParsedPatternMetadata
    quality_control: PatternQualityControlResult
    algorithm_version: str
    configuration_version: str
    stage_order: tuple[str, ...] = FUTURE_PIPELINE_STAGES


@dataclass(frozen=True)
class CandidateAnalysisStageResult:
    status: Literal["candidate ranking ready for refinement", "candidate generation failed"]
    next_stage: Optional[Literal["single_phase_refinement"]]
    input_data: XRDAnalysisInput
    parsed_pattern: ParsedPatternMetadata
    quality_control: PatternQualityControlResult
    candidate_generation: CandidateGenerationResult
    algorithm_version: str
    configuration_version: str
    stage_order: tuple[str, ...] = FUTURE_PIPELINE_STAGES


@dataclass(frozen=True)
class SinglePhaseRefinementStageResult:
    status: Literal["single-phase-refinement-ready", "single-phase refinement failed"]
    next_stage: Optional[Literal["two_phase_hypothesis_search"]]
    input_data: XRDAnalysisInput
    parsed_pattern: ParsedPatternMetadata
    quality_control: PatternQualityControlResult
    candidate_generation: CandidateGenerationResult
    single_phase_refinement: SinglePhaseRefinementBatchResult
    algorithm_version: str
    configuration_version: str
    stage_order: tuple[str, ...] = FUTURE_PIPELINE_STAGES


def validate_analysis_input(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> XRDAnalysisInput:
    """Validate the Milestone 2 plain-data input contract before any science runs."""
    return validate_xrd_analysis_input(analysis_input, configuration=configuration)


def prepare_analysis_input(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> PreparedAnalysisStage:
    """Validate the input and return the staged hand-off to future parsing work."""
    validated = validate_analysis_input(analysis_input, configuration=configuration)
    return PreparedAnalysisStage(
        status="input_validated",
        next_stage="pattern_parsing",
        input_data=validated,
        algorithm_version=validated.algorithm_version,
        configuration_version=validated.configuration_version,
    )


def run_xrd_analysis_pipeline(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
    progress_callback: ProgressCallback | None = None,
):
    """Run the Milestone 6 staged pipeline through final uncertainty-aware phase-state decision."""
    validated = validate_analysis_input(analysis_input, configuration=configuration)
    if progress_callback is not None:
        progress_callback("parsing_pattern", "Parsing and normalizing the XRD pattern.")
    parsed_pattern, quality_control = parse_and_qc_input_pattern(
        validated,
        configuration=configuration,
    )
    if progress_callback is not None:
        progress_callback("quality_control", "Running XRD quality-control checks.")
    if not quality_control.passed:
        return XRDAnalysisResult(
            phase_state="insufficient-quality data",
            warnings=quality_control.warnings,
            algorithm_version=validated.algorithm_version,
            configuration_version=validated.configuration_version,
            best_hypothesis=None,
            alternative_hypotheses=(),
            evidence_score=0.0,
            provenance=validated.provenance,
            parsed_pattern=parsed_pattern,
            quality_control=quality_control,
            failure_codes=("insufficient-quality data",),
            analysis_provenance_notes=("pipeline:qc_failed",),
        )
    if progress_callback is not None:
        progress_callback("generating_candidates", "Generating and ranking candidate phases.")
    candidate_generation = build_ranked_phase_candidates(
        validated,
        parsed_pattern,
        quality_control,
        configuration=configuration,
    )
    if progress_callback is not None:
        progress_callback("refining_single_phase", "Refining the strongest single-phase candidates.")
    single_phase_refinement = refine_top_single_phase_candidates(
        validated,
        parsed_pattern,
        candidate_generation,
        configuration=configuration,
    )
    return run_final_phase_decision(
        validated,
        parsed_pattern,
        quality_control,
        candidate_generation,
        single_phase_refinement,
        configuration=configuration,
        progress_callback=progress_callback,
    )


def run_and_persist_xrd_analysis(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
):
    """Run the Milestone 6 scientific pipeline, then persist Milestone 7 artifacts."""
    from .persistence import run_and_persist_xrd_analysis as _run_and_persist

    return _run_and_persist(analysis_input, configuration=configuration)
