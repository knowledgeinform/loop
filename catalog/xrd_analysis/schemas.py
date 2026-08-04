from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Optional

from catalog import xrd_store


WarningSeverity = Literal["info", "warning", "error"]
WarningStage = Literal[
    "input_assembly",
    "validation",
    "pattern_parsing",
    "quality_control",
    "candidate_generation",
    "single_phase_refinement",
    "two_phase_refinement",
    "decision",
]
CoordinateType = Literal["two_theta", "d_spacing", "q"]
PatternType = Literal["continuous", "stick"]
CoordinateOrder = Literal["ascending", "descending", "unsorted", "constant", "empty"]
PatternParserType = Literal[
    "loop_csv",
    "generic_csv",
    "generic_txt",
    "rigaku_ascii",
    "reflection_card",
    "binary_raw",
    "dataframe",
]
PatternStageStatus = Literal["pattern accepted for later analysis", "insufficient-quality data"]
CandidateSource = Literal[
    "intended_structure",
    "curated_reference",
    "material_linked_structure",
    "recipe_linked_structure",
    "trial_linked_structure",
    "literature_linked_structure",
    "dft_linked_structure",
    "external_snapshot",
    "offline_cached_cod",
]
CandidateGenerationStatus = Literal[
    "candidate ranking ready for refinement",
    "candidate generation failed",
]
CandidateFailureReason = Literal[
    "no_candidate_sources_available",
    "no_chemically_compatible_candidates",
    "no_simulatable_candidates",
    "no_rankable_candidates",
]
SinglePhaseRefinementStatus = Literal[
    "single-phase-refinement-ready",
    "single-phase refinement failed",
]
TwoPhaseRefinementStatus = Literal[
    "two-phase-hypotheses-ready",
    "two-phase refinement failed",
]
SinglePhaseCandidateStatus = Literal["completed", "failed", "partial"]
RefinementConvergenceStatus = Literal["converged", "partial", "failed", "not_run"]
SelectedModelKind = Literal["single_phase", "two_phase", "none"]
PhaseState = Literal[
    "likely single-phase",
    "likely multiphase",
    "unresolved",
    "insufficient-quality data",
]

ALLOWED_PHASE_STATES: tuple[PhaseState, ...] = (
    "likely single-phase",
    "likely multiphase",
    "unresolved",
    "insufficient-quality data",
)

INPUT_WARNING_CODES: tuple[str, ...] = (
    "missing_raw_file_hash",
    "missing_raw_file_reference",
    "missing_wavelength",
    "missing_coordinate_column",
    "missing_intensity_column",
    "missing_instrument_profile",
    "missing_nominal_composition",
    "missing_element_list",
    "missing_scan_range",
    "inconsistent_stoichiometry",
    "missing_recipe_identity",
    "missing_trial_identity",
)

PATTERN_WARNING_CODES: tuple[str, ...] = (
    "invalid_rows_removed",
    "missing_wavelength_for_conversion",
    "coordinate_type_conflict",
    "parsed_range_mismatch",
    "parsed_step_size_mismatch",
    "irregular_step_spacing",
    "repeated_coordinates",
    "missing_intervals",
    "coordinates_out_of_bounds",
    "all_non_positive_intensity",
    "high_negative_intensity_fraction",
    "constant_intensity",
    "possible_intensity_clipping",
    "stick_pattern_limited",
    "parser_output_type_conflict",
)

CANDIDATE_WARNING_CODES: tuple[str, ...] = (
    "no_candidate_sources_available",
    "intended_structure_reference_missing",
    "candidate_cif_unreadable",
    "candidate_contains_unlisted_element",
    "candidate_formula_unparseable",
    "candidate_simulation_failed",
    "duplicate_candidate_removed",
    "no_chemically_compatible_candidates",
    "no_simulatable_candidates",
    "no_rankable_candidates",
    "reference_snapshot_mismatch",
)

REFINEMENT_WARNING_CODES: tuple[str, ...] = (
    "missing_two_theta_pattern",
    "missing_wavelength_for_refinement",
    "missing_instrument_profile",
    "invalid_instrument_profile",
    "candidate_cif_load_failed",
    "gsas_project_creation_failed",
    "histogram_load_failed",
    "phase_load_failed",
    "refinement_stage_failed",
    "refinement_nonconvergent",
    "singular_refinement",
    "invalid_scale_factor",
    "lattice_change_exceeded_bound",
    "zero_shift_exceeded_bound",
    "sample_displacement_exceeded_bound",
    "invalid_calculated_pattern",
    "result_extraction_failed",
    "candidate_refinement_failed",
    "all_single_phase_refinements_failed",
)

DECISION_WARNING_CODES: tuple[str, ...] = (
    "no_valid_single_phase_hypothesis",
    "no_valid_two_phase_hypothesis",
    "two_phase_pair_generation_failed",
    "duplicate_two_phase_pair_removed",
    "two_phase_refinement_failed",
    "invalid_phase_scale_factor",
    "second_phase_scale_below_threshold",
    "insufficient_second_phase_reflection_support",
    "second_phase_evidence_fully_overlapped",
    "model_comparison_unavailable",
    "model_comparison_ambiguous",
    "unstable_phase_assignment",
    "unstable_second_phase_scale",
    "all_two_phase_refinements_failed",
    "candidate_set_may_be_incomplete",
    "final_decision_unresolved",
)

QUALITY_FAILURE_CODES: tuple[str, ...] = (
    "no_usable_points",
    "too_few_usable_points",
    "zero_coordinate_range",
    "flat_signal",
    "insufficient_peak_evidence",
    "all_non_positive_intensity",
)

