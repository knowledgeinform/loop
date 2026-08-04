from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from scipy.signal import find_peaks

from catalog.gsas_runtime import cleanup_paths, configure_gsas, new_project, prepare_project_path, resolve_instrument_parameter_file, write_temp_xye
from catalog.rietveld_refinement import _parse_formula, _phase_composition_from_cif

from .reporting import dumps_canonical_json
from .schemas import (
    CANDIDATE_WARNING_CODES,
    DEFAULT_XRD_ANALYSIS_CONFIG,
    CandidateFailureReason,
    CandidateGenerationResult,
    CandidateSimulation,
    CoordinateType,
    LinkedStructureReference,
    ParsedPatternMetadata,
    PatternQualityControlResult,
    RankedPhaseCandidate,
    ReferencePhaseManifestEntry,
    ReferencePhaseSnapshot,
    StoichiometricAmount,
    XRDAnalysisConfig,
    XRDAnalysisInput,
    XRDAnalysisWarning,
)

REFERENCE_PHASES_DIR = Path(__file__).resolve().parent.parent / "reference_phases"
REFERENCE_MANIFEST_PATH = REFERENCE_PHASES_DIR / "manifest.json"
_ELEMENT_RE = re.compile(r"[A-Z][a-z]?")
_PERIODIC_TABLE = (
    "H", "He",
    "Li", "Be", "B", "C", "N", "O", "F", "Ne",
    "Na", "Mg", "Al", "Si", "P", "S", "Cl", "Ar",
    "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni", "Cu", "Zn",
    "Ga", "Ge", "As", "Se", "Br", "Kr",
    "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "In", "Sn", "Sb", "Te", "I", "Xe",
    "Cs", "Ba", "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy",
    "Ho", "Er", "Tm", "Yb", "Lu",
    "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
    "Tl", "Pb", "Bi", "Po", "At", "Rn",
    "Fr", "Ra", "Ac", "Th", "Pa", "U", "Np", "Pu", "Am", "Cm", "Bk", "Cf",
    "Es", "Fm", "Md", "No", "Lr",
    "Rf", "Db", "Sg", "Bh", "Hs", "Mt", "Ds", "Rg", "Cn", "Nh", "Fl", "Mc",
    "Lv", "Ts", "Og",
)
_ATOMIC_NUMBERS = {symbol: index for index, symbol in enumerate(_PERIODIC_TABLE, start=1)}


class CandidateGenerationError(RuntimeError):
    """Base class for deterministic Milestone 4 candidate-stage failures."""


class ReferenceSnapshotMismatchError(CandidateGenerationError):
    """Raised when a curated reference CIF hash no longer matches the manifest."""


class CandidateSimulationError(CandidateGenerationError):
    """Raised when a candidate CIF cannot be simulated into a screening pattern."""


