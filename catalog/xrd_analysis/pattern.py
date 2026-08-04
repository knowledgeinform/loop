from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

import numpy as np
import pandas as pd
from django.conf import settings
from scipy.signal import find_peaks, savgol_filter

from catalog.utils import parse_xrd_file

from .schemas import (
    DEFAULT_XRD_ANALYSIS_CONFIG,
    CoordinateConversionError,
    CoordinateOrder,
    CoordinateType,
    MetadataItem,
    ParsedPatternMetadata,
    PatternParseError,
    PatternParserType,
    PatternProvenance,
    PatternQualityControlResult,
    PatternStageStatus,
    PatternType,
    QUALITY_FAILURE_CODES,
    QualityControlSettingsPlaceholder,
    RawFileReference,
    RawFileReferenceError,
    UnsupportedPatternFormatError,
    XRDAnalysisConfig,
    XRDAnalysisInput,
    XRDAnalysisWarning,
)

_COORDINATE_HINTS = {
    "two_theta": ("angle", "2theta", "two_theta", "twotheta", "theta"),
    "d_spacing": ("d", "dspacing", "d_spacing", "spacing"),
    "q": ("q", "qvalue", "reciprocal"),
}
_INTENSITY_HINTS = ("intensity", "counts", "count", "cts", "y")
_PARSER_TYPES: tuple[PatternParserType, ...] = (
    "loop_csv",
    "generic_csv",
    "generic_txt",
    "rigaku_ascii",
    "reflection_card",
    "binary_raw",
    "dataframe",
)