_WARNING_CODE_SET = (
    set(INPUT_WARNING_CODES)
    | set(PATTERN_WARNING_CODES)
    | set(CANDIDATE_WARNING_CODES)
    | set(REFINEMENT_WARNING_CODES)
    | set(DECISION_WARNING_CODES)
)
_RADIATION_WAVELENGTHS = {
    "cu_ka": 1.5406,
    "cuka": 1.5406,
    "cu kα": 1.5406,
    "cu ka": 1.5406,
    "co_ka": 1.7890,
    "coka": 1.7890,
    "co kα": 1.7890,
    "co ka": 1.7890,
    "mo_ka": 0.7107,
    "moka": 0.7107,
    "mo kα": 0.7107,
    "mo ka": 0.7107,
}
_MISSING_TOKENS = {"", "na", "n/a", "none", "null", "unknown", "unspecified"}
_SCAN_RANGE_RE = re.compile(
    r"([+-]?[0-9]+(?:\.[0-9]+)?)\s*(?:-|to|–|—|~)\s*([+-]?[0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


class XRDAnalysisInputError(ValueError):
    """Raised when repository records cannot be associated with an analysis input."""


class XRDPatternError(ValueError):
    """Base class for typed Milestone 3 pattern parsing and QC failures."""


class RawFileReferenceError(XRDPatternError):
    """Raised when a raw-file reference cannot be resolved into readable input."""


class UnsupportedPatternFormatError(XRDPatternError):
    """Raised when the referenced file format cannot be parsed by the repo parser."""


class PatternParseError(XRDPatternError):
    """Raised when pattern parsing fails before QC can run."""


class CoordinateConversionError(XRDPatternError):
    """Raised when a required coordinate conversion is physically impossible."""


class UnusablePatternError(XRDPatternError):
    """Raised when cleanup leaves no meaningful pattern data to analyze."""


@dataclass(frozen=True)
class XRDAnalysisWarning:
    code: str
    message: str
    severity: WarningSeverity
    field: str
    stage: WarningStage = "input_assembly"

    def __post_init__(self) -> None:
        if self.code not in _WARNING_CODE_SET:
            raise ValueError(f"Unknown XRD analysis warning code: {self.code}")


@dataclass(frozen=True)
class StoichiometricAmount:
    element: str
    amount: float


@dataclass(frozen=True)
class ExpectedSiteAssignment:
    element: str
    site_label: str


@dataclass(frozen=True)
class MetadataItem:
    key: str
    value: Optional[str] = None


@dataclass(frozen=True)
class RawFileReference:
    reference_kind: Literal["stored_path", "resolved_path", "raw_data_link", "raw_db_path"]
    locator: str
    original_filename: Optional[str] = None
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None


@dataclass(frozen=True)
class LinkedStructureReference:
    reference_id: str
    source_kind: Literal["material", "recipe", "trial", "literature", "dft", "external_snapshot"]
    cif_path: Optional[str] = None
    formula: Optional[str] = None
    structure_family: Optional[str] = None
    space_group: Optional[str] = None
    source_identifier: Optional[str] = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstrumentProfile:
    instrument_label: Optional[str] = None
    instrument_parameter_path: Optional[str] = None
    geometry: Optional[str] = None
    sample_holder: Optional[str] = None
    metadata_items: tuple[MetadataItem, ...] = ()


@dataclass(frozen=True)
class PrecursorRecord:
    name: Optional[str] = None
    formula: Optional[str] = None
    cas_number: Optional[str] = None
    purity: Optional[str] = None
    supplier: Optional[str] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class SynthesisStepRecord:
    step_number: int
    step_type: str
    notes: Optional[str] = None
    atmosphere: Optional[str] = None
    furnace_type: Optional[str] = None
    temperature_c: Optional[float] = None
    max_temp_c: Optional[float] = None
    ramp_rate_c_min: Optional[float] = None
    hold_time_hours: Optional[float] = None
    hold_time_min: Optional[float] = None
    scan_speed_deg_min: Optional[float] = None
    step_size_deg: Optional[float] = None
    radiation: Optional[str] = None
    two_theta_range: Optional[str] = None
    precursors: tuple[PrecursorRecord, ...] = ()
    extra_fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SynthesisContext:
    ordered_steps: tuple[SynthesisStepRecord, ...]
    precursor_records: tuple[PrecursorRecord, ...]
    temperatures_c: tuple[float, ...]
    ramp_rates_c_min: tuple[float, ...]
    hold_times_hours: tuple[float, ...]
    atmospheres: tuple[str, ...]
    furnace_types: tuple[str, ...]
    preparation_notes: tuple[str, ...]


@dataclass(frozen=True)
class XRDAnalysisProvenance:
    source_material: dict[str, Any]
    source_recipe: dict[str, Any]
    source_trial: dict[str, Any]
    source_raw_file: Optional[dict[str, Any]]
    measurement_metadata: tuple[MetadataItem, ...]
    assembly_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class XRDAnalysisInput:
    material_auid: str
    recipe_auid: str
    trial_id: str
    raw_file_hash: Optional[str]
    raw_file_reference: Optional[RawFileReference]
    nominal_composition: Optional[str]
    elements: tuple[str, ...]
    stoichiometric_amounts: tuple[StoichiometricAmount, ...]
    structure_family: Optional[str]
    expected_space_group: Optional[str]
    expected_site_assignments: tuple[ExpectedSiteAssignment, ...]
    radiation_source: Optional[str]
    wavelength_angstrom: Optional[float]
    coordinate_type: Optional[CoordinateType]
    coordinate_column: Optional[str]
    intensity_column: Optional[str]
    scan_min: Optional[float]
    scan_max: Optional[float]
    step_size: Optional[float]
    scan_speed: Optional[float]
    instrument_profile: Optional[InstrumentProfile]
    synthesis_context: SynthesisContext
    warnings: tuple[XRDAnalysisWarning, ...]
    algorithm_version: str
    configuration_version: str
    provenance: XRDAnalysisProvenance
    linked_structure_references: tuple[LinkedStructureReference, ...] = ()


@dataclass(frozen=True)
class PatternProvenance:
    parser_type: PatternParserType
    source_label: str
    metadata_items: tuple[MetadataItem, ...] = ()
    cleanup_steps: tuple[str, ...] = ()
    invalid_row_count: int = 0
    duplicate_group_count: int = 0
    duplicate_coordinate_tolerance: Optional[float] = None
    duplicate_coordinate_rule: Optional[str] = None


@dataclass(frozen=True)
class ParsedPatternMetadata:
    parser_type: PatternParserType
    pattern_type: PatternType
    original_coordinate_type: CoordinateType
    coordinate_column: str
    intensity_column: str
    original_coordinates: tuple[Optional[float], ...]
    original_intensities: tuple[Optional[float], ...]
    normalized_two_theta: Optional[tuple[float, ...]]
    normalized_d_spacing: Optional[tuple[float, ...]]
    normalized_q: Optional[tuple[float, ...]]
    normalized_intensities: tuple[float, ...]
    wavelength_angstrom: Optional[float]
    usable_point_count: int
    coordinate_order: CoordinateOrder
    median_step_size: Optional[float]
    step_size_variation: Optional[float]
    warnings: tuple[XRDAnalysisWarning, ...] = ()
    provenance: PatternProvenance = field(
        default_factory=lambda: PatternProvenance(
            parser_type="dataframe",
            source_label="",
        )
    )


@dataclass(frozen=True)
class PatternQualityControlResult:
    status: PatternStageStatus
    pattern_type: PatternType
    usable_point_count: int
    coordinate_min: Optional[float]
    coordinate_max: Optional[float]
    range_width: Optional[float]
    median_step_size: Optional[float]
    step_size_variation: Optional[float]
    fraction_invalid_rows_removed: float
    duplicate_count: int
    negative_intensity_fraction: float
    non_positive_intensity_fraction: float
    approximate_signal_to_noise: Optional[float]
    detectable_peak_region_count: int
    missing_interval_count: int
    clipping_detected: bool
    failure_codes: tuple[str, ...] = ()
    warnings: tuple[XRDAnalysisWarning, ...] = ()
    provenance_notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "pattern accepted for later analysis"


QualityControlPlaceholder = PatternQualityControlResult


@dataclass(frozen=True)
class PhaseCandidatePlaceholder:
    candidate_id: str
    label: Optional[str] = None
    source: Optional[str] = None
    rank_hint: Optional[int] = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferencePhaseManifestEntry:
    candidate_identifier: str
    relative_cif_path: str
    sha256: str
    formula: Optional[str]
    element_set: tuple[str, ...]
    space_group: Optional[str]
    structure_family: Optional[str]
    source: str
    source_identifier: Optional[str]
    notes: tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True)