def load_reference_phase_snapshot(
    manifest_path: str | Path | None = None,
    *,
    validate_hashes: bool = True,
) -> ReferencePhaseSnapshot:
    manifest_file = Path(manifest_path or REFERENCE_MANIFEST_PATH).expanduser().resolve()
    payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    entries: list[ReferencePhaseManifestEntry] = []
    for raw_entry in payload.get("entries", []):
        entries.append(
            ReferencePhaseManifestEntry(
                candidate_identifier=str(raw_entry["candidate_identifier"]),
                relative_cif_path=str(raw_entry["relative_cif_path"]),
                sha256=str(raw_entry["sha256"]),
                formula=_clean_text(raw_entry.get("formula")),
                element_set=tuple(sorted(_normalize_element_set(raw_entry.get("element_set") or ()))),
                space_group=_normalize_space_group(raw_entry.get("space_group")),
                structure_family=_clean_text(raw_entry.get("structure_family")),
                source=str(raw_entry.get("source") or "curated_reference"),
                source_identifier=_clean_text(raw_entry.get("source_identifier")),
                notes=tuple(str(item) for item in (raw_entry.get("notes") or ())),
                enabled=bool(raw_entry.get("enabled", True)),
            )
        )
    canonical_payload = {
        "snapshot_version": payload.get("snapshot_version"),
        "entries": json.loads(dumps_canonical_json(entries)),
    }
    snapshot_hash = hashlib.sha256(
        json.dumps(canonical_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    snapshot = ReferencePhaseSnapshot(
        snapshot_version=str(payload.get("snapshot_version") or "reference-phases-v1"),
        snapshot_hash=snapshot_hash,
        manifest_path=str(manifest_file),
        entries=tuple(entries),
        notes=tuple(str(item) for item in (payload.get("notes") or ())),
    )
    if validate_hashes:
        validate_reference_phase_snapshot(snapshot)
    return snapshot


def validate_reference_phase_snapshot(snapshot: ReferencePhaseSnapshot) -> None:
    manifest_dir = Path(snapshot.manifest_path).parent
    for entry in snapshot.entries:
        if not entry.enabled:
            continue
        cif_path = (manifest_dir / entry.relative_cif_path).resolve()
        if not cif_path.exists():
            raise ReferenceSnapshotMismatchError(f"Missing curated reference CIF: {entry.relative_cif_path}")
        actual_hash = hashlib.sha256(cif_path.read_bytes()).hexdigest()
        if actual_hash != entry.sha256:
            raise ReferenceSnapshotMismatchError(
                f"Reference snapshot mismatch for {entry.relative_cif_path}: expected {entry.sha256}, got {actual_hash}"
            )


def build_ranked_phase_candidates(
    analysis_input: XRDAnalysisInput,
    parsed_pattern: ParsedPatternMetadata,
    quality_control: PatternQualityControlResult,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
    reference_snapshot: ReferencePhaseSnapshot | None = None,
) -> CandidateGenerationResult:
    warnings: list[XRDAnalysisWarning] = []
    snapshot = reference_snapshot
    if snapshot is None:
        try:
            snapshot = load_reference_phase_snapshot()
        except ReferenceSnapshotMismatchError as exc:
            return CandidateGenerationResult(
                status="candidate generation failed",
                failure_reason="no_candidate_sources_available",
                warnings=(
                    _candidate_warning(
                        "reference_snapshot_mismatch",
                        "candidate_generation",
                        str(exc),
                    ),
                ),
                reference_snapshot=None,
                candidates=(),
                algorithm_version=analysis_input.algorithm_version,
                configuration_version=analysis_input.configuration_version,
                provenance=("reference_snapshot_mismatch",),
            )

    raw_candidates = _build_candidate_pool(analysis_input, snapshot, warnings)
    if not raw_candidates:
        warnings.append(
            _candidate_warning(
                "no_candidate_sources_available",
                "candidate_generation",
                "No candidate sources were available for the accepted pattern.",
            )
        )
        return CandidateGenerationResult(
            status="candidate generation failed",
            failure_reason="no_candidate_sources_available",
            warnings=tuple(warnings),
            reference_snapshot=snapshot,
            candidates=(),
            algorithm_version=analysis_input.algorithm_version,
            configuration_version=analysis_input.configuration_version,
            provenance=("no_candidate_sources_available",),
        )

    compatible, exclusion_warnings = _apply_chemistry_filter(
        raw_candidates,
        analysis_input,
        configuration=configuration,
    )
    warnings.extend(exclusion_warnings)
    if not compatible:
        warnings.append(
            _candidate_warning(
                "no_chemically_compatible_candidates",
                "elements",
                "All available candidates were excluded by the sample element set.",
            )
        )
        return CandidateGenerationResult(
            status="candidate generation failed",
            failure_reason="no_chemically_compatible_candidates",
            warnings=tuple(warnings),
            reference_snapshot=snapshot,
            candidates=(),
            algorithm_version=analysis_input.algorithm_version,
            configuration_version=analysis_input.configuration_version,
            provenance=("no_chemically_compatible_candidates",),
        )

    pre_scored = [
        _with_soft_scores(candidate, analysis_input, configuration=configuration)
        for candidate in compatible
    ]
    pre_scored.sort(
        key=lambda candidate: (
            candidate.intended_structure_match,
            candidate.chemical_compatibility_score,
            candidate.stoichiometric_similarity_score,
            candidate.synthesis_context_score,
            candidate.source,
            candidate.candidate_id,
        ),
        reverse=True,
    )
    limited = pre_scored[: configuration.candidate_ranking.maximum_candidates_before_simulation]
    clustered, duplicate_warnings = _cluster_duplicate_candidates(
        limited,
        configuration=configuration,
    )
    warnings.extend(duplicate_warnings)
    simulated = _simulate_candidates(
        clustered[: configuration.candidate_ranking.maximum_simulated_candidates],
        analysis_input,
        parsed_pattern,
        configuration=configuration,
    )
    simulatable = [candidate for candidate in simulated if candidate.simulation is not None]
    warnings.extend(
        warning
        for candidate in simulated
        for warning in candidate.warnings
        if warning.code in CANDIDATE_WARNING_CODES
    )
    if not simulatable:
        warnings.append(
            _candidate_warning(
                "no_simulatable_candidates",
                "raw_file_reference",
                "No chemically compatible candidates could be simulated over the measured range.",
            )
        )
        return CandidateGenerationResult(
            status="candidate generation failed",
            failure_reason="no_simulatable_candidates",
            warnings=tuple(_dedupe_warnings(warnings)),
            reference_snapshot=snapshot,
            candidates=tuple(simulated),
            algorithm_version=analysis_input.algorithm_version,
            configuration_version=analysis_input.configuration_version,
            provenance=("no_simulatable_candidates",),
        )

    ranked = [
        _with_diffraction_score(
            candidate,
            parsed_pattern,
            quality_control,
            configuration=configuration,
        )
        for candidate in simulatable
    ]
    ranked = [candidate for candidate in ranked if candidate.diffraction_pre_rank_score > 0.0 or candidate.intended_structure_match]
    if not ranked:
        warnings.append(
            _candidate_warning(
                "no_rankable_candidates",
                "candidate_generation",
                "No candidate produced a usable preliminary diffraction ranking.",
            )
        )
        return CandidateGenerationResult(
            status="candidate generation failed",
            failure_reason="no_rankable_candidates",
            warnings=tuple(_dedupe_warnings(warnings)),
            reference_snapshot=snapshot,
            candidates=(),
            algorithm_version=analysis_input.algorithm_version,
            configuration_version=analysis_input.configuration_version,
            provenance=("no_rankable_candidates",),
        )

    selected = _select_top_candidates(
        ranked,
        configuration=configuration,
    )
    return CandidateGenerationResult(
        status="candidate ranking ready for refinement",
        failure_reason=None,
        warnings=tuple(_dedupe_warnings(warnings)),
        reference_snapshot=snapshot,
        candidates=tuple(selected),
        algorithm_version=analysis_input.algorithm_version,
        configuration_version=analysis_input.configuration_version,
        provenance=(
            "candidate_pool_built",
            "chemistry_filtered",
            "duplicates_clustered",
            "screening_patterns_simulated",
            "preliminary_diffraction_ranking_complete",
        ),
    )


def _build_candidate_pool(
    analysis_input: XRDAnalysisInput,
    snapshot: ReferencePhaseSnapshot,
    warnings: list[XRDAnalysisWarning],
) -> list[RankedPhaseCandidate]:
    candidates: list[RankedPhaseCandidate] = []
    intended = _build_intended_structure_candidate(analysis_input, snapshot, warnings)
    if intended is not None:
        candidates.append(intended)

    manifest_dir = Path(snapshot.manifest_path).parent
    for entry in snapshot.entries:
        if not entry.enabled:
            continue
        cif_path = str((manifest_dir / entry.relative_cif_path).resolve())
        composition = _normalize_formula_to_amounts(entry.formula)
        candidate_warnings = ()
        if entry.formula and not composition:
            candidate_warnings = (
                _candidate_warning(
                    "candidate_formula_unparseable",
                    "formula",
                    f"Candidate {entry.candidate_identifier} has an unparseable formula: {entry.formula}.",
                ),
            )
        candidates.append(
            RankedPhaseCandidate(
                candidate_id=entry.candidate_identifier,
                source="curated_reference",
                source_identifier=entry.source_identifier or entry.candidate_identifier,
                source_snapshot=snapshot.snapshot_hash,
                cif_path=cif_path,
                cif_hash=entry.sha256,
                formula=entry.formula,
                normalized_composition=composition,
                element_set=entry.element_set,
                space_group=entry.space_group,
                structure_family=entry.structure_family,
                intended_structure_match=intended is not None and intended.candidate_id == entry.candidate_identifier,
                chemical_compatibility_score=0.0,
                stoichiometric_similarity_score=0.5,
                synthesis_context_score=0.5,
                diffraction_pre_rank_score=0.0,
                combined_pre_rank_score=0.0,
                duplicate_cluster_id=None,
                warnings=candidate_warnings,
                provenance=(
                    f"source=curated_reference",
                    f"relative_cif_path={entry.relative_cif_path}",
                ),
            )
        )

    for linked in analysis_input.linked_structure_references:
        if not linked.cif_path:
            continue
        formula = linked.formula
        composition = _normalize_formula_to_amounts(formula)
        candidate_warnings = ()
        if formula and not composition:
            candidate_warnings = (
                _candidate_warning(
                    "candidate_formula_unparseable",
                    "formula",
                    f"Candidate {linked.reference_id} has an unparseable formula: {formula}.",
                ),
            )
        candidates.append(
            RankedPhaseCandidate(
                candidate_id=f"{linked.source_kind}:{linked.reference_id}",
                source=_linked_source_name(linked),
                source_identifier=linked.source_identifier or linked.reference_id,
                source_snapshot=None,
                cif_path=linked.cif_path,
                cif_hash=_safe_hash_file(linked.cif_path),
                formula=formula,
                normalized_composition=composition,
                element_set=tuple(item.element for item in composition),
                space_group=_normalize_space_group(linked.space_group),
                structure_family=_clean_text(linked.structure_family),
                intended_structure_match=False,
                chemical_compatibility_score=0.0,
                stoichiometric_similarity_score=0.5,
                synthesis_context_score=0.5,
                diffraction_pre_rank_score=0.0,
                combined_pre_rank_score=0.0,
                duplicate_cluster_id=None,
                warnings=candidate_warnings,
                provenance=(f"linked_source={linked.source_kind}",),
            )
        )
    return _dedupe_exact_candidate_ids(candidates)


def _build_intended_structure_candidate(
    analysis_input: XRDAnalysisInput,
    snapshot: ReferencePhaseSnapshot,
    warnings: list[XRDAnalysisWarning],
) -> RankedPhaseCandidate | None:
    if not analysis_input.structure_family:
        return None
    target_space_group = _normalize_space_group(analysis_input.expected_space_group)
    target_elements = set(_normalize_element_set(analysis_input.elements))
    exact_matches = [
        entry for entry in snapshot.entries
        if entry.enabled
        and _clean_text(entry.structure_family) == analysis_input.structure_family
        and set(entry.element_set) == target_elements
        and (target_space_group is None or _normalize_space_group(entry.space_group) == target_space_group)
    ]
    if not exact_matches:
        warnings.append(
            _candidate_warning(
                "intended_structure_reference_missing",
                "structure_family",
                "No validated intended-structure reference CIF was found for the supplied target structure fields.",
            )
        )
        return None
    entry = sorted(exact_matches, key=lambda item: (item.candidate_identifier, item.relative_cif_path))[0]
    manifest_dir = Path(snapshot.manifest_path).parent
    composition = _normalize_formula_to_amounts(entry.formula)
    return RankedPhaseCandidate(
        candidate_id=entry.candidate_identifier,
        source="intended_structure",
        source_identifier=entry.source_identifier or entry.candidate_identifier,
        source_snapshot=snapshot.snapshot_hash,
        cif_path=str((manifest_dir / entry.relative_cif_path).resolve()),
        cif_hash=entry.sha256,
        formula=entry.formula,
        normalized_composition=composition,
        element_set=tuple(item.element for item in composition),
        space_group=entry.space_group,
        structure_family=entry.structure_family,
        intended_structure_match=True,
        chemical_compatibility_score=0.0,
        stoichiometric_similarity_score=0.5,
        synthesis_context_score=0.5,
        diffraction_pre_rank_score=0.0,
        combined_pre_rank_score=0.0,
        duplicate_cluster_id=None,
        warnings=(),
        provenance=(
            "source=intended_structure",
            f"expected_space_group={target_space_group or ''}",
        ),
    )


def _apply_chemistry_filter(
    candidates: Sequence[RankedPhaseCandidate],
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig,
) -> tuple[list[RankedPhaseCandidate], list[XRDAnalysisWarning]]:
    sample_elements = set(_normalize_element_set(analysis_input.elements))
    kept: list[RankedPhaseCandidate] = []
    warnings: list[XRDAnalysisWarning] = []
    for candidate in candidates:
        candidate_elements = set(candidate.element_set)
        unexpected = sorted(candidate_elements.difference(sample_elements))
        if unexpected:
            warnings.append(
                _candidate_warning(
                    "candidate_contains_unlisted_element",
                    "elements",
                    f"Excluded {candidate.candidate_id} because it contains unlisted elements: {', '.join(unexpected)}.",
                )
            )
            continue
        kept.append(candidate)
    return kept, warnings


def _with_soft_scores(
    candidate: RankedPhaseCandidate,
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig,
) -> RankedPhaseCandidate:
    chemical_score = 1.0
    if set(candidate.element_set) != set(_normalize_element_set(analysis_input.elements)):
        chemical_score = max(0.0, 1.0 - configuration.candidate_ranking.subset_element_bonus)
    stoich_score = _stoichiometric_similarity(candidate, analysis_input)
    context_score = _synthesis_context_score(candidate, analysis_input, configuration=configuration)
    combined = (
        configuration.candidate_ranking.chemical_score_weight * chemical_score
        + configuration.candidate_ranking.stoichiometric_score_weight * stoich_score
        + configuration.candidate_ranking.synthesis_context_score_weight * context_score
    )
    if candidate.intended_structure_match:
        combined += configuration.candidate_ranking.intended_structure_bonus
    return replace(
        candidate,
        chemical_compatibility_score=chemical_score,
        stoichiometric_similarity_score=stoich_score,
        synthesis_context_score=context_score,
        combined_pre_rank_score=combined,
    )


def _cluster_duplicate_candidates(
    candidates: Sequence[RankedPhaseCandidate],
    *,
    configuration: XRDAnalysisConfig,
) -> tuple[list[RankedPhaseCandidate], list[XRDAnalysisWarning]]:
    grouped: dict[str, list[RankedPhaseCandidate]] = {}
    for candidate in candidates:
        key = _duplicate_cluster_key(candidate, configuration=configuration)
        grouped.setdefault(key, []).append(candidate)

    clustered: list[RankedPhaseCandidate] = []
    warnings: list[XRDAnalysisWarning] = []
    for key, cluster in sorted(grouped.items()):
        cluster_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        representative = sorted(
            cluster,
            key=lambda candidate: (
                0 if candidate.intended_structure_match else 1,
                -candidate.combined_pre_rank_score,
                candidate.source,
                candidate.candidate_id,
            ),
        )[0]
        alternates = tuple(
            candidate.candidate_id
            for candidate in sorted(cluster, key=lambda item: item.candidate_id)
            if candidate.candidate_id != representative.candidate_id
        )
        clustered.append(
            replace(
                representative,
                duplicate_cluster_id=cluster_id,
                alternate_sources=alternates,
                provenance=representative.provenance + (f"duplicate_cluster_key={key}",),
            )
        )
        for removed in cluster:
            if removed.candidate_id == representative.candidate_id:
                continue
            warnings.append(
                _candidate_warning(
                    "duplicate_candidate_removed",
                    "candidate_generation",
                    f"Removed duplicate candidate {removed.candidate_id} in favor of {representative.candidate_id}.",
                )
            )
    return clustered, warnings


def _simulate_candidates(
    candidates: Sequence[RankedPhaseCandidate],
    analysis_input: XRDAnalysisInput,
    parsed_pattern: ParsedPatternMetadata,
    *,
    configuration: XRDAnalysisConfig,
) -> list[RankedPhaseCandidate]:
    simulated: list[RankedPhaseCandidate] = []
    for candidate in candidates:
        try:
            simulation = simulate_candidate_pattern(
                candidate,
                analysis_input,
                parsed_pattern,
                configuration=configuration,
            )
            simulated.append(replace(candidate, simulation=simulation))
        except CandidateSimulationError as exc:
            code = "candidate_cif_unreadable" if "unreadable" in str(exc) else "candidate_simulation_failed"
            simulated.append(
                replace(
                    candidate,
                    warnings=candidate.warnings + (
                        _candidate_warning(
                            code,
                            "raw_file_reference",
                            f"{candidate.candidate_id} could not be simulated: {exc}",
                        ),
                    ),
                )
            )
    return simulated


def simulate_candidate_pattern(
    candidate: RankedPhaseCandidate,
    analysis_input: XRDAnalysisInput,
    parsed_pattern: ParsedPatternMetadata,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> CandidateSimulation:
    cif_path = candidate.cif_path
    if not cif_path:
        raise CandidateSimulationError("candidate has no CIF path")
    cif_file = Path(cif_path)
    if not cif_file.exists():
        raise CandidateSimulationError("candidate CIF is unreadable")
    wavelength = analysis_input.wavelength_angstrom or parsed_pattern.wavelength_angstrom
    if wavelength is None:
        raise CandidateSimulationError("wavelength is unavailable for simulation")
    two_theta_values = parsed_pattern.normalized_two_theta
    if not two_theta_values:
        raise CandidateSimulationError("normalized two-theta coordinates are unavailable")
    theta_min = float(min(two_theta_values))
    theta_max = float(max(two_theta_values))
    reflections = _simulate_reflections_from_cif(
        cif_file,
        wavelength_angstrom=wavelength,
        two_theta_min=theta_min,
        two_theta_max=theta_max,
    )
    if not reflections:
        raise CandidateSimulationError("no reflections were generated in the measured range")
    positions = tuple(item["two_theta"] for item in reflections)
    intensities = tuple(item["relative_intensity"] for item in reflections)
    return CandidateSimulation(
        candidate_id=candidate.candidate_id,
        reflection_positions_two_theta=positions,
        reflection_relative_intensities=intensities,
        measured_coordinate_min=theta_min,
        measured_coordinate_max=theta_max,
        wavelength_angstrom=wavelength,
        settings_version=configuration.configuration_version,
        warnings=(),
        provenance=(
            f"reflection_count={len(reflections)}",
            f"screening_simulation={reflections[0].get('simulation_method', 'gsasii_reflection_list')}",
            f"screening_simulation_version={reflections[0].get('simulation_version', 'unknown')}",
        ),
    )


def _with_diffraction_score(
    candidate: RankedPhaseCandidate,
    parsed_pattern: ParsedPatternMetadata,
    quality_control: PatternQualityControlResult,
    *,
    configuration: XRDAnalysisConfig,
) -> RankedPhaseCandidate:
    if candidate.simulation is None:
        return candidate
    metrics = _diffraction_metrics(
        parsed_pattern,
        candidate.simulation,
        quality_control,
        configuration=configuration,
    )
    score = (
        configuration.candidate_ranking.coverage_weight * metrics["coverage"]
        + configuration.candidate_ranking.position_agreement_weight * metrics["position_agreement"]
        + configuration.candidate_ranking.whole_pattern_similarity_weight * metrics["whole_pattern_similarity"]
        + configuration.candidate_ranking.position_only_similarity_weight * metrics["position_only_similarity"]
        + configuration.candidate_ranking.matched_region_count_weight * metrics["matched_region_score"]
        - configuration.candidate_ranking.absent_strong_peak_penalty_weight * metrics["absent_strong_peak_penalty"]
        - configuration.candidate_ranking.unexplained_region_penalty_weight * metrics["unexplained_region_penalty"]
        - configuration.candidate_ranking.shift_penalty_weight * metrics["shift_penalty"]
    )
    score = max(0.0, score)
    combined = candidate.combined_pre_rank_score + score
    return replace(
        candidate,
        diffraction_pre_rank_score=score,
        combined_pre_rank_score=combined,
        provenance=candidate.provenance + (
            f"best_shift_degrees={metrics['best_shift']:.5f}",
            f"matched_regions={metrics['matched_region_count']}",
        ),
    )


def _select_top_candidates(
    candidates: Sequence[RankedPhaseCandidate],
    *,
    configuration: XRDAnalysisConfig,
) -> list[RankedPhaseCandidate]:
    sorted_candidates = sorted(
        candidates,
        key=lambda candidate: (
            candidate.combined_pre_rank_score,
            candidate.diffraction_pre_rank_score,
            candidate.intended_structure_match,
            candidate.candidate_id,
        ),
        reverse=True,
    )
    top = list(sorted_candidates[: configuration.candidate_ranking.final_top_k])
    intended = next((candidate for candidate in sorted_candidates if candidate.intended_structure_match), None)
    if intended is not None and all(item.candidate_id != intended.candidate_id for item in top):
        if len(top) >= configuration.candidate_ranking.final_top_k:
            top[-1] = intended
        else:
            top.append(intended)
    deduped: list[RankedPhaseCandidate] = []
    seen: set[str] = set()
    for candidate in top:
        if candidate.candidate_id in seen:
            continue
        seen.add(candidate.candidate_id)
        deduped.append(candidate)
    return deduped


def _diffraction_metrics(
    parsed_pattern: ParsedPatternMetadata,
    simulation: CandidateSimulation,
    quality_control: PatternQualityControlResult,
    *,
    configuration: XRDAnalysisConfig,
) -> dict[str, float]:
    observed_x = np.asarray(parsed_pattern.normalized_two_theta or (), dtype=float)
    observed_y = np.asarray(parsed_pattern.normalized_intensities, dtype=float)
    if observed_x.size == 0 or observed_y.size == 0:
        return {
            "coverage": 0.0,
            "position_agreement": 0.0,
            "whole_pattern_similarity": 0.0,
            "position_only_similarity": 0.0,
            "matched_region_score": 0.0,
            "absent_strong_peak_penalty": 1.0,
            "unexplained_region_penalty": 1.0,
            "shift_penalty": 1.0,
            "best_shift": 0.0,
            "matched_region_count": 0.0,
        }

    sim_pos = np.asarray(simulation.reflection_positions_two_theta, dtype=float)
    sim_int = np.asarray(simulation.reflection_relative_intensities, dtype=float)
    observed_peaks = _observed_peak_regions(parsed_pattern, quality_control)
    tolerance = (
        configuration.candidate_ranking.stick_pattern_peak_position_tolerance_degrees
        if parsed_pattern.pattern_type == "stick"
        else configuration.candidate_ranking.peak_position_tolerance_degrees
    )
    best_metrics: dict[str, float] | None = None
    shifts = np.linspace(
        -configuration.candidate_ranking.maximum_allowed_screening_shift_degrees,
        configuration.candidate_ranking.maximum_allowed_screening_shift_degrees,
        9,
    )
    for shift in shifts:
        shifted = sim_pos + shift
        coverage, matched_count, mean_delta = _coverage_score(observed_peaks, shifted, tolerance)
        absent_penalty = _absent_strong_peak_penalty(observed_peaks, shifted, sim_int, tolerance, configuration=configuration)
        unexplained_penalty = max(0.0, 1.0 - coverage)
        whole_similarity = _whole_pattern_similarity(observed_x, observed_y, shifted, sim_int)
        position_only = _position_only_similarity(observed_peaks, shifted, tolerance)
        matched_region_score = min(1.0, matched_count / max(1.0, float(len(observed_peaks) or 1)))
        metrics = {
            "coverage": coverage,
            "position_agreement": max(0.0, 1.0 - (mean_delta / max(tolerance, 1e-9))),
            "whole_pattern_similarity": whole_similarity,
            "position_only_similarity": position_only,
            "matched_region_score": matched_region_score,
            "absent_strong_peak_penalty": absent_penalty,
            "unexplained_region_penalty": unexplained_penalty,
            "shift_penalty": abs(float(shift)) / max(configuration.candidate_ranking.maximum_allowed_screening_shift_degrees, 1e-9),
            "best_shift": float(shift),
            "matched_region_count": float(matched_count),
        }
        if best_metrics is None or _metrics_rank_value(metrics, configuration=configuration) > _metrics_rank_value(best_metrics, configuration=configuration):
            best_metrics = metrics
    assert best_metrics is not None
    return best_metrics


def _metrics_rank_value(metrics: Mapping[str, float], *, configuration: XRDAnalysisConfig) -> float:
    return (
        configuration.candidate_ranking.coverage_weight * float(metrics["coverage"])
        + configuration.candidate_ranking.position_agreement_weight * float(metrics["position_agreement"])
        + configuration.candidate_ranking.whole_pattern_similarity_weight * float(metrics["whole_pattern_similarity"])
        + configuration.candidate_ranking.position_only_similarity_weight * float(metrics["position_only_similarity"])
        + configuration.candidate_ranking.matched_region_count_weight * float(metrics["matched_region_score"])
        - configuration.candidate_ranking.absent_strong_peak_penalty_weight * float(metrics["absent_strong_peak_penalty"])
        - configuration.candidate_ranking.unexplained_region_penalty_weight * float(metrics["unexplained_region_penalty"])
        - configuration.candidate_ranking.shift_penalty_weight * float(metrics["shift_penalty"])
    )


def _observed_peak_regions(
    parsed_pattern: ParsedPatternMetadata,
    quality_control: PatternQualityControlResult,
) -> list[tuple[float, float]]:
    if parsed_pattern.pattern_type == "stick":
        positions = parsed_pattern.normalized_two_theta or parsed_pattern.original_coordinates
        intensities = parsed_pattern.normalized_intensities
        return [
            (float(position), float(intensity))
            for position, intensity in zip(positions or (), intensities)
            if position is not None
        ]
    x = np.asarray(parsed_pattern.normalized_two_theta or (), dtype=float)
    y = np.asarray(parsed_pattern.normalized_intensities, dtype=float)
    if x.size < 3:
        return []
    prominence = max(float(np.std(y)) * 0.5, float(np.max(y) - np.min(y)) * 0.08, 1e-6)
    distance_points = max(1, int(round((quality_control.median_step_size or 0.1) and 0.25 / max(quality_control.median_step_size or 0.1, 1e-6))))
    indices, _ = find_peaks(y, prominence=prominence, distance=distance_points)
    if indices.size == 0:
        indices = np.array([int(np.argmax(y))])
    return [(float(x[idx]), float(y[idx])) for idx in indices.tolist()]


def _coverage_score(
    observed_peaks: Sequence[tuple[float, float]],
    predicted_positions: np.ndarray,
    tolerance: float,
) -> tuple[float, int, float]:
    if not observed_peaks:
        return 0.0, 0, tolerance
    matched_weight = 0.0
    total_weight = 0.0
    matched_count = 0
    deltas: list[float] = []
    for position, intensity in observed_peaks:
        total_weight += max(float(intensity), 0.0)
        if predicted_positions.size == 0:
            continue
        delta = float(np.min(np.abs(predicted_positions - position)))
        if delta <= tolerance:
            matched_weight += max(float(intensity), 0.0)
            matched_count += 1
            deltas.append(delta)
    coverage = matched_weight / total_weight if total_weight > 0 else 0.0
    mean_delta = float(np.mean(deltas)) if deltas else tolerance
    return coverage, matched_count, mean_delta


def _absent_strong_peak_penalty(
    observed_peaks: Sequence[tuple[float, float]],
    predicted_positions: np.ndarray,
    predicted_intensities: np.ndarray,
    tolerance: float,
    *,
    configuration: XRDAnalysisConfig,
) -> float:
    if predicted_positions.size == 0 or predicted_intensities.size == 0:
        return 1.0
    strong_indices = np.where(predicted_intensities >= configuration.candidate_ranking.strong_peak_fraction_threshold)[0]
    if strong_indices.size == 0:
        return 0.0
    observed_positions = np.asarray([peak[0] for peak in observed_peaks], dtype=float)
    misses = 0
    for idx in strong_indices.tolist():
        if observed_positions.size == 0 or float(np.min(np.abs(observed_positions - predicted_positions[idx]))) > tolerance:
            misses += 1
    return misses / float(len(strong_indices))


def _whole_pattern_similarity(
    observed_x: np.ndarray,
    observed_y: np.ndarray,
    predicted_positions: np.ndarray,
    predicted_intensities: np.ndarray,
) -> float:
    if observed_x.size == 0 or predicted_positions.size == 0:
        return 0.0
    simulated = np.zeros_like(observed_x, dtype=float)
    sigma = max(float(np.median(np.diff(observed_x))) * 1.5 if observed_x.size > 1 else 0.1, 0.05)
    for position, intensity in zip(predicted_positions, predicted_intensities):
        simulated += float(intensity) * np.exp(-0.5 * ((observed_x - float(position)) / sigma) ** 2)
    if np.max(simulated) > 0:
        simulated /= np.max(simulated)
    scaled_observed = observed_y.astype(float)
    if np.max(scaled_observed) > 0:
        scaled_observed = scaled_observed / np.max(scaled_observed)
    if np.std(simulated) == 0 or np.std(scaled_observed) == 0:
        return 0.0
    corr = float(np.corrcoef(simulated, scaled_observed)[0, 1])
    if math.isnan(corr):
        return 0.0
    return max(0.0, min(1.0, (corr + 1.0) / 2.0))


def _position_only_similarity(
    observed_peaks: Sequence[tuple[float, float]],
    predicted_positions: np.ndarray,
    tolerance: float,
) -> float:
    if not observed_peaks or predicted_positions.size == 0:
        return 0.0
    matched = 0
    for position, _intensity in observed_peaks:
        if float(np.min(np.abs(predicted_positions - position))) <= tolerance:
            matched += 1
    union = len(observed_peaks) + int(predicted_positions.size) - matched
    return matched / float(max(union, 1))


def _stoichiometric_similarity(candidate: RankedPhaseCandidate, analysis_input: XRDAnalysisInput) -> float:
    sample = {item.element: float(item.amount) for item in analysis_input.stoichiometric_amounts}
    candidate_map = {item.element: float(item.amount) for item in candidate.normalized_composition}
    if not sample or not candidate_map:
        return 0.5
    sample_total = sum(sample.values())
    candidate_total = sum(candidate_map.values())
    if sample_total <= 0 or candidate_total <= 0:
        return 0.5
    all_elements = sorted(set(sample) | set(candidate_map))
    distance = 0.0
    for element in all_elements:
        distance += abs((sample.get(element, 0.0) / sample_total) - (candidate_map.get(element, 0.0) / candidate_total))
    return max(0.0, 1.0 - (distance / 2.0))


def _synthesis_context_score(
    candidate: RankedPhaseCandidate,
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig,
) -> float:
    context = analysis_input.synthesis_context
    if not context.ordered_steps and not context.precursor_records:
        return configuration.candidate_ranking.neutral_context_score
    score = configuration.candidate_ranking.neutral_context_score
    precursor_text = " ".join(
        filter(
            None,
            [precursor.formula for precursor in context.precursor_records] + [precursor.name for precursor in context.precursor_records],
        )
    ).lower()
    if candidate.formula and candidate.formula.lower() in precursor_text:
        score += 0.15
    if candidate.structure_family and analysis_input.structure_family and candidate.structure_family == analysis_input.structure_family:
        score += 0.15
    max_temp = max(context.temperatures_c or (0.0,))
    if max_temp >= 700.0:
        score += 0.05
    return max(0.0, min(1.0, score))


def _duplicate_cluster_key(candidate: RankedPhaseCandidate, *, configuration: XRDAnalysisConfig) -> str:
    if configuration.candidate_ranking.duplicate_formula_space_group_enabled:
        formula_key = _formula_key(candidate.formula)
        sg_key = _normalize_space_group(candidate.space_group) or "unknown"
        if formula_key != "unknown" or sg_key != "unknown":
            return f"formula_space_group:{formula_key}|{sg_key}"
    if candidate.cif_hash:
        return f"sha256:{candidate.cif_hash}"
    return f"candidate:{candidate.candidate_id}"


def _simulate_reflections_from_cif(
    cif_path: Path,
    *,
    wavelength_angstrom: float,
    two_theta_min: float,
    two_theta_max: float,
) -> list[dict[str, Any]]:
    if two_theta_max <= two_theta_min:
        raise CandidateSimulationError("invalid screening range")
    try:
        G2sc = configure_gsas()
        from GSASII import GSASIIpath  # type: ignore
        from GSASII import GSASIIlattice as G2lat  # type: ignore
        from GSASII import GSASIIstrIO as G2stIO  # type: ignore
        from GSASII import GSASIIstrMath as G2strMath  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised only without GSAS-II
        raise CandidateSimulationError("GSAS-II runtime is unavailable for candidate screening") from exc

    phase_name = cif_path.stem
    gpx_path, remove_gpx = prepare_project_path()
    instprm_path, remove_instprm = resolve_instrument_parameter_file()
    xye_path = _write_screening_histogram(two_theta_min, two_theta_max)
    try:
        project = new_project(G2sc, gpx_path)
        histogram = project.add_powder_histogram(xye_path, iparams=instprm_path, fmthint="xye")
        _set_histogram_wavelength(histogram, wavelength_angstrom)
        phase_object = project.add_phase(str(cif_path.resolve()), phasename=phase_name, histograms=[histogram], fmthint="CIF")

        histograms = {
            histogram.name: {
                **project.data[histogram.name],
                "hId": phase_object.data["Histograms"][histogram.name]["hId"],
                "wtFactor": 1.0,
            }
        }
        phases = {
            name: phase_data
            for name, phase_data in project.data["Phases"].items()
            if name != "data" and phase_data is not None
        }
        histogram_name = histogram.name
        phase = phases[phase_name]

        natoms, atom_index, _phase_vary, phase_dict, _pawley_lookup, ff_tables, ef_tables, orb_tables, bl_tables, mf_tables, max_ss_wave = G2stIO.GetPhaseData(
            phases,
            RestraintDict=None,
            Print=False,
        )
        calc_controls = {
            "atomIndx": atom_index,
            "Natoms": natoms,
            "FFtables": ff_tables,
            "EFtables": ef_tables,
            "ORBtables": orb_tables,
            "BLtables": bl_tables,
            "MFtables": mf_tables,
            "maxSSwave": max_ss_wave,
        }
        _hap_vary, hap_dict, control_dict = G2stIO.GetHistogramPhaseData(
            phases,
            histograms,
            Controls=calc_controls,
            Print=False,
        )
        calc_controls.update(control_dict)
        _hist_vary, hist_dict, _hist_dict1, control_dict = G2stIO.GetHistogramData(histograms, Print=False)
        calc_controls.update(control_dict)

        parm_dict: dict[str, Any] = {}
        parm_dict.update(phase_dict)
        parm_dict.update(hap_dict)
        parm_dict.update(hist_dict)
        G2stIO.GetFprime(calc_controls, histograms)

        reflection_dict = histograms[histogram_name]["Reflection Lists"].get(phase_name)
        if not reflection_dict or reflection_dict["RefList"].size == 0:
            return []

        phase_id = phase["pId"]
        histogram_id = histograms[histogram_name]["hId"]
        phase_prefix = f"{phase_id}::"
        phase_hist_prefix = f"{phase_id}:{histogram_id}:"
        hist_prefix = f":{histogram_id}:"
        sg_data = phase["General"]["SGData"]
        sg_matrices = np.array([ops[0].T for ops in sg_data["SGOps"]], dtype=float)
        reciprocal_metric, real_metric = G2lat.A2Gmat([parm_dict[phase_prefix + f"A{i}"] for i in range(6)])
        reciprocal_volume = float(np.sqrt(np.linalg.det(reciprocal_metric)))
        wave_key = hist_prefix + ("Lam1" if hist_prefix + "Lam1" in parm_dict else "Lam")
        wave = float(parm_dict[wave_key])
        G2strMath.StructureFactor2(
            reflection_dict,
            reciprocal_metric,
            hist_prefix,
            phase_prefix,
            sg_data,
            calc_controls,
            parm_dict,
        )

        reflections: list[dict[str, Any]] = []
        for reflection in reflection_dict["RefList"]:
            unique_reflection = np.inner(reflection[:3], sg_matrices)
            two_theta = float(
                G2strMath.GetReflPos(
                    reflection,
                    0,
                    wave,
                    [parm_dict[phase_prefix + f"A{i}"] for i in range(6)],
                    phase_prefix,
                    hist_prefix,
                    phase_hist_prefix,
                    calc_controls,
                    parm_dict,
                )
            )
            if two_theta < two_theta_min or two_theta > two_theta_max:
                continue
            d_spacing = float(reflection[4])
            if d_spacing <= 0.0:
                continue
            lorentz = 1.0 / (2.0 * math.sin(math.radians(two_theta / 2.0)) ** 2 * math.cos(math.radians(two_theta / 2.0)))
            intensity_correction, _, _, _ = G2strMath.GetIntensityCorr(
                reflection,
                0,
                unique_reflection,
                reciprocal_metric,
                real_metric,
                phase_prefix,
                phase_hist_prefix,
                hist_prefix,
                sg_data,
                calc_controls,
                parm_dict,
            )
            screening_intensity = float(reflection[9] * intensity_correction * reciprocal_volume * lorentz)
            if screening_intensity <= 0.0:
                continue
            reflections.append(
                {
                    "h": int(round(float(reflection[0]))),
                    "k": int(round(float(reflection[1]))),
                    "l": int(round(float(reflection[2]))),
                    "multiplicity": int(round(float(reflection[3]))),
                    "d_spacing": d_spacing,
                    "two_theta": round(two_theta, 6),
                    "relative_intensity": screening_intensity,
                    "wavelength_angstrom": wavelength_angstrom,
                    "simulation_method": "gsasii_reflection_list",
                    "simulation_version": _gsas_version_string(GSASIIpath),
                }
            )
    except CandidateSimulationError:
        raise
    except Exception as exc:
        raise CandidateSimulationError(f"GSAS-II screening simulation failed: {exc}") from exc
    finally:
        cleanup_paths((gpx_path, remove_gpx), (instprm_path, remove_instprm), (xye_path, True))

    if not reflections:
        return []
    max_intensity = max(item["relative_intensity"] for item in reflections)
    for item in reflections:
        item["relative_intensity"] = float(item["relative_intensity"] / max_intensity)
    reflections.sort(key=lambda item: (float(item["two_theta"]), -float(item["relative_intensity"])))
    return reflections


def _write_screening_histogram(two_theta_min: float, two_theta_max: float) -> str:
    step = min(0.05, max(0.01, (two_theta_max - two_theta_min) / 3000.0))
    point_count = max(2, int(round((two_theta_max - two_theta_min) / step)) + 1)
    coordinates = np.linspace(two_theta_min, two_theta_max, point_count, dtype=float)
    intensities = np.full(point_count, 100.0, dtype=float)
    sigma = np.full(point_count, 1.0, dtype=float)
    return write_temp_xye(coordinates, intensities, sigma)


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


def _gsas_version_string(gsas_path_module: Any) -> str:
    get_version = getattr(gsas_path_module, "GetVersionNumber", None)
    if callable(get_version):
        try:
            return str(get_version())
        except Exception:
            return "unknown"
    return "unknown"


def _parse_cif_structure(cif_path: Path) -> tuple[dict[str, float], list[tuple[str, str, str]], list[dict[str, Any]]]:
    lines = cif_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    cell = {
        "a": _read_cif_scalar(lines, "_cell_length_a"),
        "b": _read_cif_scalar(lines, "_cell_length_b"),
        "c": _read_cif_scalar(lines, "_cell_length_c"),
        "alpha": _read_cif_scalar(lines, "_cell_angle_alpha"),
        "beta": _read_cif_scalar(lines, "_cell_angle_beta"),
        "gamma": _read_cif_scalar(lines, "_cell_angle_gamma"),
    }
    sym_headers, sym_rows = _read_cif_loop(lines, "_symmetry_equiv_pos_as_xyz", "_space_group_symop_operation_xyz")
    if sym_rows:
        expr_index = 1 if len(sym_headers) > 1 else 0
        symmetry_ops = [_parse_symmetry_operation(row[expr_index]) for row in sym_rows]
    else:
        symmetry_ops = [("x", "y", "z")]
    atom_headers, atom_rows = _read_cif_loop(lines, "_atom_site_fract_x", "_atom_site_fract_y")
    if not atom_rows:
        raise CandidateSimulationError("CIF contains no atom-site loop")
    header_index = {header: idx for idx, header in enumerate(atom_headers)}
    atoms: list[dict[str, Any]] = []
    symbol_key = next((key for key in header_index if key.endswith("type_symbol")), None)
    x_key = next((key for key in header_index if key.endswith("fract_x")), None)
    y_key = next((key for key in header_index if key.endswith("fract_y")), None)
    z_key = next((key for key in header_index if key.endswith("fract_z")), None)
    occ_key = next((key for key in header_index if key.endswith("occupancy")), None)
    if symbol_key is None or x_key is None or y_key is None or z_key is None:
        raise CandidateSimulationError("CIF atom-site loop is missing required fractional coordinates")
    for row in atom_rows:
        symbol = _clean_element_symbol(row[header_index[symbol_key]])
        if not symbol:
            continue
        atoms.append(
            {
                "symbol": symbol,
                "x": _coerce_cif_number(row[header_index[x_key]]),
                "y": _coerce_cif_number(row[header_index[y_key]]),
                "z": _coerce_cif_number(row[header_index[z_key]]),
                "occupancy": _coerce_cif_number(row[header_index[occ_key]]) if occ_key is not None else 1.0,
            }
        )
    return cell, symmetry_ops, atoms


def _read_cif_scalar(lines: Sequence[str], key: str) -> float:
    for line in lines:
        if line.strip().startswith(f"{key} "):
            parts = line.split(maxsplit=1)
            return _coerce_cif_number(parts[1])
    raise CandidateSimulationError(f"CIF is missing required scalar {key}")


def _read_cif_loop(lines: Sequence[str], *required_markers: str) -> tuple[list[str], list[list[str]]]:
    index = 0
    while index < len(lines):
        if lines[index].strip() != "loop_":
            index += 1
            continue
        index += 1
        headers: list[str] = []
        while index < len(lines) and lines[index].strip().startswith("_"):
            headers.append(lines[index].strip())
            index += 1
        if not headers:
            continue
        if not any(marker in " ".join(headers) for marker in required_markers):
            while index < len(lines) and lines[index].strip() and not lines[index].strip().startswith("loop_") and not lines[index].strip().startswith("_"):
                index += 1
            continue
        rows: list[list[str]] = []
        while index < len(lines):
            stripped = lines[index].strip()
            if not stripped or stripped.startswith("#"):
                index += 1
                continue
            if stripped == "loop_" or stripped.startswith("_"):
                break
            rows.append(_tokenize_cif_row(stripped))
            index += 1
        return headers, rows
    return [], []


def _tokenize_cif_row(text: str) -> list[str]:
    return re.findall(r"(?:'[^']*'|\"[^\"]*\"|\S+)", text)


def _parse_symmetry_operation(expr: str) -> tuple[str, str, str]:
    cleaned = expr.strip().strip("'").strip('"')
    parts = [part.strip() for part in cleaned.split(",")]
    if len(parts) != 3:
        raise CandidateSimulationError(f"Unsupported symmetry operation: {expr}")
    return parts[0], parts[1], parts[2]


def _expand_atomic_positions(
    symmetry_ops: Sequence[tuple[str, str, str]],
    atoms: Sequence[Mapping[str, Any]],
) -> list[dict[str, float | str]]:
    expanded: list[dict[str, float | str]] = []
    seen: set[tuple[str, int, int, int]] = set()
    for atom in atoms:
        for op_x, op_y, op_z in symmetry_ops:
            x = _eval_symmetry_component(op_x, float(atom["x"]), float(atom["y"]), float(atom["z"]))
            y = _eval_symmetry_component(op_y, float(atom["x"]), float(atom["y"]), float(atom["z"]))
            z = _eval_symmetry_component(op_z, float(atom["x"]), float(atom["y"]), float(atom["z"]))
            wrapped = (_wrap_fractional(x), _wrap_fractional(y), _wrap_fractional(z))
            key = (
                str(atom["symbol"]),
                int(round(wrapped[0] * 1_000_000)),
                int(round(wrapped[1] * 1_000_000)),
                int(round(wrapped[2] * 1_000_000)),
            )
            if key in seen:
                continue
            seen.add(key)
            expanded.append(
                {
                    "symbol": str(atom["symbol"]),
                    "x": wrapped[0],
                    "y": wrapped[1],
                    "z": wrapped[2],
                    "occupancy": float(atom.get("occupancy", 1.0) or 1.0),
                }
            )
    return expanded


def _eval_symmetry_component(expr: str, x: float, y: float, z: float) -> float:
    total = 0.0
    for term in re.findall(r"[+-]?[^+-]+", expr.replace(" ", "")):
        sign = -1.0 if term.startswith("-") else 1.0
        body = term[1:] if term[:1] in "+-" else term
        if body == "x":
            total += sign * x
        elif body == "y":
            total += sign * y
        elif body == "z":
            total += sign * z
        else:
            total += sign * _coerce_cif_number(body)
    return total


def _wrap_fractional(value: float) -> float:
    wrapped = value % 1.0
    return 0.0 if abs(wrapped - 1.0) < 1e-9 else wrapped


def _d_spacing(cell: Mapping[str, float], h: int, k: int, l: int) -> float | None:
    a_vec, b_vec, c_vec = _lattice_vectors(cell)
    volume = float(np.dot(a_vec, np.cross(b_vec, c_vec)))
    if abs(volume) < 1e-9:
        return None
    a_star = np.cross(b_vec, c_vec) / volume
    b_star = np.cross(c_vec, a_vec) / volume
    c_star = np.cross(a_vec, b_vec) / volume
    g = h * a_star + k * b_star + l * c_star
    magnitude = float(np.linalg.norm(g))
    if magnitude <= 1e-12:
        return None
    return 1.0 / magnitude


def _lattice_vectors(cell: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    a = float(cell["a"])
    b = float(cell["b"])
    c = float(cell["c"])
    alpha = math.radians(float(cell["alpha"]))
    beta = math.radians(float(cell["beta"]))
    gamma = math.radians(float(cell["gamma"]))
    a_vec = np.array([a, 0.0, 0.0], dtype=float)
    b_vec = np.array([b * math.cos(gamma), b * math.sin(gamma), 0.0], dtype=float)
    c_x = c * math.cos(beta)
    sin_gamma = math.sin(gamma)
    if abs(sin_gamma) < 1e-12:
        raise CandidateSimulationError("degenerate cell gamma angle")
    c_y = c * ((math.cos(alpha) - math.cos(beta) * math.cos(gamma)) / sin_gamma)
    c_z_sq = max(c * c - c_x * c_x - c_y * c_y, 0.0)
    c_vec = np.array([c_x, c_y, math.sqrt(c_z_sq)], dtype=float)
    return a_vec, b_vec, c_vec


def _estimate_max_index(cell: Mapping[str, float], d_min: float) -> int:
    shortest = min(float(cell["a"]), float(cell["b"]), float(cell["c"]))
    return max(3, int(math.ceil(shortest / max(d_min, 1e-6))) + 1)


def _structure_factor_intensity(positions: Sequence[Mapping[str, float | str]], h: int, k: int, l: int) -> float:
    structure_factor = 0.0j
    for atom in positions:
        symbol = str(atom["symbol"])
        scattering = float(_ATOMIC_NUMBERS.get(symbol, 10))
        phase = 2.0 * math.pi * (
            h * float(atom["x"])
            + k * float(atom["y"])
            + l * float(atom["z"])
        )
        structure_factor += scattering * float(atom.get("occupancy", 1.0) or 1.0) * complex(math.cos(phase), math.sin(phase))
    return float(abs(structure_factor) ** 2)


def _merge_reflections(reflections: Sequence[Mapping[str, float]]) -> list[dict[str, float]]:
    merged: list[dict[str, float]] = []
    for reflection in sorted(reflections, key=lambda item: (float(item["two_theta"]), -float(item["relative_intensity"]))):
        if not merged or abs(float(reflection["two_theta"]) - merged[-1]["two_theta"]) > 1e-5:
            merged.append(
                {
                    "two_theta": float(reflection["two_theta"]),
                    "relative_intensity": float(reflection["relative_intensity"]),
                }
            )
        else:
            merged[-1]["relative_intensity"] += float(reflection["relative_intensity"])
    return merged


def _normalize_formula_to_amounts(formula: str | None) -> tuple[StoichiometricAmount, ...]:
    if not formula:
        return ()
    try:
        parsed = _phase_composition_from_formula(formula)
    except Exception:
        return ()
    return tuple(
        StoichiometricAmount(element=element, amount=float(amount))
        for element, amount in sorted(parsed.items())
    )


def _phase_composition_from_formula(formula: str) -> dict[str, float]:
    return dict(sorted(_parse_formula(formula).items()))


def _normalize_element_set(elements: Iterable[Any]) -> tuple[str, ...]:
    normalized = sorted({
        _clean_element_symbol(item)
        for item in elements or ()
        if _clean_element_symbol(item)
    })
    return tuple(normalized)


def _clean_element_symbol(value: Any) -> str:
    match = _ELEMENT_RE.search(str(value or "").strip())
    return match.group(0) if match else ""


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_space_group(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip()


def _formula_key(formula: str | None) -> str:
    if not formula:
        return "unknown"
    try:
        composition = _phase_composition_from_formula(formula)
    except Exception:
        return formula.strip().lower()
    return "".join(f"{element}{composition[element]:g}" for element in sorted(composition))


def _coerce_cif_number(value: Any) -> float:
    text = str(value).strip().strip("'").strip('"')
    text = text.split("(", 1)[0]
    if "/" in text and text.count("/") == 1 and not any(ch.isalpha() for ch in text):
        numerator, denominator = text.split("/", 1)
        return float(numerator) / float(denominator)
    return float(text)


def _candidate_warning(code: str, field_name: str, message: str) -> XRDAnalysisWarning:
    return XRDAnalysisWarning(
        code=code,
        message=message,
        severity="warning",
        field=field_name,
        stage="candidate_generation",
    )


def _linked_source_name(linked: LinkedStructureReference) -> str:
    mapping = {
        "material": "material_linked_structure",
        "recipe": "recipe_linked_structure",
        "trial": "trial_linked_structure",
        "literature": "literature_linked_structure",
        "dft": "dft_linked_structure",
        "external_snapshot": "external_snapshot",
    }
    return mapping[linked.source_kind]


def _dedupe_exact_candidate_ids(candidates: Sequence[RankedPhaseCandidate]) -> list[RankedPhaseCandidate]:
    deduped: dict[str, RankedPhaseCandidate] = {}
    for candidate in candidates:
        if candidate.candidate_id not in deduped:
            deduped[candidate.candidate_id] = candidate
    return [deduped[key] for key in sorted(deduped)]


def _dedupe_warnings(warnings: Sequence[XRDAnalysisWarning]) -> list[XRDAnalysisWarning]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[XRDAnalysisWarning] = []
    for warning in warnings:
        key = (warning.code, warning.field, warning.message)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(warning)
    return deduped


def _safe_hash_file(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


__all__ = [
    "CandidateGenerationError",
    "CandidateSimulationError",
    "REFERENCE_MANIFEST_PATH",
    "REFERENCE_PHASES_DIR",
    "ReferenceSnapshotMismatchError",
    "build_ranked_phase_candidates",
    "load_reference_phase_snapshot",
    "simulate_candidate_pattern",
    "validate_reference_phase_snapshot",
]