def parse_pattern_from_input(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> ParsedPatternMetadata:
    source_path = resolve_raw_file_reference_path(analysis_input.raw_file_reference)
    filename = _source_filename(analysis_input.raw_file_reference, source_path)
    try:
        parser_metadata, df = parse_xrd_file(source_path, filename)
    except FileNotFoundError as exc:
        raise RawFileReferenceError(str(exc)) from exc
    except ValueError as exc:
        message = str(exc)
        if "unsupported" in message.lower() or "could not parse" in message.lower():
            raise UnsupportedPatternFormatError(message) from exc
        raise PatternParseError(message) from exc
    except Exception as exc:  # GSAS-II runtime or parser failure
        raise PatternParseError(str(exc)) from exc

    parser_type = _infer_parser_type(filename, parser_metadata, df)
    return normalize_table_pattern(
        df,
        coordinate_column=analysis_input.coordinate_column,
        intensity_column=analysis_input.intensity_column,
        coordinate_type=analysis_input.coordinate_type,
        wavelength_angstrom=analysis_input.wavelength_angstrom,
        parser_type=parser_type,
        pattern_type="stick" if df.attrs.get("plot_style") == "stick" else "continuous",
        parser_metadata=_metadata_items(parser_metadata),
        source_label=source_path,
        configuration=configuration,
    )


def normalize_table_pattern(
    table: pd.DataFrame,
    *,
    coordinate_column: Optional[str] = None,
    intensity_column: Optional[str] = None,
    coordinate_type: Optional[CoordinateType] = None,
    wavelength_angstrom: Optional[float] = None,
    parser_type: PatternParserType = "dataframe",
    pattern_type: PatternType = "continuous",
    parser_metadata: tuple[MetadataItem, ...] = (),
    source_label: str = "<memory>",
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> ParsedPatternMetadata:
    if parser_type not in _PARSER_TYPES:
        raise UnsupportedPatternFormatError(f"Unsupported parser_type {parser_type!r}")
    if not isinstance(table, pd.DataFrame):
        raise PatternParseError("Pattern normalization expects a pandas DataFrame")

    working = table.copy()
    selected_coordinate = _resolve_coordinate_column(working, coordinate_column)
    selected_intensity = _resolve_intensity_column(working, intensity_column, selected_coordinate)

    raw_coordinates, raw_intensities = _coerce_series(
        working[selected_coordinate],
        working[selected_intensity],
    )
    coordinate_order = _coordinate_order(raw_coordinates)
    warnings: list[XRDAnalysisWarning] = []
    cleanup_steps: list[str] = []

    invalid_mask = np.array(
        [
            coord is None
            or intensity is None
            or not math.isfinite(coord)
            or not math.isfinite(intensity)
            for coord, intensity in zip(raw_coordinates, raw_intensities)
        ],
        dtype=bool,
    )
    invalid_row_count = int(np.count_nonzero(invalid_mask))
    if invalid_row_count:
        cleanup_steps.append(f"removed_invalid_rows:{invalid_row_count}")
        warnings.append(
            _warning(
                "invalid_rows_removed",
                "original_coordinates",
                f"Removed {invalid_row_count} invalid or non-finite rows during pattern normalization.",
                stage="pattern_parsing",
            )
        )

    usable_coordinates = np.asarray(
        [float(value) for value, drop in zip(raw_coordinates, invalid_mask) if not drop],
        dtype=float,
    )
    usable_intensities = np.asarray(
        [float(value) for value, drop in zip(raw_intensities, invalid_mask) if not drop],
        dtype=float,
    )

    if usable_coordinates.size:
        sort_index = np.argsort(usable_coordinates, kind="mergesort")
        if not np.array_equal(sort_index, np.arange(sort_index.size)):
            cleanup_steps.append("sorted_coordinates:ascending")
        usable_coordinates = usable_coordinates[sort_index]
        usable_intensities = usable_intensities[sort_index]

    dedup_coordinates, dedup_intensities, duplicate_group_count = _merge_duplicate_coordinates(
        usable_coordinates,
        usable_intensities,
        tolerance=configuration.pattern_processing.duplicate_coordinate_tolerance,
    )
    if duplicate_group_count:
        cleanup_steps.append(f"merged_duplicate_groups:{duplicate_group_count}")
        warnings.append(
            _warning(
                "repeated_coordinates",
                "original_coordinates",
                f"Collapsed {duplicate_group_count} repeated coordinate groups using the configured duplicate rule.",
                stage="pattern_parsing",
            )
        )

    inferred_coordinate_type = _resolve_coordinate_type(
        coordinate_type=coordinate_type,
        coordinate_column=selected_coordinate,
        parser_type=parser_type,
        pattern_type=pattern_type,
    )
    if coordinate_type and coordinate_type != inferred_coordinate_type and parser_type != "dataframe":
        warnings.append(
            _warning(
                "coordinate_type_conflict",
                "coordinate_type",
                "Declared coordinate type conflicts with the parser-normalized pattern representation.",
                stage="pattern_parsing",
            )
        )

    if parser_type != "dataframe":
        if coordinate_column and coordinate_column not in working.columns and coordinate_column != "Angle":
            warnings.append(
                _warning(
                    "parser_output_type_conflict",
                    "coordinate_column",
                    "The repository parser standardized the coordinate column to 'Angle'; the declared source column name was not preserved.",
                    stage="pattern_parsing",
                )
            )
        if intensity_column and intensity_column not in working.columns and intensity_column != "Intensity":
            warnings.append(
                _warning(
                    "parser_output_type_conflict",
                    "intensity_column",
                    "The repository parser standardized the intensity column to 'Intensity'; the declared source column name was not preserved.",
                    stage="pattern_parsing",
                )
            )

    two_theta, d_spacing, q_values, conversion_steps, conversion_warnings = _derive_coordinate_views(
        dedup_coordinates,
        coordinate_type=inferred_coordinate_type,
        wavelength_angstrom=wavelength_angstrom,
    )
    if (
        inferred_coordinate_type in {"d_spacing", "q"}
        and wavelength_angstrom is not None
        and dedup_coordinates.size > 0
        and two_theta is None
    ):
        raise CoordinateConversionError(
            "Could not derive any physically valid two-theta coordinates from the supplied pattern and wavelength."
        )
    if two_theta is not None and two_theta.size and inferred_coordinate_type != "two_theta":
        reorder = np.argsort(two_theta, kind="mergesort")
        two_theta = two_theta[reorder]
        dedup_intensities = dedup_intensities[reorder]
        if d_spacing is not None:
            d_spacing = d_spacing[reorder]
        if q_values is not None:
            q_values = q_values[reorder]
        cleanup_steps.append("reordered_by_two_theta:ascending")
    cleanup_steps.extend(conversion_steps)
    warnings.extend(conversion_warnings)

    median_step_size, step_size_variation = _step_metrics(_active_coordinates(two_theta, dedup_coordinates))
    provenance = PatternProvenance(
        parser_type=parser_type,
        source_label=source_label,
        metadata_items=parser_metadata,
        cleanup_steps=tuple(cleanup_steps),
        invalid_row_count=invalid_row_count,
        duplicate_group_count=duplicate_group_count,
        duplicate_coordinate_tolerance=configuration.pattern_processing.duplicate_coordinate_tolerance,
        duplicate_coordinate_rule=configuration.pattern_processing.duplicate_coordinate_rule,
    )

    return ParsedPatternMetadata(
        parser_type=parser_type,
        pattern_type=pattern_type,
        original_coordinate_type=inferred_coordinate_type,
        coordinate_column=selected_coordinate,
        intensity_column=selected_intensity,
        original_coordinates=tuple(raw_coordinates),
        original_intensities=tuple(raw_intensities),
        normalized_two_theta=tuple(two_theta.tolist()) if two_theta is not None else None,
        normalized_d_spacing=tuple(d_spacing.tolist()) if d_spacing is not None else None,
        normalized_q=tuple(q_values.tolist()) if q_values is not None else None,
        normalized_intensities=tuple(float(value) for value in dedup_intensities.tolist()),
        wavelength_angstrom=wavelength_angstrom,
        usable_point_count=int(dedup_coordinates.size),
        coordinate_order=coordinate_order,
        median_step_size=median_step_size,
        step_size_variation=step_size_variation,
        warnings=tuple(warnings),
        provenance=provenance,
    )


def run_pattern_quality_control(
    parsed_pattern: ParsedPatternMetadata,
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> PatternQualityControlResult:
    qc = configuration.quality_control
    warnings = list(parsed_pattern.warnings)
    failure_codes: list[str] = []
    provenance_notes: list[str] = []

    active_coordinates = np.asarray(
        _active_coordinates(parsed_pattern.normalized_two_theta, parsed_pattern.original_coordinates),
        dtype=float,
    )
    intensities = np.asarray(parsed_pattern.normalized_intensities, dtype=float)

    usable_point_count = int(intensities.size)
    coordinate_min = float(active_coordinates.min()) if active_coordinates.size else None
    coordinate_max = float(active_coordinates.max()) if active_coordinates.size else None
    range_width = (
        float(coordinate_max - coordinate_min)
        if coordinate_min is not None and coordinate_max is not None
        else None
    )
    median_step_size = parsed_pattern.median_step_size
    step_size_variation = parsed_pattern.step_size_variation
    invalid_fraction = (
        parsed_pattern.provenance.invalid_row_count / max(len(parsed_pattern.original_coordinates), 1)
    )
    duplicate_count = parsed_pattern.provenance.duplicate_group_count
    negative_fraction = (
        float(np.mean(intensities < 0)) if intensities.size else 0.0
    )
    non_positive_fraction = (
        float(np.mean(intensities <= 0)) if intensities.size else 0.0
    )
    clipping_detected = _detect_clipping(intensities, qc)

    snr = None
    peak_count = 0
    missing_interval_count = 0
    if parsed_pattern.pattern_type == "continuous" and active_coordinates.size:
        snr, peak_count = _signal_metrics(active_coordinates, intensities)
        missing_interval_count = _missing_interval_count(active_coordinates, median_step_size, qc)
    else:
        provenance_notes.append("stick_pattern_exempt_from_continuous_grid_rules")
        warnings.append(
            _warning(
                "stick_pattern_limited",
                "pattern_type",
                "Stick-pattern inputs remain parseable but are not treated as equivalent to measured continuous scans for later phase analysis.",
                stage="quality_control",
            )
        )

    warnings.extend(
        _metadata_validation_warnings(
            parsed_pattern,
            analysis_input,
            step_size_variation=step_size_variation,
            missing_interval_count=missing_interval_count,
            negative_fraction=negative_fraction,
            non_positive_fraction=non_positive_fraction,
            clipping_detected=clipping_detected,
            configuration=configuration,
        )
    )

    if usable_point_count == 0:
        failure_codes.append("no_usable_points")
    if parsed_pattern.pattern_type == "continuous":
        if usable_point_count < qc.min_usable_points_continuous:
            failure_codes.append("too_few_usable_points")
        if range_width is None or range_width < qc.min_range_width_continuous:
            failure_codes.append("zero_coordinate_range")
        if non_positive_fraction >= 1.0:
            failure_codes.append("all_non_positive_intensity")
        if _is_flat_signal(intensities, qc):
            failure_codes.append("flat_signal")
        if (
            (snr is None or snr < qc.min_signal_to_noise)
            or peak_count < qc.min_detectable_peak_regions_continuous
        ):
            failure_codes.append("insufficient_peak_evidence")

    status: PatternStageStatus
    if failure_codes:
        status = "insufficient-quality data"
    else:
        status = "pattern accepted for later analysis"

    return PatternQualityControlResult(
        status=status,
        pattern_type=parsed_pattern.pattern_type,
        usable_point_count=usable_point_count,
        coordinate_min=coordinate_min,
        coordinate_max=coordinate_max,
        range_width=range_width,
        median_step_size=median_step_size,
        step_size_variation=step_size_variation,
        fraction_invalid_rows_removed=float(invalid_fraction),
        duplicate_count=duplicate_count,
        negative_intensity_fraction=float(negative_fraction),
        non_positive_intensity_fraction=float(non_positive_fraction),
        approximate_signal_to_noise=snr,
        detectable_peak_region_count=peak_count,
        missing_interval_count=missing_interval_count,
        clipping_detected=clipping_detected,
        failure_codes=tuple(_unique_ordered(failure_codes)),
        warnings=tuple(_unique_warning_objects(warnings)),
        provenance_notes=tuple(provenance_notes),
    )


def parse_and_qc_input_pattern(
    analysis_input: XRDAnalysisInput,
    *,
    configuration: XRDAnalysisConfig = DEFAULT_XRD_ANALYSIS_CONFIG,
) -> tuple[ParsedPatternMetadata, PatternQualityControlResult]:
    parsed_pattern = parse_pattern_from_input(analysis_input, configuration=configuration)
    qc_result = run_pattern_quality_control(
        parsed_pattern,
        analysis_input,
        configuration=configuration,
    )
    return parsed_pattern, qc_result


def resolve_raw_file_reference_path(raw_file_reference: Optional[RawFileReference]) -> str:
    if raw_file_reference is None or not raw_file_reference.locator:
        raise RawFileReferenceError("No raw-file reference is available for pattern parsing")

    locator = raw_file_reference.locator
    if raw_file_reference.reference_kind in {"stored_path", "resolved_path", "raw_db_path"}:
        return _resolve_locator_path(locator, allow_media_relative=True)

    if raw_file_reference.reference_kind == "raw_data_link":
        parsed = urlparse(locator)
        media_url = getattr(settings, "MEDIA_URL", "/media/")
        if parsed.path.startswith(media_url):
            rel = parsed.path[len(media_url) :].lstrip("/")
            return _resolve_locator_path(rel, allow_media_relative=True)
        raise RawFileReferenceError(f"Raw-data URL is not readable from local MEDIA_ROOT: {locator}")

    raise RawFileReferenceError(f"Unsupported raw-file reference kind: {raw_file_reference.reference_kind}")


def _resolve_locator_path(locator: str, *, allow_media_relative: bool) -> str:
    raw_path = Path(locator)
    if raw_path.is_absolute():
        return _require_readable_file(raw_path, original_locator=locator)
    if allow_media_relative:
        return _resolve_media_relative_path(locator)
    raise RawFileReferenceError(f"Raw-file path is not readable: {locator}")


def _resolve_media_relative_path(locator: str) -> str:
    media_root = Path(settings.MEDIA_ROOT).resolve(strict=False)
    candidate = (media_root / locator).resolve(strict=False)
    try:
        candidate.relative_to(media_root)
    except ValueError as exc:
        raise RawFileReferenceError(f"Raw-file path escapes MEDIA_ROOT: {locator}") from exc
    return _require_readable_file(candidate, original_locator=locator)


def _require_readable_file(path: Path, *, original_locator: str) -> str:
    normalized = path.resolve(strict=False)
    if not normalized.is_file() or not os.access(normalized, os.R_OK):
        raise RawFileReferenceError(f"Raw-file path is not readable: {original_locator}")
    return str(normalized)


def _source_filename(raw_file_reference: Optional[RawFileReference], source_path: str) -> str:
    if raw_file_reference and raw_file_reference.original_filename:
        return raw_file_reference.original_filename
    return os.path.basename(source_path)


def _resolve_coordinate_column(df: pd.DataFrame, explicit_column: Optional[str]) -> str:
    if explicit_column:
        if explicit_column in df.columns:
            return explicit_column
        if "Angle" in df.columns and explicit_column == "Angle":
            return "Angle"
        raise PatternParseError(f"Coordinate column {explicit_column!r} is not present in the parsed table")
    if "Angle" in df.columns:
        return "Angle"
    return _auto_detect_numeric_column(df, role="coordinate")


def _resolve_intensity_column(
    df: pd.DataFrame,
    explicit_column: Optional[str],
    coordinate_column: str,
) -> str:
    if explicit_column:
        if explicit_column in df.columns:
            return explicit_column
        if "Intensity" in df.columns and explicit_column == "Intensity":
            return "Intensity"
        raise PatternParseError(f"Intensity column {explicit_column!r} is not present in the parsed table")
    if "Intensity" in df.columns:
        return "Intensity"
    return _auto_detect_numeric_column(df, role="intensity", exclude={coordinate_column})


def _auto_detect_numeric_column(
    df: pd.DataFrame,
    *,
    role: str,
    exclude: Optional[set[str]] = None,
) -> str:
    excluded = exclude or set()
    candidates = [col for col in df.columns if col not in excluded]
    numeric_candidates = [col for col in candidates if pd.api.types.is_numeric_dtype(df[col])]
    if not numeric_candidates:
        raise PatternParseError(f"No numeric columns are available for automatic {role} detection")

    lowered = {col: str(col).strip().lower().replace(" ", "_") for col in numeric_candidates}
    if role == "coordinate":
        for key in ("Angle",):
            if key in numeric_candidates:
                return key
        for pattern_type, hints in _COORDINATE_HINTS.items():
            for col, normalized in lowered.items():
                if any(hint in normalized for hint in hints):
                    return col
    else:
        if "Intensity" in numeric_candidates:
            return "Intensity"
        for col, normalized in lowered.items():
            if any(hint in normalized for hint in _INTENSITY_HINTS):
                return col

    return numeric_candidates[0]


def _coerce_series(
    coordinate_series: pd.Series,
    intensity_series: pd.Series,
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    coordinates: list[Optional[float]] = []
    intensities: list[Optional[float]] = []
    for raw_coordinate, raw_intensity in zip(coordinate_series.tolist(), intensity_series.tolist()):
        coordinates.append(_coerce_optional_float(raw_coordinate))
        intensities.append(_coerce_optional_float(raw_intensity))
    return coordinates, intensities


def _coerce_optional_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _coordinate_order(coordinates: Iterable[Optional[float]]) -> CoordinateOrder:
    numeric = [value for value in coordinates if value is not None and math.isfinite(value)]
    if not numeric:
        return "empty"
    if len(numeric) == 1 or all(math.isclose(numeric[0], value) for value in numeric[1:]):
        return "constant"
    if all(numeric[idx] <= numeric[idx + 1] for idx in range(len(numeric) - 1)):
        return "ascending"
    if all(numeric[idx] >= numeric[idx + 1] for idx in range(len(numeric) - 1)):
        return "descending"
    return "unsorted"


def _merge_duplicate_coordinates(
    coordinates: np.ndarray,
    intensities: np.ndarray,
    *,
    tolerance: float,
) -> tuple[np.ndarray, np.ndarray, int]:
    if coordinates.size == 0:
        return coordinates, intensities, 0

    merged_coordinates: list[float] = [float(coordinates[0])]
    merged_intensities: list[float] = [float(intensities[0])]
    duplicate_groups = 0

    for coordinate, intensity in zip(coordinates[1:], intensities[1:]):
        if abs(float(coordinate) - merged_coordinates[-1]) <= tolerance:
            merged_intensities[-1] += float(intensity)
            duplicate_groups += 1
        else:
            merged_coordinates.append(float(coordinate))
            merged_intensities.append(float(intensity))

    return (
        np.asarray(merged_coordinates, dtype=float),
        np.asarray(merged_intensities, dtype=float),
        duplicate_groups,
    )


def _resolve_coordinate_type(
    *,
    coordinate_type: Optional[CoordinateType],
    coordinate_column: str,
    parser_type: PatternParserType,
    pattern_type: PatternType,
) -> CoordinateType:
    if coordinate_type is not None:
        return coordinate_type
    if pattern_type == "stick" or parser_type in {
        "loop_csv",
        "generic_csv",
        "generic_txt",
        "rigaku_ascii",
        "reflection_card",
        "binary_raw",
    }:
        return "two_theta"
    lowered = coordinate_column.strip().lower().replace(" ", "_")
    for candidate, hints in _COORDINATE_HINTS.items():
        if any(hint in lowered for hint in hints):
            return candidate  # type: ignore[return-value]
    return "two_theta"


def _derive_coordinate_views(
    coordinates: np.ndarray,
    *,
    coordinate_type: CoordinateType,
    wavelength_angstrom: Optional[float],
) -> tuple[
    Optional[np.ndarray],
    Optional[np.ndarray],
    Optional[np.ndarray],
    list[str],
    list[XRDAnalysisWarning],
]:
    cleanup_steps: list[str] = []
    warnings: list[XRDAnalysisWarning] = []

    if coordinates.size == 0:
        return None, None, None, cleanup_steps, warnings

    if coordinate_type == "two_theta":
        two_theta = coordinates.astype(float, copy=True)
        d_spacing = None
        q_values = None
        if wavelength_angstrom is None:
            warnings.append(
                _warning(
                    "missing_wavelength_for_conversion",
                    "wavelength_angstrom",
                    "Two-theta values were preserved, but d-spacing and Q could not be derived without wavelength metadata.",
                    stage="pattern_parsing",
                )
            )
        else:
            theta_radians = np.radians(two_theta / 2.0)
            sin_theta = np.sin(theta_radians)
            valid = (two_theta > 0.0) & (two_theta < 180.0) & (sin_theta > 0.0)
            if np.any(~valid):
                warnings.append(
                    _warning(
                        "coordinates_out_of_bounds",
                        "original_coordinates",
                        "Some two-theta values were outside the physically valid conversion range.",
                        stage="pattern_parsing",
                    )
                )
            d_spacing = np.full(two_theta.shape, np.nan, dtype=float)
            q_values = np.full(two_theta.shape, np.nan, dtype=float)
            d_spacing[valid] = wavelength_angstrom / (2.0 * sin_theta[valid])
            q_values[valid] = (4.0 * math.pi * sin_theta[valid]) / wavelength_angstrom
            d_spacing = _drop_invalid_converted(d_spacing, two_theta, field_name="d_spacing")
            q_values = _drop_invalid_converted(q_values, two_theta, field_name="q")
        return two_theta, d_spacing, q_values, cleanup_steps, warnings

    if coordinate_type == "d_spacing":
        d_spacing = coordinates.astype(float, copy=True)
        valid = d_spacing > 0.0
        if np.any(~valid):
            warnings.append(
                _warning(
                    "coordinates_out_of_bounds",
                    "original_coordinates",
                    "Some d-spacing values were non-positive and cannot support physical coordinate conversion.",
                    stage="pattern_parsing",
                )
            )
        q_values = np.where(valid, (2.0 * math.pi) / d_spacing, np.nan)
        two_theta = None
        if wavelength_angstrom is None:
            warnings.append(
                _warning(
                    "missing_wavelength_for_conversion",
                    "wavelength_angstrom",
                    "D-spacing values were preserved, but two-theta could not be derived without wavelength metadata.",
                    stage="pattern_parsing",
                )
            )
        else:
            ratio = wavelength_angstrom / (2.0 * d_spacing)
            theta = np.full(d_spacing.shape, np.nan, dtype=float)
            valid_ratio = valid & (ratio > 0.0) & (ratio <= 1.0)
            if np.any(valid & ~valid_ratio):
                warnings.append(
                    _warning(
                        "coordinates_out_of_bounds",
                        "original_coordinates",
                        "Some d-spacing values imply an impossible two-theta geometry for the supplied wavelength.",
                        stage="pattern_parsing",
                    )
                )
            theta[valid_ratio] = np.degrees(2.0 * np.arcsin(ratio[valid_ratio]))
            two_theta = _drop_invalid_converted(theta, d_spacing, field_name="two_theta")
        return two_theta, d_spacing, _drop_invalid_converted(q_values, d_spacing, field_name="q"), cleanup_steps, warnings

    if coordinate_type == "q":
        q_values = coordinates.astype(float, copy=True)
        valid = q_values > 0.0
        if np.any(~valid):
            warnings.append(
                _warning(
                    "coordinates_out_of_bounds",
                    "original_coordinates",
                    "Some Q values were non-positive and cannot support physical coordinate conversion.",
                    stage="pattern_parsing",
                )
            )
        d_spacing = np.where(valid, (2.0 * math.pi) / q_values, np.nan)
        two_theta = None
        if wavelength_angstrom is None:
            warnings.append(
                _warning(
                    "missing_wavelength_for_conversion",
                    "wavelength_angstrom",
                    "Q values were preserved, but two-theta could not be derived without wavelength metadata.",
                    stage="pattern_parsing",
                )
            )
        else:
            ratio = (q_values * wavelength_angstrom) / (4.0 * math.pi)
            theta = np.full(q_values.shape, np.nan, dtype=float)
            valid_ratio = valid & (ratio > 0.0) & (ratio <= 1.0)
            if np.any(valid & ~valid_ratio):
                warnings.append(
                    _warning(
                        "coordinates_out_of_bounds",
                        "original_coordinates",
                        "Some Q values imply an impossible two-theta geometry for the supplied wavelength.",
                        stage="pattern_parsing",
                    )
                )
            theta[valid_ratio] = np.degrees(2.0 * np.arcsin(ratio[valid_ratio]))
            two_theta = _drop_invalid_converted(theta, q_values, field_name="two_theta")
        return two_theta, _drop_invalid_converted(d_spacing, q_values, field_name="d_spacing"), q_values, cleanup_steps, warnings

    raise CoordinateConversionError(f"Unsupported coordinate_type {coordinate_type!r}")


def _drop_invalid_converted(
    converted: np.ndarray,
    source_coordinates: np.ndarray,
    *,
    field_name: str,
) -> Optional[np.ndarray]:
    valid = np.isfinite(converted)
    if not np.any(valid):
        return None
    if np.count_nonzero(~valid) == converted.size:
        raise CoordinateConversionError(
            f"Could not derive any physically valid {field_name} coordinates from the parsed pattern"
        )
    return converted


def _active_coordinates(
    two_theta_coordinates: Optional[Iterable[float]],
    original_coordinates: Iterable[Any],
) -> list[float]:
    if two_theta_coordinates is not None:
        return [
            float(value)
            for value in two_theta_coordinates
            if value is not None and math.isfinite(float(value))
        ]
    return [
        float(value)
        for value in original_coordinates
        if value is not None and math.isfinite(float(value))
    ]


def _step_metrics(coordinates: list[float]) -> tuple[Optional[float], Optional[float]]:
    if len(coordinates) < 2:
        return None, None
    steps = np.diff(np.asarray(coordinates, dtype=float))
    steps = steps[steps > 0]
    if steps.size == 0:
        return None, None
    median = float(np.median(steps))
    if median == 0.0:
        return median, None
    variation = float(np.std(steps) / median)
    return median, variation


def _signal_metrics(coordinates: np.ndarray, intensities: np.ndarray) -> tuple[Optional[float], int]:
    if intensities.size < 5:
        return None, 0

    smooth_window = _odd_window(intensities.size, preferred=11)
    baseline_window = _odd_window(intensities.size, preferred=max(11, intensities.size // 5))
    smoothed = savgol_filter(intensities, smooth_window, polyorder=2, mode="interp")
    baseline = savgol_filter(smoothed, baseline_window, polyorder=2, mode="interp")
    residual = intensities - baseline
    noise = float(np.std(residual - np.median(residual)))
    signal = float(np.max(np.maximum(residual, 0.0))) if residual.size else 0.0
    if noise <= 0.0:
        snr = None if signal <= 0.0 else float("inf")
    else:
        snr = float(signal / noise)

    median_step, _ = _step_metrics(coordinates.tolist())
    distance = 1
    if median_step and coordinates.size > 1:
        spacing = float(np.median(np.diff(coordinates)))
        if spacing > 0.0:
            distance = max(1, int(round(0.2 / spacing)))
    prominence = max(noise * 2.5, signal * 0.1, 1e-9)
    peaks, _ = find_peaks(np.maximum(residual, 0.0), prominence=prominence, distance=distance)
    return snr, int(peaks.size)


def _odd_window(length: int, *, preferred: int) -> int:
    window = min(max(preferred, 5), length if length % 2 == 1 else length - 1)
    if window < 3:
        return 3 if length >= 3 else max(length, 1)
    return window if window % 2 == 1 else max(window - 1, 3)


def _missing_interval_count(
    coordinates: np.ndarray,
    median_step_size: Optional[float],
    qc: QualityControlSettingsPlaceholder,
) -> int:
    if coordinates.size < 3 or not median_step_size or median_step_size <= 0.0:
        return 0
    steps = np.diff(coordinates)
    return int(np.count_nonzero(steps > (median_step_size * qc.missing_interval_multiplier)))


def _detect_clipping(intensities: np.ndarray, qc: QualityControlSettingsPlaceholder) -> bool:
    if intensities.size == 0:
        return False
    max_intensity = float(np.max(intensities))
    if max_intensity <= 0.0:
        return False
    clipped_fraction = float(
        np.mean(intensities >= (max_intensity * qc.saturation_relative_level))
    )
    return clipped_fraction >= qc.clipping_fraction_threshold


def _is_flat_signal(intensities: np.ndarray, qc: QualityControlSettingsPlaceholder) -> bool:
    if intensities.size == 0:
        return True
    median = float(np.median(np.abs(intensities)))
    if median == 0.0:
        return float(np.std(intensities)) == 0.0
    return float(np.std(intensities) / median) < qc.flat_signal_relative_std_threshold


def _metadata_validation_warnings(
    parsed_pattern: ParsedPatternMetadata,
    analysis_input: XRDAnalysisInput,
    *,
    step_size_variation: Optional[float],
    missing_interval_count: int,
    negative_fraction: float,
    non_positive_fraction: float,
    clipping_detected: bool,
    configuration: XRDAnalysisConfig,
) -> list[XRDAnalysisWarning]:
    qc = configuration.quality_control
    warnings: list[XRDAnalysisWarning] = []
    if parsed_pattern.usable_point_count == 0:
        warnings.append(
            _warning(
                "invalid_rows_removed",
                "usable_point_count",
                "No usable diffraction rows remained after cleanup.",
                stage="quality_control",
            )
        )
        return warnings

    active_coordinates = _active_coordinates(
        parsed_pattern.normalized_two_theta,
        parsed_pattern.original_coordinates,
    )
    coordinate_min = min(active_coordinates)
    coordinate_max = max(active_coordinates)

    if analysis_input.scan_min is not None and abs(coordinate_min - analysis_input.scan_min) > qc.range_mismatch_tolerance:
        warnings.append(
            _warning(
                "parsed_range_mismatch",
                "scan_min",
                "Parsed coordinate minimum differs materially from the recorded scan minimum.",
                stage="quality_control",
            )
        )
    if analysis_input.scan_max is not None and abs(coordinate_max - analysis_input.scan_max) > qc.range_mismatch_tolerance:
        warnings.append(
            _warning(
                "parsed_range_mismatch",
                "scan_max",
                "Parsed coordinate maximum differs materially from the recorded scan maximum.",
                stage="quality_control",
            )
        )
    if (
        analysis_input.step_size is not None
        and parsed_pattern.median_step_size is not None
        and analysis_input.step_size > 0.0
    ):
        relative_delta = abs(parsed_pattern.median_step_size - analysis_input.step_size) / analysis_input.step_size
        if relative_delta > qc.step_size_relative_tolerance:
            warnings.append(
                _warning(
                    "parsed_step_size_mismatch",
                    "step_size",
                    "Measured step size differs materially from the recorded step size.",
                    stage="quality_control",
                )
            )
    if step_size_variation is not None and step_size_variation > qc.irregular_step_variation_warning_ratio:
        warnings.append(
            _warning(
                "irregular_step_spacing",
                "step_size_variation",
                "Step spacing is irregular relative to the measured median step size.",
                stage="quality_control",
            )
        )
    if negative_fraction > qc.negative_intensity_fraction_warning:
        warnings.append(
            _warning(
                "high_negative_intensity_fraction",
                "negative_intensity_fraction",
                "A large fraction of intensities are negative.",
                stage="quality_control",
            )
        )
    if non_positive_fraction >= 1.0:
        warnings.append(
            _warning(
                "all_non_positive_intensity",
                "non_positive_intensity_fraction",
                "All intensities are non-positive.",
                stage="quality_control",
            )
        )
    if missing_interval_count > 0:
        warnings.append(
            _warning(
                "missing_intervals",
                "missing_interval_count",
                "Large coordinate gaps suggest missing intervals or severe discontinuities.",
                stage="quality_control",
            )
        )
    if clipping_detected:
        warnings.append(
            _warning(
                "possible_intensity_clipping",
                "clipping_detected",
                "Intensity values appear clipped or saturated near the measurement maximum.",
                stage="quality_control",
            )
        )
    if parsed_pattern.pattern_type == "continuous" and _is_flat_signal(np.asarray(parsed_pattern.normalized_intensities, dtype=float), qc):
        warnings.append(
            _warning(
                "constant_intensity",
                "normalized_intensities",
                "The parsed pattern is constant or nearly constant in intensity.",
                stage="quality_control",
            )
        )
    return warnings


def _warning(
    code: str,
    field_name: str,
    message: str,
    *,
    stage: str,
) -> XRDAnalysisWarning:
    return XRDAnalysisWarning(
        code=code,
        message=message,
        severity="warning",
        field=field_name,
        stage=stage,  # type: ignore[arg-type]
    )


def _metadata_items(raw_metadata: Iterable[tuple[str, str]]) -> tuple[MetadataItem, ...]:
    return tuple(MetadataItem(key=str(key), value=str(value)) for key, value in raw_metadata)


def _infer_parser_type(
    filename: str,
    parser_metadata: list[tuple[str, str]],
    df: pd.DataFrame,
) -> PatternParserType:
    ext = os.path.splitext(filename or "")[1].lower()
    metadata_map = {str(key): str(value) for key, value in parser_metadata}
    if ext == ".raw":
        return "binary_raw"
    if df.attrs.get("plot_style") == "stick":
        return "reflection_card"
    if metadata_map.get("Source") == "Rigaku ASCII export":
        return "rigaku_ascii"
    if "Angle,Intensity" in Path(filename).name or parser_metadata:
        if any(key == "K-Alpha1 wavelength" for key, _value in parser_metadata):
            return "loop_csv"
    if ext == ".csv":
        return "generic_csv"
    return "generic_txt"


def _unique_ordered(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in QUALITY_FAILURE_CODES:
            continue
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _unique_warning_objects(warnings: list[XRDAnalysisWarning]) -> list[XRDAnalysisWarning]:
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[XRDAnalysisWarning] = []
    for warning in warnings:
        key = (warning.code, warning.field, warning.stage, warning.message)
        if key in seen:
            continue
        seen.add(key)
        unique.append(warning)
    return unique