class ReferencePhaseSnapshot:
    snapshot_version: str
    snapshot_hash: str
    manifest_path: str
    entries: tuple[ReferencePhaseManifestEntry, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateSimulation:
    candidate_id: str
    reflection_positions_two_theta: tuple[float, ...]
    reflection_relative_intensities: tuple[float, ...]
    measured_coordinate_min: Optional[float]
    measured_coordinate_max: Optional[float]
    wavelength_angstrom: Optional[float]
    settings_version: str
    warnings: tuple[XRDAnalysisWarning, ...] = ()
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class RankedPhaseCandidate:
    candidate_id: str
    source: CandidateSource
    source_identifier: str
    source_snapshot: Optional[str]
    cif_path: Optional[str]
    cif_hash: Optional[str]
    formula: Optional[str]
    normalized_composition: tuple[StoichiometricAmount, ...]
    element_set: tuple[str, ...]
    space_group: Optional[str]
    structure_family: Optional[str]
    intended_structure_match: bool
    chemical_compatibility_score: float
    stoichiometric_similarity_score: float
    synthesis_context_score: float
    diffraction_pre_rank_score: float
    combined_pre_rank_score: float
    duplicate_cluster_id: Optional[str]
    warnings: tuple[XRDAnalysisWarning, ...] = ()
    provenance: tuple[str, ...] = ()
    simulation: Optional[CandidateSimulation] = None
    alternate_sources: tuple[str, ...] = ()
    excluded: bool = False


@dataclass(frozen=True)
class CandidateGenerationResult:
    status: CandidateGenerationStatus
    failure_reason: Optional[CandidateFailureReason]
    warnings: tuple[XRDAnalysisWarning, ...]
    reference_snapshot: Optional[ReferencePhaseSnapshot]
    candidates: tuple[RankedPhaseCandidate, ...]
    algorithm_version: str
    configuration_version: str
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileParameterBound:
    name: str
    minimum: float
    maximum: float


@dataclass(frozen=True)
class SinglePhaseRefinementRequest:
    material_auid: str
    recipe_auid: str
    trial_id: str
    raw_file_hash: Optional[str]
    observed_two_theta: tuple[float, ...]
    observed_intensities: tuple[float, ...]
    original_coordinate_type: CoordinateType
    wavelength_angstrom: Optional[float]
    instrument_profile: Optional[InstrumentProfile]
    candidate: RankedPhaseCandidate
    algorithm_version: str
    configuration_version: str
    reference_snapshot_hash: Optional[str] = None
    pattern_reference: Optional[str] = None
    pattern_hash: Optional[str] = None
    initial_phase_scale_factor: Optional[float] = None
    initial_zero_shift: Optional[float] = None
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class RefinementStageRecord:
    stage_name: str
    status: Literal["completed", "failed", "skipped"]
    runtime_seconds: float
    warnings: tuple[XRDAnalysisWarning, ...] = ()
    failure_codes: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpectedReflectionRecord:
    h: int
    k: int
    l: int
    multiplicity: int
    d_spacing: float
    two_theta: float
    predicted_intensity: float


@dataclass(frozen=True)
class ResidualRegion:
    start_two_theta: float
    end_two_theta: float
    maximum_residual: float
    integrated_positive_residual: float
    nearby_observed_peak_two_theta: Optional[float] = None
    nearby_observed_peak_intensity: Optional[float] = None
    nearby_expected_reflection_positions: tuple[float, ...] = ()


@dataclass(frozen=True)
class UnsupportedPredictedRegion:
    predicted_two_theta: float
    predicted_intensity: float
    observed_local_intensity: float
    support_score: float
    reason: str


@dataclass(frozen=True)
class RefinedLatticeParameters:
    length_a: Optional[float] = None
    length_b: Optional[float] = None
    length_c: Optional[float] = None
    angle_alpha: Optional[float] = None
    angle_beta: Optional[float] = None
    angle_gamma: Optional[float] = None
    volume: Optional[float] = None


@dataclass(frozen=True)
class SinglePhaseHypothesisResult:
    hypothesis_id: str
    candidate_id: str
    candidate_source: CandidateSource
    candidate_source_identifier: str
    candidate_cif_hash: Optional[str]
    reference_snapshot: Optional[str]
    algorithm_version: str
    configuration_version: str
    gsasii_version: Optional[str]
    instrument_profile_serialization: Optional[str]
    screening_pre_rank_score: float
    refinement_status: SinglePhaseCandidateStatus
    convergence_status: RefinementConvergenceStatus
    completed_stages: tuple[RefinementStageRecord, ...]
    failed_stage: Optional[str]
    runtime_seconds: float
    warnings: tuple[XRDAnalysisWarning, ...]
    failure_codes: tuple[str, ...]
    exception_summary: Optional[str]
    rwp: Optional[float]
    rp: Optional[float]
    goodness_of_fit: Optional[float]
    chi_squared: Optional[float]
    weighted_residual: Optional[float]
    observation_count: int
    refined_parameter_count: int
    degrees_of_freedom: Optional[int]
    phase_scale_factor: Optional[float]
    refined_lattice_parameters: RefinedLatticeParameters
    zero_shift: Optional[float]
    sample_displacement: Optional[float]
    refined_profile_terms: dict[str, float] = field(default_factory=dict)
    crystallite_size: Optional[float] = None
    microstrain: Optional[float] = None
    observed_two_theta: tuple[float, ...] = ()
    observed_intensities: tuple[float, ...] = ()
    calculated_total_pattern: tuple[float, ...] = ()
    calculated_background: tuple[float, ...] = ()
    difference_pattern: tuple[float, ...] = ()
    expected_reflections: tuple[ExpectedReflectionRecord, ...] = ()
    significant_positive_residual_regions: tuple[ResidualRegion, ...] = ()
    unsupported_strong_predicted_regions: tuple[UnsupportedPredictedRegion, ...] = ()
    raw_residual_metrics: dict[str, Any] = field(default_factory=dict)
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class SinglePhaseRefinementBatchResult:
    status: SinglePhaseRefinementStatus
    warnings: tuple[XRDAnalysisWarning, ...]
    successful_hypotheses: tuple[SinglePhaseHypothesisResult, ...]
    failed_candidate_results: tuple[SinglePhaseHypothesisResult, ...]
    algorithm_version: str
    configuration_version: str
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class TwoPhasePairProposal:
    proposal_id: str
    base_single_phase_hypothesis_id: str
    primary_candidate_id: str
    secondary_candidate_id: str
    normalized_candidate_ids: tuple[str, str]
    proposal_score: float
    supported_residual_region_count: int
    partially_distinctive_region_count: int
    includes_intended_structure: bool
    warnings: tuple[XRDAnalysisWarning, ...] = ()
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class TwoPhaseRefinementRequest:
    material_auid: str
    recipe_auid: str
    trial_id: str
    raw_file_hash: Optional[str]
    observed_two_theta: tuple[float, ...]
    observed_intensities: tuple[float, ...]
    wavelength_angstrom: Optional[float]
    instrument_profile: Optional[InstrumentProfile]
    primary_candidate: RankedPhaseCandidate
    secondary_candidate: RankedPhaseCandidate
    algorithm_version: str
    configuration_version: str
    source_single_phase_hypothesis_id: str
    proposal_provenance: tuple[str, ...]
    reference_snapshots: tuple[Optional[str], Optional[str]] = (None, None)
    pattern_reference: Optional[str] = None
    pattern_hash: Optional[str] = None
    initial_phase_scale_factors: tuple[Optional[float], Optional[float]] = (None, None)
    initial_zero_shift: Optional[float] = None
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhaseRefinementSummary:
    candidate_id: str
    candidate_source: CandidateSource
    candidate_source_identifier: str
    candidate_cif_hash: Optional[str]
    scale_factor: Optional[float]
    refined_lattice_parameters: RefinedLatticeParameters
    expected_reflections: tuple[ExpectedReflectionRecord, ...] = ()
    unsupported_predicted_regions: tuple[UnsupportedPredictedRegion, ...] = ()


@dataclass(frozen=True)
class PhaseSupportRegion:
    start_two_theta: float
    end_two_theta: float
    support_kind: Literal["shared", "primary"]
    supporting_candidate_ids: tuple[str, ...]
    primarily_supported_candidate_id: Optional[str] = None
    nearby_observed_peak_two_theta: Optional[float] = None
    nearby_observed_peak_intensity: Optional[float] = None
    predicted_reflection_positions: tuple[float, ...] = ()


@dataclass(frozen=True)
class TwoPhaseHypothesisResult:
    hypothesis_id: str
    candidate_ids: tuple[str, str]
    candidate_sources: tuple[CandidateSource, CandidateSource]
    candidate_source_identifiers: tuple[str, str]
    candidate_cif_hashes: tuple[Optional[str], Optional[str]]
    reference_snapshots: tuple[Optional[str], Optional[str]]
    algorithm_version: str
    configuration_version: str
    gsasii_version: Optional[str]
    instrument_profile_serialization: Optional[str]
    proposal_provenance: tuple[str, ...]
    source_single_phase_hypothesis_id: str
    screening_pre_rank_score: float
    refinement_status: SinglePhaseCandidateStatus
    convergence_status: RefinementConvergenceStatus
    completed_stages: tuple[RefinementStageRecord, ...]
    failed_stage: Optional[str]
    runtime_seconds: float
    warnings: tuple[XRDAnalysisWarning, ...]
    failure_codes: tuple[str, ...]
    exception_summary: Optional[str]
    rwp: Optional[float]
    rp: Optional[float]
    goodness_of_fit: Optional[float]
    chi_squared: Optional[float]
    weighted_residual: Optional[float]
    observation_count: int
    refined_parameter_count: int
    degrees_of_freedom: Optional[int]
    phase_results: tuple[PhaseRefinementSummary, PhaseRefinementSummary]
    zero_shift: Optional[float]
    sample_displacement: Optional[float]
    refined_profile_terms: dict[str, float] = field(default_factory=dict)
    observed_two_theta: tuple[float, ...] = ()
    observed_intensities: tuple[float, ...] = ()
    calculated_total_pattern: tuple[float, ...] = ()
    calculated_background: tuple[float, ...] = ()
    difference_pattern: tuple[float, ...] = ()
    significant_positive_residual_regions: tuple[ResidualRegion, ...] = ()
    unsupported_strong_predicted_regions: tuple[UnsupportedPredictedRegion, ...] = ()
    regions_supported_by_both_phases: tuple[PhaseSupportRegion, ...] = ()
    regions_primarily_supported_by_one_phase: tuple[PhaseSupportRegion, ...] = ()
    raw_residual_metrics: dict[str, Any] = field(default_factory=dict)
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class TwoPhaseRefinementBatchResult:
    status: TwoPhaseRefinementStatus
    warnings: tuple[XRDAnalysisWarning, ...]
    pair_proposals: tuple[TwoPhasePairProposal, ...]
    successful_hypotheses: tuple[TwoPhaseHypothesisResult, ...]
    failed_hypotheses: tuple[TwoPhaseHypothesisResult, ...]
    algorithm_version: str
    configuration_version: str
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class PenalizedModelMetrics:
    hypothesis_id: str
    model_type: SelectedModelKind
    candidate_ids: tuple[str, ...]
    weighted_residual_sum: Optional[float]
    observation_count: int
    refined_parameter_count: int
    aic: Optional[float]
    aicc: Optional[float]
    bic: Optional[float]


@dataclass(frozen=True)
class ModelComparisonSummary:
    best_single_phase_metrics: Optional[PenalizedModelMetrics]
    best_two_phase_metrics: Optional[PenalizedModelMetrics]
    preferred_model: SelectedModelKind
    delta_aic: Optional[float] = None
    delta_aicc: Optional[float] = None
    delta_bic: Optional[float] = None
    minimum_penalized_improvement: Optional[float] = None
    warnings: tuple[XRDAnalysisWarning, ...] = ()


@dataclass(frozen=True)
class DecisionCriteriaResult:
    single_phase_adequate: bool
    two_phase_penalized_improvement: bool
    second_phase_positive_scale: bool
    second_phase_scale_above_threshold: bool
    second_phase_reflection_support_sufficient: bool
    second_phase_partially_distinctive: bool
    second_phase_not_dominated_by_one_region: bool
    classification_stable: bool
    score_stable: bool
    candidate_set_may_be_incomplete: bool
    warnings: tuple[XRDAnalysisWarning, ...] = ()
    failure_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StabilityRunResult:
    perturbation_label: str
    model_type: SelectedModelKind
    candidate_ids: tuple[str, ...]
    selected_hypothesis_id: Optional[str]
    phase_state: PhaseState
    background_coefficient_count: int
    residual_threshold_multiplier: float
    scale_seed_multipliers: tuple[float, ...]
    zero_shift_seed: Optional[float]
    dominant_region_downweighted: bool
    penalized_score_delta: Optional[float]
    second_phase_scale_factor: Optional[float]
    provenance: tuple[str, ...] = ()


@dataclass(frozen=True)
class StabilityAssessmentResult:
    runs: tuple[StabilityRunResult, ...]
    classification_agreement: float
    candidate_agreement: float
    second_phase_support_retained: bool
    scale_stability: Optional[float]
    score_stability: Optional[float]
    warnings: tuple[XRDAnalysisWarning, ...] = ()


@dataclass(frozen=True)
class SelectedBestModel:
    model_type: SelectedModelKind
    hypothesis_id: Optional[str]
    candidate_ids: tuple[str, ...] = ()
    selection_reason: Optional[str] = None


@dataclass(frozen=True)
class AnalysisEvidenceComponents:
    model_comparison_margin: float
    residual_evidence: float
    second_phase_evidence: float
    stability: float
    data_quality: float
    candidate_coverage: float
    refinement_convergence: float
    overall_score: float


@dataclass(frozen=True)
class LinkedStructureSnapshotRecord:
    reference_id: str
    source_kind: str
    source_identifier: Optional[str]
    cif_hash: Optional[str]
    snapshot_hash: Optional[str]


@dataclass(frozen=True)
class PersistedArtifactRecord:
    relative_path: str
    sha256: str
    content_type: str
    size_bytes: int
    artifact_type: str
    parent_raw_file_hash: Optional[str]
    analysis_id: str
    algorithm_version: str


@dataclass(frozen=True)
class CompactAnalysisSummary:
    analysis_id: str
    analysis_status: str
    phase_state: PhaseState
    evidence_score: Optional[float]
    warning_count: int
    algorithm_version: str
    configuration_version: str
    selected_model_type: Optional[str] = None
    selected_candidate_ids: tuple[str, ...] = ()
    failure_code_count: int = 0
    reference_snapshot_identity: Optional[str] = None
    completion_time: Optional[str] = None
    result_manifest_path: Optional[str] = None
    best_hypothesis_id: Optional[str] = None


@dataclass(frozen=True)
class ReproducibilityManifest:
    analysis_id: str
    raw_file_hash: Optional[str]
    material_auid: str
    recipe_auid: str
    trial_id: str
    input_schema_version: str
    result_schema_version: str
    algorithm_version: str
    configuration_version: str
    configuration_hash: str
    configuration_serialization: str
    reference_phase_snapshot_version: Optional[str]
    reference_phase_snapshot_hash: Optional[str]
    linked_structure_snapshots: tuple[LinkedStructureSnapshotRecord, ...]
    gsasii_version: Optional[str]
    python_version: str
    package_versions: dict[str, Optional[str]]
    parsing_method: str
    candidate_simulation_method: str
    refinement_method: str
    model_comparison_formulas: tuple[str, ...]
    classification_thresholds: dict[str, Any]
    identity_fields: tuple[str, ...]
    provenance_only_fields: tuple[str, ...]
    input_warnings: tuple[XRDAnalysisWarning, ...]
    analysis_warnings: tuple[XRDAnalysisWarning, ...]
    created_artifacts: tuple[PersistedArtifactRecord, ...]
    result_hash: str
    started_at: Optional[str]
    completed_at: Optional[str]
    execution_environment_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CacheValidationResult:
    analysis_id: str
    analysis_directory: str
    valid: bool
    status: str
    failure_code: Optional[str] = None
    warning_codes: tuple[str, ...] = ()
    missing_artifacts: tuple[str, ...] = ()
    detail: Optional[str] = None


@dataclass(frozen=True)
class PersistedXRDAnalysis:
    analysis_id: str
    analysis_directory: str
    result: "XRDAnalysisResult"
    summary: CompactAnalysisSummary
    reproducibility_manifest: ReproducibilityManifest
    persisted_artifacts: tuple[PersistedArtifactRecord, ...]
    reused_existing: bool = False
    persistence_warning_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhaseHypothesis:
    hypothesis_id: str
    crystalline_phase_count: Optional[int] = None
    candidate_ids: tuple[str, ...] = ()
    description: Optional[str] = None
    evidence_score: Optional[float] = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WarningSeverityDefinition:
    severity: WarningSeverity
    description: str


@dataclass(frozen=True)
class PatternProcessingSettings:
    duplicate_coordinate_tolerance: float = 1e-6
    duplicate_coordinate_rule: str = "sum_intensity_keep_first_coordinate"
    coordinate_out_of_bounds_min_two_theta: float = 0.0
    coordinate_out_of_bounds_max_two_theta: float = 180.0


@dataclass(frozen=True)
class QualityControlSettingsPlaceholder:
    require_scan_range_metadata: bool = False
    require_wavelength_for_axis_conversions: bool = True
    min_usable_points_continuous: int = 20
    min_range_width_continuous: float = 1.0
    min_signal_to_noise: float = 1.5
    min_detectable_peak_regions_continuous: int = 1
    flat_signal_relative_std_threshold: float = 0.01
    range_mismatch_tolerance: float = 0.5
    step_size_relative_tolerance: float = 0.25
    irregular_step_variation_warning_ratio: float = 0.2
    missing_interval_multiplier: float = 5.0
    negative_intensity_fraction_warning: float = 0.05
    negative_intensity_fraction_failure: float = 0.5
    clipping_fraction_threshold: float = 0.02
    saturation_relative_level: float = 0.99
    reserved_noise_metric: Optional[str] = "savgol_residual_std"


@dataclass(frozen=True)
class CandidateRankingSettings:
    use_nominal_stoichiometry: bool = True
    use_synthesis_context: bool = True
    maximum_candidates_before_simulation: int = 24
    maximum_simulated_candidates: int = 12
    final_top_k: int = 6
    maximum_representatives_per_duplicate_cluster: int = 1
    maximum_allowed_screening_shift_degrees: float = 0.35
    peak_position_tolerance_degrees: float = 0.20
    strong_peak_fraction_threshold: float = 0.35
    stick_pattern_peak_position_tolerance_degrees: float = 0.18
    coverage_weight: float = 0.30
    position_agreement_weight: float = 0.25
    whole_pattern_similarity_weight: float = 0.15
    position_only_similarity_weight: float = 0.10
    matched_region_count_weight: float = 0.10
    absent_strong_peak_penalty_weight: float = 0.15
    unexplained_region_penalty_weight: float = 0.10
    shift_penalty_weight: float = 0.05
    chemical_score_weight: float = 0.12
    stoichiometric_score_weight: float = 0.08
    synthesis_context_score_weight: float = 0.05
    neutral_context_score: float = 0.5
    duplicate_formula_space_group_enabled: bool = True
    subset_element_bonus: float = 0.05
    intended_structure_bonus: float = 0.05


@dataclass(frozen=True)
class SinglePhaseRefinementSettings:
    maximum_single_phase_candidates_refined: int = 6
    background_model_type: str = "chebyschev-1"
    background_coefficient_count: int = 6
    refine_scale: bool = True
    refine_zero_shift: bool = True
    maximum_absolute_zero_shift_degrees: float = 0.5
    refine_sample_displacement: bool = False
    maximum_absolute_sample_displacement: float = 5.0
    refine_lattice_parameters: bool = True
    maximum_relative_lattice_parameter_change: float = 0.05
    allowed_profile_terms: tuple[str, ...] = ()
    profile_parameter_bounds: tuple[ProfileParameterBound, ...] = ()
    refine_crystallite_size: bool = False
    refine_microstrain: bool = False
    refine_preferred_orientation: bool = False
    refine_atomic_coordinates: bool = False
    refine_occupancies: bool = False
    refine_displacement_parameters: bool = False
    allow_generic_instrument_fallback: bool = False
    generic_instrument_label: str = "CuKa lab data"
    initial_stage_cycles: int = 3
    maximum_iterations: int = 6
    convergence_tolerance: float = 1e-4
    stage_failure_behavior: str = "stop_candidate_keep_best_valid_stage"
    residual_region_threshold_fraction_of_max: float = 0.10
    residual_region_min_points: int = 3
    residual_expected_reflection_window_degrees: float = 0.25
    unsupported_predicted_region_strong_fraction: float = 0.35
    unsupported_predicted_region_support_ratio_threshold: float = 0.25
    unsupported_predicted_region_window_degrees: float = 0.20

    def __post_init__(self) -> None:
        if self.maximum_single_phase_candidates_refined <= 0:
            raise ValueError("maximum_single_phase_candidates_refined must be positive")
        if self.background_coefficient_count <= 0:
            raise ValueError("background_coefficient_count must be positive")
        if self.maximum_iterations <= 0 or self.initial_stage_cycles <= 0:
            raise ValueError("refinement cycles must be positive")
        if self.refine_preferred_orientation:
            raise ValueError("Milestone 5 keeps preferred-orientation refinement disabled")
        if self.refine_atomic_coordinates:
            raise ValueError("Milestone 5 keeps atomic-coordinate refinement disabled")
        if self.refine_occupancies:
            raise ValueError("Milestone 5 keeps occupancy refinement disabled")
        if self.refine_displacement_parameters:
            raise ValueError("Milestone 5 keeps displacement-parameter refinement disabled")


@dataclass(frozen=True)
class TwoPhaseDecisionSettings:
    maximum_base_single_phase_hypotheses: int = 3
    maximum_secondary_candidates_per_base: int = 3
    maximum_total_two_phase_refinements: int = 6
    minimum_residual_region_strength_fraction: float = 0.20
    minimum_residual_region_count: int = 1
    preserve_intended_candidate_when_available: bool = True
    minimum_second_phase_scale_factor: float = 0.02
    minimum_supported_reflection_regions: int = 2
    minimum_partially_distinctive_regions: int = 1
    maximum_single_region_evidence_fraction: float = 0.70
    aic_threshold: float = 2.0
    aicc_threshold: float = 2.0
    bic_threshold: float = 2.0
    minimum_penalized_improvement: float = 2.0
    ambiguity_margin: float = 2.0
    maximum_stability_runs: int = 4
    background_coefficient_perturbation: int = 1
    residual_threshold_multipliers: tuple[float, ...] = (0.8, 1.2)
    scale_seed_multipliers: tuple[float, ...] = (0.8, 1.2)
    zero_shift_seed_delta: float = 0.02
    dominant_region_half_window_degrees: float = 0.20
    minimum_classification_agreement: float = 0.67
    minimum_scale_stability: float = 0.60
    minimum_score_stability: float = 0.60
    adequate_single_phase_max_residual_regions: int = 1
    adequate_single_phase_max_unsupported_regions: int = 2
    evidence_score_clip: float = 1.0

    def __post_init__(self) -> None:
        if self.maximum_base_single_phase_hypotheses <= 0:
            raise ValueError("maximum_base_single_phase_hypotheses must be positive")
        if self.maximum_secondary_candidates_per_base <= 0:
            raise ValueError("maximum_secondary_candidates_per_base must be positive")
        if self.maximum_total_two_phase_refinements <= 0:
            raise ValueError("maximum_total_two_phase_refinements must be positive")
        if self.minimum_supported_reflection_regions <= 0:
            raise ValueError("minimum_supported_reflection_regions must be positive")
        if not self.residual_threshold_multipliers:
            raise ValueError("residual_threshold_multipliers must not be empty")
        if not self.scale_seed_multipliers:
            raise ValueError("scale_seed_multipliers must not be empty")


@dataclass(frozen=True)
class XRDAnalysisConfig:
    algorithm_version: str
    configuration_version: str
    supported_phase_states: tuple[PhaseState, ...]
    maximum_crystalline_phases: int = 2
    warning_severities: tuple[WarningSeverityDefinition, ...] = ()
    pattern_processing: PatternProcessingSettings = PatternProcessingSettings()
    quality_control: QualityControlSettingsPlaceholder = QualityControlSettingsPlaceholder()
    candidate_ranking: CandidateRankingSettings = CandidateRankingSettings()
    single_phase_refinement: SinglePhaseRefinementSettings = SinglePhaseRefinementSettings()
    decision: TwoPhaseDecisionSettings = TwoPhaseDecisionSettings()

    def __post_init__(self) -> None:
        if self.maximum_crystalline_phases != 2:
            raise ValueError("Milestone 2 fixes maximum_crystalline_phases at 2")
        if self.supported_phase_states != ALLOWED_PHASE_STATES:
            raise ValueError("supported_phase_states must match the exact allowed phase labels")

    def canonical_payload(self) -> dict[str, Any]:
        return to_jsonable(self)

    def canonical_json(self) -> str:
        return json.dumps(self.canonical_payload(), sort_keys=True, separators=(",", ":"))

    def content_hash_input(self) -> str:
        return self.canonical_json()

    def content_hash(self) -> str:
        return hashlib.sha256(self.content_hash_input().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class XRDAnalysisResult:
    phase_state: PhaseState
    warnings: tuple[XRDAnalysisWarning, ...]
    algorithm_version: str
    configuration_version: str
    best_hypothesis: Optional[PhaseHypothesis]
    alternative_hypotheses: tuple[PhaseHypothesis, ...]
    evidence_score: Optional[float]
    provenance: XRDAnalysisProvenance
    parsed_pattern: Optional[ParsedPatternMetadata] = None
    quality_control: Optional[QualityControlPlaceholder] = None
    phase_candidates: tuple[RankedPhaseCandidate | PhaseCandidatePlaceholder, ...] = ()
    ranked_candidate_shortlist: tuple[RankedPhaseCandidate, ...] = ()
    successful_single_phase_hypotheses: tuple[SinglePhaseHypothesisResult, ...] = ()
    failed_single_phase_attempts: tuple[SinglePhaseHypothesisResult, ...] = ()
    successful_two_phase_hypotheses: tuple[TwoPhaseHypothesisResult, ...] = ()
    failed_two_phase_attempts: tuple[TwoPhaseHypothesisResult, ...] = ()
    best_single_phase_hypothesis: Optional[SinglePhaseHypothesisResult] = None
    best_two_phase_hypothesis: Optional[TwoPhaseHypothesisResult] = None
    selected_best_model: Optional[SelectedBestModel] = None
    model_comparison: Optional[ModelComparisonSummary] = None
    decision_criteria: Optional[DecisionCriteriaResult] = None
    stability_results: Optional[StabilityAssessmentResult] = None
    evidence_components: Optional[AnalysisEvidenceComponents] = None
    failure_codes: tuple[str, ...] = ()
    analysis_provenance_notes: tuple[str, ...] = ()
    analysis_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.phase_state not in ALLOWED_PHASE_STATES:
            raise ValueError(f"Unsupported phase_state: {self.phase_state}")


DEFAULT_XRD_ANALYSIS_CONFIG = XRDAnalysisConfig(
    algorithm_version="loop-xrd-phase-analysis-mvp-v5",
    configuration_version="loop-xrd-config-v5",
    supported_phase_states=ALLOWED_PHASE_STATES,
    warning_severities=(
        WarningSeverityDefinition("info", "Informational missing-or-derived metadata note."),
        WarningSeverityDefinition("warning", "Important metadata gap that does not block assembly."),
        WarningSeverityDefinition("error", "Blocking metadata issue for a later pipeline stage."),
    ),
)


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        result: dict[str, Any] = {}
        for item in fields(value):
            result[item.name] = to_jsonable(getattr(value, item.name))
        return result
    if isinstance(value, dict):
        return {str(key): to_jsonable(val) for key, val in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return value


def assemble_xrd_analysis_input(
    *,
    material: Any,
    recipe: Any,
    trial: Any,
    raw_file: Any | None = None,
    raw_file_path: str | None = None,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> XRDAnalysisInput:
    material_auid = _clean_text(_get_attr(material, "id") or _get_attr(material, "material_auid"))
    recipe_auid = _clean_text(_get_attr(recipe, "id") or _get_attr(recipe, "recipe_auid"))
    trial_id = _clean_text(_get_attr(trial, "trial_id"))
    if not material_auid:
        raise XRDAnalysisInputError("Material identity is required to assemble XRDAnalysisInput")
    if not recipe_auid:
        raise XRDAnalysisInputError("Recipe identity is required to assemble XRDAnalysisInput")
    if not trial_id:
        raise XRDAnalysisInputError("Trial identity is required to assemble XRDAnalysisInput")

    warnings: list[XRDAnalysisWarning] = []
    recipe_elements = _as_dict(_get_attr(recipe, "elements"))
    material_elements = _as_dict(_get_attr(material, "elements"))
    composition_map = material_elements or recipe_elements
    if not composition_map:
        warnings.append(_warning("missing_nominal_composition", "nominal_composition", "Nominal composition is missing from the repository records."))
        warnings.append(_warning("missing_element_list", "elements", "Element list is missing from the repository records."))

    elements, stoichiometric_amounts = _normalize_stoichiometry(composition_map)
    if material_elements and recipe_elements and _normalized_stoich_map(material_elements) != _normalized_stoich_map(recipe_elements):
        warnings.append(
            _warning(
                "inconsistent_stoichiometry",
                "stoichiometric_amounts",
                "Material and recipe stoichiometric payloads are inconsistent.",
            )
        )

    nominal_composition = _formula_from_stoichiometry(stoichiometric_amounts) if stoichiometric_amounts else None

    structure_family = _clean_text(_get_attr(material, "structure_family") or _get_attr(recipe, "structure_family"))
    expected_space_group = _normalize_nullable_token(_get_attr(trial, "spacegroup"))
    expected_site_assignments = _normalize_site_assignments(_as_dict(_get_attr(trial, "element_sites")))

    exp_condition = _get_attr(trial, "exp_condition")
    additional_params = _as_dict(_get_attr(exp_condition, "additional_params"))
    raw_file_hash = _clean_text(
        _get_attr(trial, "file_hash")
        or additional_params.get("file_hash")
        or _get_attr(raw_file, "file_hash")
        or _get_attr(raw_file, "id")
    )
    if not raw_file_hash:
        warnings.append(_warning("missing_raw_file_hash", "raw_file_hash", "Raw-file hash is missing."))

    measurement_metadata = _normalize_metadata_items(
        additional_params.get("xrd_metadata") or additional_params.get("diffraction_metadata") or []
    )
    measurement_step = _find_measurement_step(additional_params, recipe)
    raw_reference = _resolve_raw_file_reference(
        recipe_auid=recipe_auid,
        trial_id=trial_id,
        trial=trial,
        raw_file=raw_file,
        raw_file_path=raw_file_path,
    )
    if raw_reference is None:
        if not raw_file_hash:
            raise XRDAnalysisInputError(
                f"Unable to assemble XRDAnalysisInput for {recipe_auid}/{trial_id}: no raw-file hash or reference is available."
            )
        warnings.append(
            _warning(
                "missing_raw_file_reference",
                "raw_file_reference",
                "No repository-approved raw-file reference is available.",
            )
        )

    radiation_source = _extract_radiation_source(measurement_step, measurement_metadata)
    wavelength_angstrom = _extract_wavelength(measurement_step, measurement_metadata, radiation_source)
    if wavelength_angstrom is None:
        warnings.append(
            _warning(
                "missing_wavelength",
                "wavelength_angstrom",
                "Wavelength is missing and could not be derived from a usable radiation source.",
            )
        )

    coordinate_column = _extract_metadata_value(
        measurement_metadata,
        "coordinate column",
        "x column",
    )
    intensity_column = _extract_metadata_value(
        measurement_metadata,
        "intensity column",
        "y column",
    )
    coordinate_type = _extract_coordinate_type(measurement_step, measurement_metadata, coordinate_column)
    if not coordinate_column:
        warnings.append(
            _warning(
                "missing_coordinate_column",
                "coordinate_column",
                "Coordinate column information is missing from the repository metadata.",
            )
        )
    if not intensity_column:
        warnings.append(
            _warning(
                "missing_intensity_column",
                "intensity_column",
                "Intensity column information is missing from the repository metadata.",
            )
        )

    scan_min, scan_max = _extract_scan_range(measurement_step, measurement_metadata)
    if scan_min is None or scan_max is None:
        warnings.append(
            _warning(
                "missing_scan_range",
                "scan_min",
                "Scan range metadata is missing or incomplete.",
            )
        )
    step_size = _coerce_float(
        _first_non_empty(
            _value_from_step(measurement_step, "step_size_deg"),
            _extract_metadata_value(measurement_metadata, "step size", "step size deg", "step width"),
        )
    )
    scan_speed = _coerce_float(
        _first_non_empty(
            _value_from_step(measurement_step, "scan_speed_deg_min"),
            _extract_metadata_value(measurement_metadata, "scan speed", "scan speed deg/min", "speed"),
        )
    )

    instrument_profile = _extract_instrument_profile(measurement_metadata)
    if instrument_profile is None:
        warnings.append(
            _warning(
                "missing_instrument_profile",
                "instrument_profile",
                "Instrument profile metadata is missing.",
            )
        )

    synthesis_context = _build_synthesis_context(additional_params, recipe)
    provenance = XRDAnalysisProvenance(
        source_material=_sanitized_snapshot(material),
        source_recipe=_sanitized_snapshot(recipe),
        source_trial=_sanitized_snapshot(trial, omit_keys={"phase_status", "success", "is_single_phase", "phases_detected"}),
        source_raw_file=_sanitized_snapshot(raw_file) if raw_file is not None else None,
        measurement_metadata=measurement_metadata,
        assembly_notes=tuple(_build_assembly_notes(raw_reference, measurement_step, measurement_metadata)),
    )

    return XRDAnalysisInput(
        material_auid=material_auid,
        recipe_auid=recipe_auid,
        trial_id=trial_id,
        raw_file_hash=raw_file_hash,
        raw_file_reference=raw_reference,
        nominal_composition=nominal_composition,
        elements=elements,
        stoichiometric_amounts=stoichiometric_amounts,
        structure_family=structure_family,
        expected_space_group=expected_space_group,
        expected_site_assignments=expected_site_assignments,
        radiation_source=radiation_source,
        wavelength_angstrom=wavelength_angstrom,
        coordinate_type=coordinate_type,
        coordinate_column=coordinate_column,
        intensity_column=intensity_column,
        scan_min=scan_min,
        scan_max=scan_max,
        step_size=step_size,
        scan_speed=scan_speed,
        instrument_profile=instrument_profile,
        synthesis_context=synthesis_context,
        warnings=tuple(warnings),
        algorithm_version=configuration.algorithm_version,
        configuration_version=configuration.configuration_version,
        provenance=provenance,
    )


def validate_xrd_analysis_input(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> XRDAnalysisInput:
    if not analysis_input.material_auid:
        raise XRDAnalysisInputError("material_auid is required")
    if not analysis_input.recipe_auid:
        raise XRDAnalysisInputError("recipe_auid is required")
    if not analysis_input.trial_id:
        raise XRDAnalysisInputError("trial_id is required")
    if analysis_input.raw_file_hash is None and analysis_input.raw_file_reference is None:
        raise XRDAnalysisInputError("A raw-file hash or repository-approved raw-file reference is required")
    if analysis_input.configuration_version != configuration.configuration_version:
        raise XRDAnalysisInputError(
            "configuration_version does not match the active XRD analysis configuration"
        )
    if analysis_input.algorithm_version != configuration.algorithm_version:
        raise XRDAnalysisInputError(
            "algorithm_version does not match the active XRD analysis configuration"
        )
    return analysis_input


def _warning(code: str, field_name: str, message: str) -> XRDAnalysisWarning:
    return XRDAnalysisWarning(
        code=code,
        message=message,
        severity="warning",
        field=field_name,
        stage="input_assembly",
    )


def _get_attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, Mapping):
        return dict(value.items())
    return {}


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_nullable_token(value: Any) -> Optional[str]:
    text = _clean_text(value)
    if text is None:
        return None
    if text.strip().lower() in _MISSING_TOKENS:
        return None
    return text


def _coerce_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_stoichiometry(elements: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[StoichiometricAmount, ...]]:
    normalized: list[StoichiometricAmount] = []
    for element, amount in sorted((elements or {}).items(), key=lambda item: str(item[0])):
        element_name = _clean_text(element)
        amount_value = _coerce_float(amount)
        if not element_name or amount_value is None:
            continue
        normalized.append(StoichiometricAmount(element=element_name, amount=amount_value))
    return tuple(item.element for item in normalized), tuple(normalized)


def _normalized_stoich_map(elements: Mapping[str, Any]) -> dict[str, float]:
    return {item.element: item.amount for item in _normalize_stoichiometry(elements)[1]}


def _format_amount(value: float) -> str:
    if float(value).is_integer():
        if int(value) == 1:
            return ""
        return str(int(value))
    return f"{value:.8g}"


def _formula_from_stoichiometry(amounts: tuple[StoichiometricAmount, ...]) -> Optional[str]:
    if not amounts:
        return None
    return "".join(f"{item.element}{_format_amount(item.amount)}" for item in amounts)


def _normalize_site_assignments(raw_sites: Mapping[str, Any]) -> tuple[ExpectedSiteAssignment, ...]:
    assignments: list[ExpectedSiteAssignment] = []
    for element, site in sorted((raw_sites or {}).items(), key=lambda item: str(item[0])):
        element_name = _clean_text(element)
        site_label = _normalize_nullable_token(site)
        if element_name and site_label:
            assignments.append(ExpectedSiteAssignment(element=element_name, site_label=site_label))
    return tuple(assignments)


def _normalize_metadata_items(raw_metadata: Any) -> tuple[MetadataItem, ...]:
    items: list[MetadataItem] = []
    for entry in raw_metadata or []:
        key: Optional[str] = None
        value: Optional[str] = None
        if isinstance(entry, (list, tuple)) and entry:
            key = _clean_text(entry[0])
            value = _clean_text(entry[1]) if len(entry) > 1 else None
        elif isinstance(entry, Mapping):
            key = _clean_text(entry.get("key") or entry.get("name") or entry.get("field"))
            value = _clean_text(entry.get("value"))
        else:
            key = _clean_text(entry)
        if key:
            items.append(MetadataItem(key=key, value=value))
    return tuple(items)


def _find_measurement_step(additional_params: Mapping[str, Any], recipe: Any) -> Optional[dict[str, Any]]:
    step_sources = additional_params.get("synthesis_steps")
    if not isinstance(step_sources, list):
        step_sources = _get_attr(recipe, "synthesis_steps")
    for step in step_sources or []:
        if isinstance(step, Mapping) and str(step.get("step_type") or "").strip().lower() == "xrd_measurement":
            return dict(step)
    return None


def _resolve_raw_file_reference(
    *,
    recipe_auid: str,
    trial_id: str,
    trial: Any,
    raw_file: Any | None,
    raw_file_path: str | None,
) -> Optional[RawFileReference]:
    if _clean_text(_get_attr(raw_file, "stored_path")):
        return RawFileReference(
            reference_kind="raw_db_path",
            locator=str(_get_attr(raw_file, "stored_path")),
            original_filename=_clean_text(_get_attr(raw_file, "original_filename")),
            content_type=_clean_text(_get_attr(raw_file, "content_type")),
            size_bytes=_get_attr(raw_file, "size_bytes"),
        )
    if _clean_text(raw_file_path):
        return RawFileReference(reference_kind="stored_path", locator=str(raw_file_path))
    resolved_path = xrd_store.resolve_raw_path(recipe_auid, trial_id)
    if resolved_path:
        return RawFileReference(reference_kind="resolved_path", locator=resolved_path)
    raw_data_link = _clean_text(_get_attr(trial, "raw_data_link"))
    if raw_data_link:
        return RawFileReference(reference_kind="raw_data_link", locator=raw_data_link)
    return None


def _extract_radiation_source(
    measurement_step: Optional[Mapping[str, Any]],
    metadata_items: tuple[MetadataItem, ...],
) -> Optional[str]:
    step_value = _normalize_nullable_token(_value_from_step(measurement_step, "radiation"))
    if step_value:
        return step_value
    return _extract_metadata_value(metadata_items, "radiation", "source", "anode material")


def _extract_wavelength(
    measurement_step: Optional[Mapping[str, Any]],
    metadata_items: tuple[MetadataItem, ...],
    radiation_source: Optional[str],
) -> Optional[float]:
    for item in metadata_items:
        key = item.key.lower()
        if "wavelength" in key:
            parsed = _coerce_float(item.value)
            if parsed is not None:
                return parsed
    normalized_source = _normalize_radiation_source(radiation_source)
    if normalized_source:
        return _RADIATION_WAVELENGTHS.get(normalized_source)
    return None


def _normalize_radiation_source(value: Optional[str]) -> Optional[str]:
    text = _normalize_nullable_token(value)
    if text is None:
        return None
    return text.lower().replace("-", "_")


def _extract_metadata_value(metadata_items: tuple[MetadataItem, ...], *needles: str) -> Optional[str]:
    lowered = tuple(token.lower() for token in needles)
    for item in metadata_items:
        key_lower = item.key.lower()
        if any(needle in key_lower for needle in lowered):
            value = _normalize_nullable_token(item.value)
            if value is not None:
                return value
    return None


def _extract_coordinate_type(
    measurement_step: Optional[Mapping[str, Any]],
    metadata_items: tuple[MetadataItem, ...],
    coordinate_column: Optional[str],
) -> Optional[CoordinateType]:
    range_hint = _normalize_nullable_token(_value_from_step(measurement_step, "two_theta_range"))
    if range_hint:
        return "two_theta"
    column_hint = (coordinate_column or "").lower()
    if "angle" in column_hint or "theta" in column_hint:
        return "two_theta"
    if "d" == column_hint or "d-spacing" in column_hint or "spacing" in column_hint:
        return "d_spacing"
    if column_hint == "q" or "reciprocal" in column_hint:
        return "q"
    metadata_hint = _extract_metadata_value(metadata_items, "coordinate type")
    if metadata_hint:
        lowered = metadata_hint.lower()
        if "theta" in lowered or "angle" in lowered:
            return "two_theta"
        if "spacing" in lowered:
            return "d_spacing"
        if lowered == "q":
            return "q"
    return None


def _extract_scan_range(
    measurement_step: Optional[Mapping[str, Any]],
    metadata_items: tuple[MetadataItem, ...],
) -> tuple[Optional[float], Optional[float]]:
    raw_range = _normalize_nullable_token(_value_from_step(measurement_step, "two_theta_range"))
    if raw_range:
        parsed = _parse_range(raw_range)
        if parsed != (None, None):
            return parsed
    start = _coerce_float(_extract_metadata_value(metadata_items, "start", "scan min", "minimum angle"))
    stop = _coerce_float(_extract_metadata_value(metadata_items, "stop", "scan max", "maximum angle"))
    if start is not None and stop is not None:
        return start, stop
    combined = _extract_metadata_value(metadata_items, "range", "two theta range")
    if combined:
        return _parse_range(combined)
    return None, None


def _parse_range(value: str) -> tuple[Optional[float], Optional[float]]:
    match = _SCAN_RANGE_RE.search(value)
    if not match:
        return None, None
    start = _coerce_float(match.group(1))
    stop = _coerce_float(match.group(2))
    return start, stop


def _extract_instrument_profile(metadata_items: tuple[MetadataItem, ...]) -> Optional[InstrumentProfile]:
    instrument_label = _extract_metadata_value(metadata_items, "instrument profile", "instrument label", "instrument")
    instrument_parameter_path = _extract_metadata_value(metadata_items, "instprm", "instrument parameter")
    geometry = _extract_metadata_value(metadata_items, "geometry")
    sample_holder = _extract_metadata_value(metadata_items, "sample holder")
    relevant_items = tuple(
        item
        for item in metadata_items
        if any(
            token in item.key.lower()
            for token in ("instrument", "profile", "instprm", "geometry", "holder")
        )
    )
    if not any((instrument_label, instrument_parameter_path, geometry, sample_holder, relevant_items)):
        return None
    return InstrumentProfile(
        instrument_label=instrument_label,
        instrument_parameter_path=instrument_parameter_path,
        geometry=geometry,
        sample_holder=sample_holder,
        metadata_items=relevant_items,
    )


def _build_synthesis_context(additional_params: Mapping[str, Any], recipe: Any) -> SynthesisContext:
    raw_steps = additional_params.get("synthesis_steps")
    if not isinstance(raw_steps, list):
        raw_steps = _get_attr(recipe, "synthesis_steps") or []

    ordered_steps: list[SynthesisStepRecord] = []
    precursor_records: list[PrecursorRecord] = []
    temperatures: list[float] = []
    ramp_rates: list[float] = []
    hold_times_hours: list[float] = []
    atmospheres: list[str] = []
    furnace_types: list[str] = []
    preparation_notes: list[str] = []

    for idx, step in enumerate(raw_steps or [], start=1):
        if not isinstance(step, Mapping):
            continue
        step_number = _coerce_int(step.get("step_number")) or idx
        step_type = _clean_text(step.get("step_type")) or "other"
        notes = _normalize_nullable_token(step.get("notes"))
        atmosphere = _normalize_nullable_token(step.get("atmosphere"))
        furnace_type = _normalize_nullable_token(step.get("furnace_type"))
        temperature_c = _coerce_float(step.get("temperature_c"))
        max_temp_c = _coerce_float(step.get("max_temp_c"))
        ramp_rate_c_min = _coerce_float(step.get("ramp_rate_c_min"))
        hold_time_hours = _coerce_float(step.get("hold_time_hours"))
        hold_time_min = _coerce_float(step.get("hold_time_min"))
        scan_speed_deg_min = _coerce_float(step.get("scan_speed_deg_min"))
        step_size_deg = _coerce_float(step.get("step_size_deg"))
        radiation = _normalize_nullable_token(step.get("radiation"))
        two_theta_range = _normalize_nullable_token(step.get("two_theta_range"))
        step_precursors = _normalize_precursors(step.get("precursors_list"), step.get("precursors"))
        precursor_records.extend(step_precursors)

        if notes:
            preparation_notes.append(notes)
        if atmosphere:
            atmospheres.append(atmosphere)
        if furnace_type:
            furnace_types.append(furnace_type)
        if temperature_c is not None:
            temperatures.append(temperature_c)
        if max_temp_c is not None:
            temperatures.append(max_temp_c)
        if ramp_rate_c_min is not None:
            ramp_rates.append(ramp_rate_c_min)
        if hold_time_hours is not None:
            hold_times_hours.append(hold_time_hours)
        if hold_time_min is not None:
            hold_times_hours.append(hold_time_min / 60.0)

        extra_fields: dict[str, Any] = {}
        for key, value in dict(step).items():
            if key in {
                "step_number",
                "step_type",
                "notes",
                "atmosphere",
                "furnace_type",
                "temperature_c",
                "max_temp_c",
                "ramp_rate_c_min",
                "hold_time_hours",
                "hold_time_min",
                "scan_speed_deg_min",
                "step_size_deg",
                "radiation",
                "two_theta_range",
                "precursors_list",
                "precursors",
            }:
                continue
            extra_fields[str(key)] = to_jsonable(value)

        ordered_steps.append(
            SynthesisStepRecord(
                step_number=step_number,
                step_type=step_type,
                notes=notes,
                atmosphere=atmosphere,
                furnace_type=furnace_type,
                temperature_c=temperature_c,
                max_temp_c=max_temp_c,
                ramp_rate_c_min=ramp_rate_c_min,
                hold_time_hours=hold_time_hours,
                hold_time_min=hold_time_min,
                scan_speed_deg_min=scan_speed_deg_min,
                step_size_deg=step_size_deg,
                radiation=radiation,
                two_theta_range=two_theta_range,
                precursors=tuple(step_precursors),
                extra_fields=extra_fields,
            )
        )

    return SynthesisContext(
        ordered_steps=tuple(ordered_steps),
        precursor_records=tuple(precursor_records),
        temperatures_c=tuple(temperatures),
        ramp_rates_c_min=tuple(ramp_rates),
        hold_times_hours=tuple(hold_times_hours),
        atmospheres=tuple(atmospheres),
        furnace_types=tuple(furnace_types),
        preparation_notes=tuple(preparation_notes),
    )


def _normalize_precursors(raw_list: Any, legacy_text: Any) -> list[PrecursorRecord]:
    precursors: list[PrecursorRecord] = []
    if isinstance(raw_list, list):
        for entry in raw_list:
            if not isinstance(entry, Mapping):
                continue
            precursors.append(
                PrecursorRecord(
                    name=_normalize_nullable_token(entry.get("name")),
                    formula=_normalize_nullable_token(entry.get("formula")),
                    cas_number=_normalize_nullable_token(entry.get("cas_number")),
                    purity=_normalize_nullable_token(entry.get("purity")),
                    supplier=_normalize_nullable_token(entry.get("supplier")),
                    notes=_normalize_nullable_token(entry.get("notes")),
                )
            )
    elif _normalize_nullable_token(legacy_text):
        precursors.append(PrecursorRecord(name=_normalize_nullable_token(legacy_text)))
    return precursors


def _build_assembly_notes(
    raw_reference: Optional[RawFileReference],
    measurement_step: Optional[Mapping[str, Any]],
    measurement_metadata: tuple[MetadataItem, ...],
) -> Iterable[str]:
    if raw_reference is not None:
        yield f"raw_file_reference:{raw_reference.reference_kind}"
    if measurement_step is not None:
        yield "measurement_source:xrd_measurement_step"
    if measurement_metadata:
        yield "measurement_source:xrd_metadata"


def _sanitized_snapshot(obj: Any, *, omit_keys: Optional[set[str]] = None) -> dict[str, Any]:
    omit = omit_keys or set()
    payload = _plain_snapshot(obj)
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): to_jsonable(value)
        for key, value in sorted(payload.items(), key=lambda item: str(item[0]))
        if key not in omit
    }


def _plain_snapshot(obj: Any) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return {str(key): _plain_snapshot(value) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_plain_snapshot(item) for item in obj]
    if isinstance(obj, tuple):
        return [_plain_snapshot(item) for item in obj]
    to_mongo = getattr(obj, "to_mongo", None)
    if callable(to_mongo):
        raw = to_mongo()
        if hasattr(raw, "to_dict"):
            return _plain_snapshot(raw.to_dict())
        return _plain_snapshot(raw)
    return obj


def _value_from_step(step: Optional[Mapping[str, Any]], key: str) -> Any:
    if not step:
        return None
    return step.get(key)


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
