"""
GSAS-II-backed helpers for Rietveld refinement and element quantification.

Important limitation:
Rietveld refinement needs one or more crystallographic phase models (typically
provided as CIF files). An XRD pattern plus a bare element list is not enough
to determine elemental amounts in a physically meaningful way. This module
therefore accepts the user-supplied element list for validation/reporting, but
requires explicit phase models for the actual refinement.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

from .gsas_runtime import (
    GSASRuntimeError,
    cleanup_paths,
    clear_sample_scale_refinement,
    configure_gsas as _runtime_configure_gsas,
    new_project as _runtime_new_project,
    prepare_project_path,
    read_project_bytes,
    resolve_instrument_parameter_file as _runtime_resolve_instrument_parameter_file,
    set_project_cycles,
    write_temp_xye as _runtime_write_temp_xye,
)
from .utils import parse_columnar_xrd_text

DEFAULT_BACKGROUND_COEFFS = 6
DEFAULT_REFINEMENT_CYCLES = 6
DEFAULT_INDEXING_PEAKS = 24
DEFAULT_WAVELENGTH_ANGSTROM = 1.5406
COD_SEARCH_URL = "https://www.crystallography.net/cod/result"
COD_ENTRY_URL_TEMPLATE = "https://www.crystallography.net/cod/{cod_id}.cif"
_ANGLE_HEADER = "Angle,Intensity"
_ELEMENT_RE = re.compile(r"[A-Z][a-z]?")
_BRAVAIS_NAMES = [
    "Cubic-F",
    "Cubic-I",
    "Cubic-P",
    "Trigonal-R",
    "Trigonal/Hexagonal-P",
    "Tetragonal-I",
    "Tetragonal-P",
    "Orthorhombic-F",
    "Orthorhombic-I",
    "Orthorhombic-A",
    "Orthorhombic-B",
    "Orthorhombic-C",
    "Orthorhombic-P",
    "Monoclinic-I",
    "Monoclinic-A",
    "Monoclinic-C",
    "Monoclinic-P",
    "Triclinic",
]


class RietveldRefinementError(RuntimeError):
    """Raised when a Rietveld refinement cannot be completed safely."""


@dataclass(frozen=True)
class PhaseModel:
    """
    Description of a crystallographic phase supplied to GSAS-II.

    Args:
        cif_path: Path to a phase file GSAS-II can import, usually a CIF.
        name: Optional phase name shown in the project and result payload.
        formula: Optional formula used when element stoichiometry should be
            taken from chemistry text rather than atom sites.
        elements: Optional explicit element stoichiometry mapping.
        fmthint: Optional GSAS-II import hint. Defaults to ``"CIF"``.
        refine_cell: Refine unit-cell parameters for this phase.
        refine_size: Refine isotropic crystallite size for this phase.
        refine_microstrain: Refine isotropic microstrain for this phase.
    """

    cif_path: str
    name: str | None = None
    formula: str | None = None
    elements: dict[str, float] | None = None
    fmthint: str | None = "CIF"
    refine_cell: bool = True
    refine_size: bool = False
    refine_microstrain: bool = False


def _configure_gsas(gsas2_path: str | None = None):
    try:
        return _runtime_configure_gsas(gsas2_path)
    except GSASRuntimeError as exc:
        raise RietveldRefinementError(str(exc)) from exc


def _new_project(G2sc: Any, gpx_path: str):
    return _runtime_new_project(G2sc, gpx_path)


def _resolve_instrument_parameter_file(
    instrument_parameter_path: str | None = None,
    instrument_label: str = "CuKa lab data",
    gsas2_path: str | None = None,
) -> tuple[str, bool]:
    try:
        return _runtime_resolve_instrument_parameter_file(
            instrument_parameter_path=instrument_parameter_path,
            instrument_label=instrument_label,
            gsas2_path=gsas2_path,
            missing_message=(
                "No GSAS-II instrument parameter file is configured. Set "
                "GSAS2_INSTPRM_PATH or provide instrument_parameter_path explicitly."
            ),
        )
    except GSASRuntimeError as exc:
        raise RietveldRefinementError(
            str(exc)
        ) from exc


def _coerce_phase_model(value: PhaseModel | Mapping[str, Any] | str) -> PhaseModel:
    if isinstance(value, PhaseModel):
        return value
    if isinstance(value, str):
        return PhaseModel(cif_path=value)
    if isinstance(value, Mapping):
        return PhaseModel(
            cif_path=str(value["cif_path"]),
            name=value.get("name"),
            formula=value.get("formula"),
            elements=dict(value["elements"]) if value.get("elements") else None,
            fmthint=value.get("fmthint", "CIF"),
            refine_cell=bool(value.get("refine_cell", True)),
            refine_size=bool(value.get("refine_size", False)),
            refine_microstrain=bool(value.get("refine_microstrain", False)),
        )
    raise TypeError(f"Unsupported phase model: {type(value)!r}")


def _clean_element_symbol(raw: str) -> str:
    match = _ELEMENT_RE.search((raw or "").strip())
    if not match:
        return ""
    return match.group(0)


def _normalize_requested_elements(
    elements: Mapping[str, float] | Sequence[str] | str,
) -> dict[str, float]:
    """
    Accept either a mapping of element ratios, a list of symbols, or a simple
    string such as ``"Co, Cu, Mn, O"`` or ``"Co200_Cu200_Mn200__rocksalt"``.
    """

    if isinstance(elements, Mapping):
        normalized: dict[str, float] = {}
        for symbol, amount in elements.items():
            cleaned = _clean_element_symbol(str(symbol))
            if not cleaned:
                continue
            value = float(amount)
            if value <= 0:
                raise ValueError(f"Element ratio for {cleaned} must be positive.")
            normalized[cleaned] = value
        if normalized:
            return normalized
        raise ValueError("Elements mapping cannot be empty.")

    if isinstance(elements, str):
        compact = elements.strip()
        if not compact:
            raise ValueError("Elements string cannot be empty.")
        parsed = dict(
            (match.group(1), float(match.group(2)))
            for match in re.finditer(r"([A-Z][a-z]?)(\d+(?:\.\d+)?)", compact)
        )
        if parsed:
            return parsed
        raw_symbols = re.split(r"[\s,;/|]+", compact.replace("__", " "))
        return _normalize_requested_elements([item for item in raw_symbols if item])

    normalized = {}
    for symbol in elements:
        cleaned = _clean_element_symbol(str(symbol))
        if cleaned:
            normalized[cleaned] = 1.0
    if not normalized:
        raise ValueError("At least one element symbol is required.")
    return normalized


def _normalize_optional_elements(
    elements: Mapping[str, float] | Sequence[str] | str | None,
) -> dict[str, float]:
    if elements is None:
        return {}
    if isinstance(elements, Mapping) and len(elements) == 0:
        return {}
    if isinstance(elements, str) and not elements.strip():
        return {}
    if isinstance(elements, Sequence) and not isinstance(elements, (str, bytes)) and len(elements) == 0:
        return {}
    return _normalize_requested_elements(elements)


def _read_text(source: str | os.PathLike[str] | Any) -> str:
    if hasattr(source, "read"):
        content = source.read()
        if hasattr(source, "seek"):
            source.seek(0)
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="ignore")
        return str(content)
    return Path(source).read_text(encoding="utf-8", errors="ignore")


def _extract_loop_metadata(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    header_index = None
    for idx, line in enumerate(lines):
        if line.strip() == _ANGLE_HEADER:
            header_index = idx
            break
    if header_index is None:
        return []

    metadata = []
    for line in lines[:header_index]:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 1:
            metadata.append((parts[0], ""))
        else:
            metadata.append((parts[0], ", ".join(parts[1:])))
    return metadata


def _infer_wavelength_from_text(text: str, default: float = DEFAULT_WAVELENGTH_ANGSTROM) -> float:
    metadata = _extract_loop_metadata(text)
    for key, value in metadata:
        key_lower = key.lower()
        if "k-alpha1 wavelength" in key_lower or key_lower == "wavelength":
            try:
                return float(value)
            except ValueError:
                pass

    inline_patterns = (
        r"lambda\s*=\s*([0-9]+(?:\.[0-9]+)?)",
        r"wavelength\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)",
    )
    for pattern in inline_patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass
    return float(default)


def _parse_loop_csv(text: str) -> pd.DataFrame:
    lines = text.splitlines()
    header_index = None
    for idx, line in enumerate(lines):
        if line.strip() == _ANGLE_HEADER:
            header_index = idx
            break
    if header_index is None:
        raise ValueError("CSV header 'Angle,Intensity' not found.")
    data_str = "\n".join(lines[header_index:])
    return pd.read_csv(io.StringIO(data_str))


def _load_xrd_dataframe(xrd_source: str | os.PathLike[str] | Any) -> pd.DataFrame:
    """
    Load an XRD pattern from either the project's CSV format or a generic
    whitespace/comma-delimited 2/3-column powder file.
    """

    if isinstance(xrd_source, pd.DataFrame):
        return xrd_source.copy()

    text = _read_text(xrd_source)
    if _ANGLE_HEADER in text:
        return _parse_loop_csv(text)

    return parse_columnar_xrd_text(text)


def _infer_wavelength(
    xrd_source: str | os.PathLike[str] | Any,
    default: float = DEFAULT_WAVELENGTH_ANGSTROM,
) -> float:
    if isinstance(xrd_source, pd.DataFrame):
        return float(default)
    return _infer_wavelength_from_text(_read_text(xrd_source), default=default)


def _normalize_pattern(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    required = {"Angle", "Intensity"}
    missing = required.difference(df.columns)
    if missing:
        missing_list = ", ".join(sorted(missing))
        raise ValueError(f"Missing required XRD columns: {missing_list}")

    theta = np.asarray(df["Angle"], dtype=float)
    intensity = np.asarray(df["Intensity"], dtype=float)
    if theta.ndim != 1 or intensity.ndim != 1 or theta.size != intensity.size:
        raise ValueError("Angle and Intensity must be 1D arrays of equal length.")
    if theta.size < 5:
        raise ValueError("Need at least 5 data points to run refinement.")

    order = np.argsort(theta)
    return theta[order], intensity[order]


def _parse_formula(formula: str) -> dict[str, float]:
    """
    Parse a chemical formula into a stoichiometry mapping.

    Supports nested parentheses and hydrate separators such as ``CuSO4·5H2O``.
    """

    cleaned = (formula or "").strip()
    if not cleaned:
        raise ValueError("Formula cannot be empty.")

    # A plain ``.`` is also the decimal separator in high-entropy formulas
    # such as ``(Co0.2Cr0.2Fe0.2Mn0.2Ni0.2)O``.  Splitting on every period
    # silently turned those amounts into hydrate segments.  The middle dot is
    # unambiguous; retain support for a plain hydrate dot only when it is not
    # between two digits.
    segments = re.split(r"·|(?<!\d)\.(?!\d)", cleaned.replace(" ", ""))
    total: dict[str, float] = {}

    def add_scaled(target: dict[str, float], source: dict[str, float], scale: float) -> None:
        for element, value in source.items():
            target[element] = target.get(element, 0.0) + value * scale

    def parse_group(text: str, index: int = 0) -> tuple[dict[str, float], int]:
        composition: dict[str, float] = {}
        while index < len(text):
            char = text[index]
            if char in "([":  # start subgroup
                nested, index = parse_group(text, index + 1)
                multiplier, index = parse_number(text, index)
                add_scaled(composition, nested, multiplier)
                continue
            if char in ")]":  # end subgroup
                return composition, index + 1

            element_match = re.match(r"[A-Z][a-z]?", text[index:])
            if not element_match:
                raise ValueError(f"Unsupported formula segment near: {text[index:]!r}")
            element = element_match.group(0)
            index += len(element)
            multiplier, index = parse_number(text, index)
            composition[element] = composition.get(element, 0.0) + multiplier

        return composition, index

    def parse_number(text: str, index: int) -> tuple[float, int]:
        number_match = re.match(r"\d+(?:\.\d+)?", text[index:])
        if not number_match:
            return 1.0, index
        return float(number_match.group(0)), index + len(number_match.group(0))

    for segment in segments:
        if not segment:
            continue
        prefix_match = re.match(r"^(\d+(?:\.\d+)?)(.*)$", segment)
        scale = 1.0
        body = segment
        if prefix_match:
            scale = float(prefix_match.group(1))
            body = prefix_match.group(2)
        parsed, next_index = parse_group(body, 0)
        if next_index != len(body):
            raise ValueError(f"Failed to parse full formula: {formula!r}")
        add_scaled(total, parsed, scale)

    if not total:
        raise ValueError(f"Could not parse formula: {formula!r}")
    return total


def _phase_composition_from_atoms(phase: Any) -> dict[str, float]:
    composition: dict[str, float] = {}
    for atom in getattr(phase, "atoms", lambda: [])():
        element = _clean_element_symbol(getattr(atom, "element", "") or getattr(atom, "type", ""))
        if not element:
            continue
        contribution = float(getattr(atom, "occupancy", 1.0)) * float(getattr(atom, "mult", 1.0))
        if contribution <= 0:
            continue
        composition[element] = composition.get(element, 0.0) + contribution
    return composition


def _phase_composition_from_cif(path: str | os.PathLike[str]) -> dict[str, float]:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    for key in ("_chemical_formula_sum", "_chemical_formula_structural"):
        match = re.search(rf"{re.escape(key)}\s+['\"]?([^'\n\"]+)['\"]?", text)
        if match:
            return _parse_formula(match.group(1))
    raise ValueError(f"Could not infer a chemical formula from {path}.")


def _infer_phase_composition(model: PhaseModel, phase: Any) -> dict[str, float]:
    if model.elements:
        return _normalize_requested_elements(model.elements)
    if model.formula:
        return _parse_formula(model.formula)
    try:
        return _phase_composition_from_cif(model.cif_path)
    except Exception:
        from_atoms = _phase_composition_from_atoms(phase)
        if from_atoms:
            return from_atoms
        raise


def _write_temp_xye(theta: np.ndarray, intensity: np.ndarray, sigma: np.ndarray) -> str:
    return _runtime_write_temp_xye(theta, intensity, sigma)


def _sigma_from_dataframe(df: pd.DataFrame, intensity: np.ndarray) -> np.ndarray:
    sigma = np.asarray(df["Sigma"], dtype=float) if "Sigma" in df.columns else np.sqrt(np.clip(intensity, 1.0, None))
    return np.where(np.isfinite(sigma) & (sigma > 0), sigma, 1.0)


def _media_root() -> Path:
    try:
        from django.conf import settings

        return Path(settings.MEDIA_ROOT)
    except Exception:
        return Path(os.environ.get("MEDIA_ROOT", Path(__file__).resolve().parent.parent / "media"))


def _media_subdir(name: str) -> Path:
    path = _media_root() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _artifact_stem(xrd_source: str | os.PathLike[str] | Any, default: str = "xrd_pattern") -> str:
    if isinstance(xrd_source, pd.DataFrame):
        return default

    source_name = ""
    if isinstance(xrd_source, (str, os.PathLike)):
        path = Path(xrd_source)
        parts = [part for part in (path.parent.name, path.stem) if part]
        source_name = "__".join(parts)
    elif hasattr(xrd_source, "name"):
        name_value = str(getattr(xrd_source, "name", "") or "")
        if name_value:
            path = Path(name_value)
            parts = [part for part in (path.parent.name, path.stem) if part]
            source_name = "__".join(parts)

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_name.strip())
    return safe or default


def _build_indexing_peak_list(
    theta: np.ndarray,
    intensity: np.ndarray,
    *,
    wavelength: float,
    max_peaks: int = DEFAULT_INDEXING_PEAKS,
) -> list[list[float | bool]]:
    """
    Create a GSAS-II-style peak list for indexing from a powder pattern.

    Each entry uses the shape expected by ``GSASIIindex.DoIndexPeaks()``,
    where the final two columns are the observed and calculated d-spacings.
    """

    if theta.size < 5:
        raise ValueError("Need at least 5 points to extract indexing peaks.")

    step = float(np.median(np.diff(theta))) if theta.size > 1 else 0.02
    distance_points = max(1, int(round(0.25 / max(step, 1e-6))))
    dynamic_range = float(np.max(intensity) - np.min(intensity))
    prominence = max(float(np.std(intensity)) * 1.2, dynamic_range * 0.03, 1.0e-6)
    indices, props = find_peaks(
        intensity,
        prominence=prominence,
        distance=distance_points,
    )

    if indices.size == 0:
        raise RietveldRefinementError("No significant peaks were found for indexing.")

    ranked = sorted(
        zip(indices.tolist(), props.get("prominences", np.zeros_like(indices)).tolist()),
        key=lambda item: (item[1], float(intensity[item[0]])),
        reverse=True,
    )[:max_peaks]
    chosen = sorted(idx for idx, _prom in ranked)
    return _build_indexing_peak_list_from_pairs(
        ((float(theta[idx]), float(intensity[idx])) for idx in chosen),
        wavelength=wavelength,
    )


def _build_indexing_peak_row(
    two_theta: float,
    peak_intensity: float,
    *,
    wavelength: float,
) -> list[float | bool] | None:
    half_angle = np.radians(two_theta / 2.0)
    if half_angle <= 0:
        return None

    d_obs = float(wavelength / (2.0 * np.sin(half_angle)))
    return [
        two_theta,
        peak_intensity,
        True,
        False,
        0,
        0,
        0,
        d_obs,
        d_obs,
    ]


def _build_indexing_peak_list_from_pairs(
    peaks: Iterable[tuple[float, float]],
    *,
    wavelength: float,
) -> list[list[float | bool]]:
    rows = [
        row
        for row in (
            _build_indexing_peak_row(float(two_theta), float(peak_intensity), wavelength=wavelength)
            for two_theta, peak_intensity in peaks
        )
        if row is not None
    ]
    if not rows:
        raise RietveldRefinementError("Unable to compute d-spacings for indexing peaks.")

    rows.sort(key=lambda row: float(row[-2]), reverse=True)
    return rows


def _build_indexing_peak_list_from_positions(
    peak_positions: Sequence[float],
    theta: np.ndarray,
    intensity: np.ndarray,
    *,
    wavelength: float,
) -> list[list[float | bool]]:
    return _build_indexing_peak_list_from_pairs(
        (
            (float(peak_pos), float(np.interp(float(peak_pos), theta, intensity)))
            for peak_pos in peak_positions
        ),
        wavelength=wavelength,
    )


def _generate_cubic_reflection_sequence(bravais_name: str, *, max_index: int = 12) -> list[int]:
    values: set[int] = set()
    for h in range(max_index + 1):
        for k in range(h + 1):
            for l in range(k + 1):
                if h == k == l == 0:
                    continue
                if bravais_name == "Cubic-I" and (h + k + l) % 2:
                    continue
                if bravais_name == "Cubic-F" and not (h % 2 == k % 2 == l % 2):
                    continue
                values.add(h * h + k * k + l * l)
    return sorted(values)


def _match_sparse_simple_lattices(
    indexing_peaks: Sequence[Sequence[float | bool]],
    *,
    bravais_flags: Sequence[bool] | None = None,
    max_start_offset: int = 6,
    max_relative_std: float = 0.06,
) -> list[dict[str, Any]]:
    """
    Match a sparse peak set against simple cubic prototype lattices.

    When only a handful of peaks are available, full generic indexing can be
    underconstrained. This helper instead tests cubic P/I/F reflection series
    directly and scores how consistently they imply a single lattice parameter.
    """

    if len(indexing_peaks) < 4 or len(indexing_peaks) > 6:
        return []

    d_obs = np.array([float(row[-2]) for row in indexing_peaks if float(row[-2]) > 0], dtype=float)
    if d_obs.size < 4:
        return []

    q_obs = 1.0 / np.square(d_obs)
    candidates: list[dict[str, Any]] = []
    bravais_options = [
        (0, "Cubic-F"),
        (1, "Cubic-I"),
        (2, "Cubic-P"),
    ]

    for bravais_index, bravais_name in bravais_options:
        if bravais_flags is not None and (
            bravais_index >= len(bravais_flags) or not bool(bravais_flags[bravais_index])
        ):
            continue
        sequence = _generate_cubic_reflection_sequence(bravais_name)
        max_offset = min(max_start_offset, len(sequence) - d_obs.size)
        for start in range(max_offset + 1):
            n_values = np.array(sequence[start : start + d_obs.size], dtype=float)
            a_values = np.sqrt(n_values / q_obs)
            mean_a = float(np.mean(a_values))
            if not np.isfinite(mean_a) or mean_a <= 0:
                continue

            rel_std = float(np.std(a_values) / mean_a)
            if rel_std > max_relative_std:
                continue

            q_fit = n_values / (mean_a * mean_a)
            rms_relative = float(np.sqrt(np.mean(np.square((q_obs - q_fit) / q_obs))))

            # Prefer assignments that use more peaks, need fewer skipped low-order
            # reflections, and imply a very consistent lattice parameter.
            m20 = max(
                0.1,
                (d_obs.size * 18.0)
                - (start * 2.5)
                - (rel_std * 700.0)
                - (rms_relative * 450.0),
            )
            candidates.append(
                {
                    "bravais_index": bravais_index,
                    "bravais_name": bravais_name,
                    "crystal_system": "cubic",
                    "m20": float(m20),
                    "x20": float(start),
                    "unit_cell": {
                        "length_a": mean_a,
                        "length_b": mean_a,
                        "length_c": mean_a,
                        "angle_alpha": 90.0,
                        "angle_beta": 90.0,
                        "angle_gamma": 90.0,
                        "volume": float(mean_a**3),
                    },
                    "generated_hkls": int(d_obs.size),
                    "space_group_candidates": [],
                    "sparse_match": {
                        "relative_std": rel_std,
                        "rms_relative": rms_relative,
                        "assigned_n": [int(value) for value in n_values.tolist()],
                    },
                }
            )

    candidates.sort(
        key=lambda item: (
            -float(item["m20"]),
            float(item.get("x20", 0.0)),
        )
    )
    return _dedupe_candidate_cells(candidates)


def _extract_indexing_peak_list(
    df: pd.DataFrame,
    theta: np.ndarray,
    intensity: np.ndarray,
    *,
    wavelength: float,
    max_peaks: int,
    gsas2_path: str | None = None,
    instrument_parameter_path: str | None = None,
    instrument_label: str = "CuKa lab data",
    limits: tuple[float, float] | None = None,
    use_gsas_peak_finder: bool = True,
    overlay_output_path: str | None = None,
) -> tuple[list[list[float | bool]], list[str], str | None]:
    warnings: list[str] = []
    overlay_path = overlay_output_path if overlay_output_path else None

    def _build_local_peak_result() -> tuple[list[list[float | bool]], list[str], str | None]:
        try:
            from .gsas_tools import peak_finder as local_peak_finder
        except ImportError:
            try:
                from gsas_tools import peak_finder as local_peak_finder  # type: ignore
            except ImportError:
                local_peak_finder = None

        if local_peak_finder is not None:
            try:
                peaks, _gpx_bytes, _overlay = local_peak_finder(
                    df,
                    max_peaks=max_peaks,
                    use_gsas=False,
                    overlay_output_path=overlay_output_path,
                )
                peak_positions = [
                    float(peak["two_theta"])
                    for peak in peaks
                    if peak.get("two_theta") is not None
                ]
                if peak_positions:
                    return (
                        _build_indexing_peak_list_from_positions(
                            peak_positions,
                            theta,
                            intensity,
                            wavelength=wavelength,
                        ),
                        warnings,
                        overlay_path if overlay_path and Path(overlay_path).exists() else None,
                    )
            except Exception:
                pass

        return (
            _build_indexing_peak_list(
                theta,
                intensity,
                wavelength=wavelength,
                max_peaks=max_peaks,
            ),
            warnings,
            overlay_path if overlay_path and Path(overlay_path).exists() else None,
        )

    if use_gsas_peak_finder:
        try:
            from .gsas_tools import peak_finder as gsas_peak_finder
        except ImportError:
            try:
                from gsas_tools import peak_finder as gsas_peak_finder  # type: ignore
            except ImportError:
                gsas_peak_finder = None
                warnings.append("GSAS-II peak finder could not be imported; using SciPy peak picking.")
        if gsas_peak_finder is not None:
            try:
                peaks, _gpx_bytes, _overlay = gsas_peak_finder(
                    df,
                    gsas2_path=gsas2_path,
                    instrument_parameter_path=instrument_parameter_path,
                    instrument_label=instrument_label,
                    limits=limits,
                    max_peaks=max_peaks,
                    use_gsas=True,
                    overlay_output_path=overlay_output_path,
                )
                peak_positions = [float(peak["two_theta"]) for peak in peaks if peak.get("two_theta") is not None]
                if peak_positions:
                    return (
                        _build_indexing_peak_list_from_positions(
                            peak_positions,
                            theta,
                            intensity,
                            wavelength=wavelength,
                        ),
                        warnings,
                        overlay_path,
                    )
                warnings.append("GSAS-II peak finder returned no peaks; using SciPy peak picking.")
            except Exception as exc:
                warnings.append(f"GSAS-II peak finder failed; using SciPy peak picking ({exc}).")

    return _build_local_peak_result()


def _coerce_bravais_flags(bravais_search: Sequence[str] | None = None) -> list[bool]:
    if not bravais_search:
        return [True] * len(_BRAVAIS_NAMES)

    normalized = {str(item).strip().lower() for item in bravais_search if str(item).strip()}
    if not normalized:
        return [True] * len(_BRAVAIS_NAMES)

    flags = [name.lower() in normalized for name in _BRAVAIS_NAMES]
    if not any(flags):
        available = ", ".join(_BRAVAIS_NAMES)
        raise ValueError(f"No requested Bravais lattices matched. Available values: {available}")
    return flags


def _crystal_system_from_bravais(bravais_name: str) -> str:
    lowered = bravais_name.lower()
    if lowered.startswith("cubic"):
        return "cubic"
    if lowered.startswith("trigonal") or lowered.startswith("hexagonal"):
        return "trigonal/hexagonal"
    if lowered.startswith("tetragonal"):
        return "tetragonal"
    if lowered.startswith("orthorhombic"):
        return "orthorhombic"
    if lowered.startswith("monoclinic"):
        return "monoclinic"
    return "triclinic"


def _phase_space_group(phase: Any) -> str | None:
    general = getattr(phase, "data", {}).get("General", {})
    sg_data = general.get("SGData", {}) if isinstance(general, dict) else {}
    value = sg_data.get("SpGrp")
    return str(value) if value else None


def _phase_atom_refinement_map(phase: Any, flags: str) -> dict[str, str]:
    atom_map: dict[str, str] = {}
    for atom in getattr(phase, "atoms", lambda: [])():
        label = getattr(atom, "label", "") or getattr(atom, "name", "")
        if label:
            atom_map[str(label)] = flags
    return atom_map


def _normalize_export_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "").strip())
    return safe or "phase"


def _to_builtin(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _to_builtin(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_to_builtin(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_to_builtin(item) for item in value)
    if isinstance(value, np.generic):
        return value.item()
    return value


def _dedupe_preserving_order(values: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _dedupe_candidate_cells(cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for cell in cells:
        unit_cell = cell["unit_cell"]
        key = (
            cell["bravais_name"],
            round(float(unit_cell["length_a"]), 4),
            round(float(unit_cell["length_b"]), 4),
            round(float(unit_cell["length_c"]), 4),
            round(float(unit_cell["angle_alpha"]), 3),
            round(float(unit_cell["angle_beta"]), 3),
            round(float(unit_cell["angle_gamma"]), 3),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(dict(cell))
    return deduped


def _quality_band_from_wr(weighted_r_factor: float | None) -> str | None:
    if weighted_r_factor is None:
        return None
    wr = float(weighted_r_factor)
    if wr < 10:
        return "excellent"
    if wr < 20:
        return "good"
    if wr < 35:
        return "fair"
    return "poor"


def _build_structure_summary(
    candidate_cells: Sequence[Mapping[str, Any]],
    phase_results: Sequence[Mapping[str, Any]],
    weighted_r_factor: float | None,
    solution_mode: str,
) -> dict[str, Any]:
    top_candidates = _rank_candidate_cells(candidate_cells, phase_results)[:5]
    best_candidate = top_candidates[0] if top_candidates else None
    refined_phases = []
    for phase in phase_results:
        refined_phases.append(
            {
                "name": phase["name"],
                "space_group": phase.get("space_group"),
                "unit_cell": phase.get("unit_cell"),
                "exported_cif_path": phase.get("exported_cif_path"),
                "solution_strategy": phase.get("solution_strategy"),
            }
        )

    return {
        "solution_mode": solution_mode,
        "candidate_count": len(candidate_cells),
        "best_candidate_cell": best_candidate,
        "top_candidate_cells": top_candidates,
        "refined_phase_count": len(phase_results),
        "refined_phases": refined_phases,
        "weighted_r_factor": None if weighted_r_factor is None else float(weighted_r_factor),
        "fit_quality": _quality_band_from_wr(weighted_r_factor),
    }


def _cell_metric_distance(candidate: Mapping[str, Any], target_cell: Mapping[str, Any]) -> float:
    cand_cell = candidate["unit_cell"]
    cand_lengths = sorted(
        [
            float(cand_cell["length_a"]),
            float(cand_cell["length_b"]),
            float(cand_cell["length_c"]),
        ]
    )
    target_lengths = sorted(
        [
            float(target_cell["length_a"]),
            float(target_cell["length_b"]),
            float(target_cell["length_c"]),
        ]
    )

    length_error = sum(
        abs(cand - target) / max(abs(target), 1.0e-6)
        for cand, target in zip(cand_lengths, target_lengths)
    ) / 3.0

    cand_angles = [
        float(cand_cell["angle_alpha"]),
        float(cand_cell["angle_beta"]),
        float(cand_cell["angle_gamma"]),
    ]
    target_angles = [
        float(target_cell["angle_alpha"]),
        float(target_cell["angle_beta"]),
        float(target_cell["angle_gamma"]),
    ]
    angle_error = sum(abs(cand - target) for cand, target in zip(cand_angles, target_angles)) / 180.0

    cand_volume = float(cand_cell["volume"])
    target_volume = float(target_cell["volume"])
    volume_error = abs(cand_volume - target_volume) / max(abs(target_volume), 1.0e-6)
    return length_error + angle_error + volume_error


def _candidate_rank_score(
    candidate: Mapping[str, Any],
    phase_results: Sequence[Mapping[str, Any]],
) -> float:
    m20 = float(candidate["m20"])
    x20 = float(candidate["x20"])
    generated_hkls = float(candidate.get("generated_hkls") or 0.0)
    volume = float(candidate["unit_cell"]["volume"])

    score = np.log1p(max(m20, 0.0))
    score += 0.04 * generated_hkls
    score -= 0.60 * x20

    if volume < 40:
        score -= 8.0
    elif volume < 80:
        score -= 3.0

    if phase_results:
        target_distances = [
            _cell_metric_distance(candidate, phase["unit_cell"])
            for phase in phase_results
            if phase.get("unit_cell")
        ]
        if target_distances:
            score -= 8.0 * min(target_distances)

    return float(score)


def _rank_candidate_cells(
    candidate_cells: Sequence[Mapping[str, Any]],
    phase_results: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    ranked = [dict(cell) for cell in candidate_cells]
    ranked.sort(
        key=lambda cell: (
            _candidate_rank_score(cell, phase_results),
            float(cell["m20"]),
            -float(cell["x20"]),
        ),
        reverse=True,
    )
    return ranked


def _rank_cod_query_candidates(candidate_cells: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    ranked = [dict(cell) for cell in candidate_cells]
    ranked.sort(
        key=lambda cell: (
            -float(cell["m20"]),
            float(cell.get("x20", 0.0)),
            float(cell["unit_cell"]["volume"]),
            -float(cell.get("generated_hkls") or 0.0),
        )
    )
    return ranked


def _parse_cod_formula(formula: str) -> dict[str, float]:
    cleaned = (formula or "").strip()
    if not cleaned:
        return {}
    parts = re.findall(r"([A-Z][a-z]?)\s*([0-9]+(?:\.[0-9]+)?)?", cleaned)
    parsed: dict[str, float] = {}
    for symbol, amount_text in parts:
        parsed[symbol] = parsed.get(symbol, 0.0) + float(amount_text or 1.0)
    return parsed


def _element_symbols_from_formula(formula: str) -> set[str]:
    return set(_parse_cod_formula(formula).keys())


def _build_cod_search_params(
    candidate_cell: Mapping[str, Any],
    requested_elements: Mapping[str, float],
    *,
    length_tol_angstrom: float = 0.08,
    angle_tol_deg: float = 1.5,
    volume_tol_fraction: float = 0.12,
    include_theoretical: bool = False,
) -> dict[str, str]:
    cell = candidate_cell["unit_cell"]
    volume = float(cell["volume"])
    params = {"format": "json"}
    for axis, key in (("a", "length_a"), ("b", "length_b"), ("c", "length_c")):
        center = float(cell[key])
        params[f"{axis}min"] = f"{center - length_tol_angstrom:.5f}"
        params[f"{axis}max"] = f"{center + length_tol_angstrom:.5f}"
    for prefix, key in (("alp", "angle_alpha"), ("bet", "angle_beta"), ("gam", "angle_gamma")):
        center = float(cell[key])
        params[f"{prefix}min"] = f"{center - angle_tol_deg:.5f}"
        params[f"{prefix}max"] = f"{center + angle_tol_deg:.5f}"
    params["vmin"] = f"{volume * (1.0 - volume_tol_fraction):.5f}"
    params["vmax"] = f"{volume * (1.0 + volume_tol_fraction):.5f}"
    if requested_elements:
        params["strictmin"] = str(len(requested_elements))
        params["strictmax"] = str(len(requested_elements))
        for idx, symbol in enumerate(sorted(requested_elements), start=1):
            params[f"el{idx}"] = symbol
    if include_theoretical:
        params["include_theoretical"] = "1"
    return params


def _cod_entry_id(entry: Mapping[str, Any]) -> str | None:
    for key in ("file", "id", "codid"):
        value = entry.get(key)
        if value:
            return str(value)
    return None


def _fetch_cod_entries(
    params: Mapping[str, str],
    *,
    timeout_seconds: float = 20.0,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{COD_SEARCH_URL}?{query}")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read().decode("utf-8")
    parsed = json.loads(payload)
    if isinstance(parsed, list):
        return [dict(item) for item in parsed]
    if isinstance(parsed, dict):
        for key in ("data", "results", "entries"):
            value = parsed.get(key)
            if isinstance(value, list):
                return [dict(item) for item in value]
    return []


def _score_cod_entry(
    entry: Mapping[str, Any],
    candidate_cell: Mapping[str, Any],
    requested_elements: Mapping[str, float],
) -> dict[str, Any]:
    try:
        entry_cell = {
            "unit_cell": {
                "length_a": float(entry["a"]),
                "length_b": float(entry["b"]),
                "length_c": float(entry["c"]),
                "angle_alpha": float(entry["alpha"]),
                "angle_beta": float(entry["beta"]),
                "angle_gamma": float(entry["gamma"]),
                "volume": float(entry["vol"]),
            }
        }
    except Exception as exc:
        raise ValueError(f"COD entry is missing cell parameters: {exc}") from exc

    cell_distance = _cell_metric_distance(entry_cell, candidate_cell["unit_cell"])
    formula = str(entry.get("formula") or "")
    formula_elements = _element_symbols_from_formula(formula)
    requested_set = set(requested_elements)
    if requested_set:
        exact_element_match = formula_elements == requested_set if formula_elements else False
        all_requested_present = requested_set.issubset(formula_elements) if formula_elements else False
    else:
        exact_element_match = False
        all_requested_present = False

    score = 10.0
    score -= 8.0 * cell_distance
    if requested_set:
        if exact_element_match:
            score += 4.0
        elif all_requested_present:
            score += 1.5
        else:
            score -= 5.0

    return {
        "cod_id": _cod_entry_id(entry),
        "score": float(score),
        "cell_distance": float(cell_distance),
        "exact_element_match": exact_element_match,
        "all_requested_present": all_requested_present,
        "formula": formula,
        "space_group": entry.get("sg") or entry.get("spacegroup"),
        "entry": dict(entry),
    }


def _download_cod_cif(
    cod_id: str,
    destination_dir: str | os.PathLike[str],
    *,
    timeout_seconds: float = 20.0,
) -> str:
    dest_dir = Path(destination_dir).expanduser().resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    destination = dest_dir / f"cod_{cod_id}.cif"
    with urllib.request.urlopen(COD_ENTRY_URL_TEMPLATE.format(cod_id=cod_id), timeout=timeout_seconds) as response:
        destination.write_bytes(response.read())
    return str(destination)


def _index_candidate_cells(
    indexing_peaks: Sequence[Sequence[float | bool]],
    bravais_flags: Sequence[bool],
    *,
    volume_guess: float,
    gsas2_path: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    _configure_gsas(gsas2_path)
    from GSASII import GSASIIindex as G2index  # type: ignore

    controls = [0.0, 0.0, 4, float(volume_guess)]
    candidate_cells: list[dict[str, Any]] = []
    warnings: list[str] = []

    for bravais_index, enabled in enumerate(bravais_flags):
        if not enabled:
            continue

        family_flags = [False] * len(_BRAVAIS_NAMES)
        family_flags[bravais_index] = True
        bravais_name = _BRAVAIS_NAMES[bravais_index]

        try:
            indexing_ok, _dmin, indexed_cells = G2index.DoIndexPeaks(
                indexing_peaks,
                controls[:],
                family_flags,
                dlg=None,
                return_Nc=True,
            )
        except FloatingPointError as exc:
            warnings.append(
                f"Indexing failed for {bravais_name}: non-physical cell generated during search ({exc})."
            )
            continue
        except Exception as exc:
            warnings.append(f"Indexing failed for {bravais_name}: {exc}")
            continue

        if not indexing_ok:
            continue

        for cell in indexed_cells:
            candidate_cells.append(
                {
                    "bravais_index": bravais_index,
                    "bravais_name": bravais_name,
                    "crystal_system": _crystal_system_from_bravais(bravais_name),
                    "m20": float(cell[0]),
                    "x20": float(cell[1]),
                    "unit_cell": {
                        "length_a": float(cell[3]),
                        "length_b": float(cell[4]),
                        "length_c": float(cell[5]),
                        "angle_alpha": float(cell[6]),
                        "angle_beta": float(cell[7]),
                        "angle_gamma": float(cell[8]),
                        "volume": float(cell[9]),
                    },
                    "generated_hkls": int(cell[12]) if len(cell) > 12 else None,
                    "space_group_candidates": [],
                }
            )

    candidate_cells.sort(key=lambda item: item["m20"], reverse=True)
    return _dedupe_candidate_cells(candidate_cells), warnings


def _solve_candidate_cells(
    indexing_peaks: Sequence[Sequence[float | bool]],
    bravais_flags: Sequence[bool],
    *,
    volume_guess: float,
    gsas2_path: str | None = None,
    sparse_warning: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    if len(indexing_peaks) <= 6:
        sparse_candidates = _match_sparse_simple_lattices(
            indexing_peaks,
            bravais_flags=bravais_flags,
        )
        if sparse_candidates:
            warnings.append(sparse_warning)
            return sparse_candidates, warnings

    candidate_cells, indexing_warnings = _index_candidate_cells(
        indexing_peaks,
        bravais_flags,
        volume_guess=volume_guess,
        gsas2_path=gsas2_path,
    )
    warnings.extend(indexing_warnings)
    return candidate_cells, warnings


def _extract_candidate_cells_with_retry(
    df: pd.DataFrame,
    theta: np.ndarray,
    intensity: np.ndarray,
    *,
    wavelength: float,
    max_peaks: int,
    bravais_flags: Sequence[bool],
    volume_guess: float,
    gsas2_path: str | None = None,
    instrument_parameter_path: str | None = None,
    instrument_label: str = "CuKa lab data",
    limits: tuple[float, float] | None = None,
    use_gsas_peak_finder: bool = True,
    overlay_output_path: str | None = None,
) -> tuple[list[list[float | bool]], list[dict[str, Any]], list[str], str | None]:
    indexing_peaks, peak_warnings, overlay_path = _extract_indexing_peak_list(
        df,
        theta,
        intensity,
        wavelength=wavelength,
        max_peaks=max_peaks,
        gsas2_path=gsas2_path,
        instrument_parameter_path=instrument_parameter_path,
        instrument_label=instrument_label,
        limits=limits,
        use_gsas_peak_finder=use_gsas_peak_finder,
        overlay_output_path=overlay_output_path,
    )
    candidate_cells, warnings = _solve_candidate_cells(
        indexing_peaks,
        bravais_flags,
        volume_guess=volume_guess,
        gsas2_path=gsas2_path,
        sparse_warning="Sparse peak mode used a direct simple-lattice matcher before generic indexing.",
    )
    warnings = list(peak_warnings) + warnings

    if candidate_cells or not use_gsas_peak_finder:
        if not candidate_cells:
            warnings.append(
                "No indexable unit cell was found from SciPy-picked peaks."
                if not use_gsas_peak_finder
                else "No indexable unit cell was found from either GSAS-II-refined peaks or SciPy-picked peaks."
            )
        return indexing_peaks, candidate_cells, warnings, overlay_path

    retry_peaks, retry_peak_warnings, retry_overlay_path = _extract_indexing_peak_list(
        df,
        theta,
        intensity,
        wavelength=wavelength,
        max_peaks=max_peaks,
        gsas2_path=gsas2_path,
        instrument_parameter_path=instrument_parameter_path,
        instrument_label=instrument_label,
        limits=limits,
        use_gsas_peak_finder=False,
        overlay_output_path=overlay_output_path,
    )
    retry_cells, retry_warnings = _solve_candidate_cells(
        retry_peaks,
        bravais_flags,
        volume_guess=volume_guess,
        gsas2_path=gsas2_path,
        sparse_warning="Sparse peak mode used a direct simple-lattice matcher after SciPy peak picking.",
    )
    warnings.extend(retry_peak_warnings)
    warnings.extend(retry_warnings)

    if retry_cells:
        warnings.append(
            "GSAS-II peak-based indexing returned no candidate cells; retried indexing with SciPy peak picking."
        )
        return retry_peaks, retry_cells, warnings, retry_overlay_path or overlay_path

    warnings.append(
        "No indexable unit cell was found from either GSAS-II-refined peaks or SciPy-picked peaks."
    )
    return indexing_peaks, candidate_cells, warnings, retry_overlay_path or overlay_path


def _extract_phase_fraction(phase: Any, histogram: Any) -> float:
    if hasattr(phase, "getHAPvalues"):
        hap_values = phase.getHAPvalues(histogram)
        scale_value = hap_values.get("Scale") or hap_values.get("PhaseFraction")
        if isinstance(scale_value, (list, tuple)) and scale_value:
            return float(scale_value[0])
        if scale_value is not None:
            return float(scale_value)

    if hasattr(phase, "HAPvalue"):
        try:
            value = phase.HAPvalue("Scale", targethistlist=[histogram])
        except TypeError:
            value = phase.HAPvalue("Scale", None, [histogram])
        if isinstance(value, (list, tuple)) and value:
            return float(value[0])
        if value is not None:
            return float(value)

    raise RietveldRefinementError("Unable to read refined phase fractions from GSAS-II.")


def _normalize_fractions(values: Mapping[str, float]) -> dict[str, float]:
    positive = {key: float(val) for key, val in values.items() if float(val) > 0}
    total = sum(positive.values())
    if total <= 0:
        raise RietveldRefinementError("GSAS-II returned no positive phase fractions.")
    return {key: value / total for key, value in positive.items()}


def _aggregate_element_fractions(
    phase_results: Sequence[Mapping[str, Any]],
    requested_elements: Mapping[str, float],
) -> tuple[dict[str, float], dict[str, float], list[str], list[str]]:
    all_element_amounts: dict[str, float] = {}
    for phase_result in phase_results:
        phase_fraction = float(phase_result["normalized_phase_fraction"])
        for element, stoich in phase_result["phase_composition"].items():
            all_element_amounts[element] = all_element_amounts.get(element, 0.0) + phase_fraction * float(stoich)

    if not all_element_amounts:
        raise RietveldRefinementError("No element stoichiometry could be derived from the phase models.")

    model_fractions = _normalize_fractions(all_element_amounts)

    requested_set = set(requested_elements)
    missing_requested = sorted(requested_set.difference(model_fractions))
    unexpected_phase_elements = sorted(set(model_fractions).difference(requested_set))

    requested_only = {
        element: value
        for element, value in model_fractions.items()
        if element in requested_set
    }
    requested_fractions = _normalize_fractions(requested_only) if requested_only else {}
    return model_fractions, requested_fractions, missing_requested, unexpected_phase_elements


def _phase_hap_refinements(model: PhaseModel) -> dict[str, Any]:
    hap_refinements: dict[str, Any] = {"Scale": True}
    if model.refine_size:
        hap_refinements["Size"] = {"type": "isotropic", "refine": True}
    if model.refine_microstrain:
        hap_refinements["Mustrain"] = {"type": "isotropic", "refine": True}
    return hap_refinements


def _phase_refinement_options(
    model: PhaseModel,
    phase_obj: Any,
    refine_atom_flags: str | None,
) -> dict[str, Any]:
    phase_refinements: dict[str, Any] = {}
    if model.refine_cell:
        phase_refinements["Cell"] = True
    if refine_atom_flags:
        atom_map = _phase_atom_refinement_map(phase_obj, refine_atom_flags)
        if atom_map:
            phase_refinements["Atoms"] = atom_map
    return phase_refinements


def _run_template_refinement(
    df: pd.DataFrame,
    theta: np.ndarray,
    intensity: np.ndarray,
    phase_models: Sequence[PhaseModel],
    *,
    gsas2_path: str | None = None,
    instrument_parameter_path: str | None = None,
    instrument_label: str = "CuKa lab data",
    limits: tuple[float, float] | None = None,
    background_coeffs: int = DEFAULT_BACKGROUND_COEFFS,
    refinement_cycles: int = DEFAULT_REFINEMENT_CYCLES,
    project_path: str | None = None,
    export_root: Path | None = None,
    export_prefix: str | None = None,
    refine_atom_flags: str | None = None,
    include_phase_fractions: bool = False,
) -> dict[str, Any]:
    sigma = _sigma_from_dataframe(df, intensity)
    G2sc = _configure_gsas(gsas2_path)
    xye_path = _write_temp_xye(theta, intensity, sigma)
    instprm_path, instprm_is_temp = _resolve_instrument_parameter_file(
        instrument_parameter_path=instrument_parameter_path,
        instrument_label=instrument_label,
        gsas2_path=gsas2_path,
    )
    gpx_path, gpx_is_temp = prepare_project_path(project_path)

    try:
        project = _new_project(G2sc, gpx_path)
        histogram = project.add_powder_histogram(xye_path, iparams=instprm_path, fmthint="xye")
        fit_limits = list(limits) if limits else [float(theta.min()), float(theta.max())]
        histogram.set_refinements(
            {
                "Limits": fit_limits,
                "Background": {
                    "type": "chebyschev-1",
                    "no. coeffs": int(background_coeffs),
                    "refine": True,
                },
            }
        )
        clear_sample_scale_refinement(histogram)

        phase_pairs: list[tuple[PhaseModel, Any]] = []
        for model in phase_models:
            cif_path = str(Path(model.cif_path).expanduser().resolve())
            phase_obj = project.add_phase(
                cif_path,
                phasename=model.name or Path(cif_path).stem,
                histograms=[histogram],
                fmthint=model.fmthint,
            )
            phase_pairs.append((PhaseModel(**{**model.__dict__, "cif_path": cif_path}), phase_obj))

        clear_sample_scale_refinement(histogram)
        set_project_cycles(project, 3)
        project.refine(makeBack=True)

        for model, phase_obj in phase_pairs:
            phase_obj.set_HAP_refinements(_phase_hap_refinements(model), [histogram])
            phase_refinements = _phase_refinement_options(model, phase_obj, refine_atom_flags)
            if phase_refinements:
                phase_obj.set_refinements(phase_refinements)

        set_project_cycles(project, refinement_cycles)
        project.refine(makeBack=True)
        project.save()

        phase_results: list[dict[str, Any]] = []
        cif_exports: list[str] = []
        for model, phase_obj in phase_pairs:
            phase_name = model.name or Path(model.cif_path).stem
            phase_result = {
                "name": phase_name,
                "cif_path": model.cif_path,
                "unit_cell": phase_obj.get_cell() if hasattr(phase_obj, "get_cell") else {},
                "space_group": _phase_space_group(phase_obj),
                "phase_composition": _infer_phase_composition(model, phase_obj),
            }
            if include_phase_fractions:
                phase_result["raw_phase_fraction"] = _extract_phase_fraction(phase_obj, histogram)
            if export_root is not None:
                export_name = _normalize_export_name(phase_name)
                if export_prefix:
                    export_name = f"{_normalize_export_name(export_prefix)}__{export_name}"
                export_path = export_root / f"{export_name}.cif"
                phase_obj.export_CIF(str(export_path))
                phase_result["exported_cif_path"] = str(export_path)
                phase_result["solution_strategy"] = "template_refinement"
                cif_exports.append(str(export_path))
            phase_results.append(phase_result)

        weighted_r_factor = None
        if hasattr(histogram, "get_wR"):
            try:
                weighted_r_factor = histogram.get_wR()
            except Exception:
                weighted_r_factor = None

        return {
            "fit_limits": fit_limits,
            "phase_results": phase_results,
            "cif_exports": cif_exports,
            "weighted_r_factor": weighted_r_factor,
            "project_path": None if gpx_is_temp else gpx_path,
            "gpx_bytes": read_project_bytes(gpx_path),
        }
    finally:
        cleanup_paths(
            (xye_path, True),
            (instprm_path, instprm_is_temp),
            (gpx_path, gpx_is_temp),
        )


def refine_element_amounts(
    xrd_source: str | os.PathLike[str] | Any,
    elements: Mapping[str, float] | Sequence[str] | str,
    *,
    phase_models: Sequence[PhaseModel | Mapping[str, Any] | str],
    gsas2_path: str | None = None,
    instrument_parameter_path: str | None = None,
    instrument_label: str = "CuKa lab data",
    limits: tuple[float, float] | None = None,
    background_coeffs: int = DEFAULT_BACKGROUND_COEFFS,
    refinement_cycles: int = DEFAULT_REFINEMENT_CYCLES,
    project_path: str | None = None,
    export_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """
    Refine phase fractions with GSAS-II and aggregate them to element fractions.

    Returns a dictionary with:
    - ``phase_results``: raw and normalized phase fractions per model
    - ``element_atomic_fractions``: normalized across every element present in the phases
    - ``requested_element_atomic_fractions``: normalized across only the user-requested elements
    - ``weighted_r_factor``: GSAS-II histogram fit statistic when available
    - ``gpx_bytes``: serialized GSAS-II project file contents

    Note that the returned elemental fractions are model-based atomic fractions,
    not direct chemistry measurements from the pattern alone.
    """

    requested_elements = _normalize_requested_elements(elements)
    normalized_models = [_coerce_phase_model(model) for model in phase_models]
    if not normalized_models:
        raise RietveldRefinementError(
            "Rietveld refinement needs at least one crystallographic phase model "
            "(usually a CIF). An XRD file plus only an element list is not enough."
        )

    df = _load_xrd_dataframe(xrd_source).sort_values("Angle", kind="mergesort").reset_index(drop=True)
    theta, intensity = _normalize_pattern(df)
    artifact_stem = _artifact_stem(xrd_source)
    export_root = Path(export_dir).expanduser().resolve() if export_dir else _media_subdir("derived_cifs")
    refinement_result = _run_template_refinement(
        df,
        theta,
        intensity,
        normalized_models,
        gsas2_path=gsas2_path,
        instrument_parameter_path=instrument_parameter_path,
        instrument_label=instrument_label,
        limits=limits,
        background_coeffs=background_coeffs,
        refinement_cycles=refinement_cycles,
        project_path=project_path,
        export_root=export_root,
        export_prefix=artifact_stem,
        include_phase_fractions=True,
    )
    phase_results = list(refinement_result["phase_results"])
    raw_phase_fractions = {
        str(phase_result["name"]): float(phase_result["raw_phase_fraction"])
        for phase_result in phase_results
    }
    normalized_phase_fractions = _normalize_fractions(raw_phase_fractions)
    for phase_result in phase_results:
        phase_result["normalized_phase_fraction"] = normalized_phase_fractions[str(phase_result["name"])]

    model_fractions, requested_fractions, missing_requested, unexpected_phase_elements = (
        _aggregate_element_fractions(phase_results, requested_elements)
    )

    return {
        "requested_elements": requested_elements,
        "fit_limits": refinement_result["fit_limits"],
        "phase_results": phase_results,
        "element_atomic_fractions": model_fractions,
        "requested_element_atomic_fractions": requested_fractions,
        "missing_requested_elements": missing_requested,
        "unexpected_phase_elements": unexpected_phase_elements,
        "weighted_r_factor": refinement_result["weighted_r_factor"],
        "project_path": refinement_result["project_path"],
        "gpx_bytes": refinement_result["gpx_bytes"],
        "cif_exports": refinement_result["cif_exports"],
    }


def derive_structures_from_xrd(
    xrd_source: str | os.PathLike[str] | Any,
    elements: Mapping[str, float] | Sequence[str] | str | None,
    *,
    phase_models: Sequence[PhaseModel | Mapping[str, Any] | str] | None = None,
    gsas2_path: str | None = None,
    instrument_parameter_path: str | None = None,
    instrument_label: str = "CuKa lab data",
    wavelength: float | None = None,
    limits: tuple[float, float] | None = None,
    background_coeffs: int = DEFAULT_BACKGROUND_COEFFS,
    refinement_cycles: int = DEFAULT_REFINEMENT_CYCLES,
    max_indexing_peaks: int = DEFAULT_INDEXING_PEAKS,
    bravais_search: Sequence[str] | None = None,
    volume_guess: float = 200.0,
    export_dir: str | os.PathLike[str] | None = None,
    project_path: str | None = None,
    refine_atom_flags: str | None = None,
    use_gsas_peak_finder: bool = True,
) -> dict[str, Any]:
    """
    Best-effort GSAS-II workflow for extracting structural information from a
    powder pattern and, when template phases are supplied, exporting refined CIFs.

    This function intentionally separates:
    - what can be estimated from powder indexing alone (candidate unit cells),
    - what can only be asserted from an existing structural model (space group),
    - and what is not automated here (ab initio structure solution from scratch).

    When ``phase_models`` are omitted, the result will contain only indexing
    candidates and explanatory warnings. When templates are supplied, the
    function performs a template-assisted refinement and exports refined CIFs.
    """

    requested_elements = _normalize_optional_elements(elements)
    df = _load_xrd_dataframe(xrd_source).sort_values("Angle", kind="mergesort").reset_index(drop=True)
    theta, intensity = _normalize_pattern(df)
    artifact_stem = _artifact_stem(xrd_source)
    overlay_output_path = str(_media_subdir("phase_overlays") / f"{artifact_stem}.png")
    active_wavelength = float(wavelength) if wavelength is not None else _infer_wavelength(xrd_source)
    bravais_flags = _coerce_bravais_flags(bravais_search)
    indexing_peaks, candidate_cells, warnings, phase_overlay_path = _extract_candidate_cells_with_retry(
        df,
        theta,
        intensity,
        wavelength=active_wavelength,
        max_peaks=max_indexing_peaks,
        bravais_flags=bravais_flags,
        volume_guess=volume_guess,
        gsas2_path=gsas2_path,
        instrument_parameter_path=instrument_parameter_path,
        instrument_label=instrument_label,
        limits=limits,
        use_gsas_peak_finder=use_gsas_peak_finder,
        overlay_output_path=overlay_output_path,
    )
    warnings.append(
        "Unit-cell indexing can propose lattice candidates, but space group "
        "cannot be determined uniquely from indexing alone."
    )
    warnings.append(
        "Ab initio structure solution from a powder pattern is not automated "
        "here; template-assisted refinement is supported when phase CIFs are supplied."
    )

    normalized_models = [_coerce_phase_model(model) for model in (phase_models or [])]
    if not normalized_models:
        result = {
            "requested_elements": requested_elements,
            "wavelength": active_wavelength,
            "indexing_peaks": indexing_peaks,
            "candidate_cells": candidate_cells,
            "phase_results": [],
            "cif_exports": [],
            "phase_overlay_path": phase_overlay_path,
            "warnings": warnings,
            "solution_mode": "index_only",
        }
        result["summary"] = _build_structure_summary(
            candidate_cells,
            [],
            None,
            solution_mode="index_only",
        )
        return _to_builtin(result)

    if len(normalized_models) > 1:
        warnings.append(
            "Mixture support is template-assisted only; automatic decomposition "
            "of a mixed powder pattern into unknown phases is not implemented."
        )

    export_root = Path(export_dir).expanduser().resolve() if export_dir else _media_subdir("derived_cifs")
    export_root.mkdir(parents=True, exist_ok=True)
    refinement_result = _run_template_refinement(
        df,
        theta,
        intensity,
        normalized_models,
        gsas2_path=gsas2_path,
        instrument_parameter_path=instrument_parameter_path,
        instrument_label=instrument_label,
        limits=limits,
        background_coeffs=background_coeffs,
        refinement_cycles=refinement_cycles,
        project_path=project_path,
        export_root=export_root,
        export_prefix=artifact_stem,
        refine_atom_flags=refine_atom_flags,
    )
    phase_results = [
        {
            "name": phase_result["name"],
            "template_cif_path": phase_result["cif_path"],
            "exported_cif_path": phase_result.get("exported_cif_path"),
            "space_group": phase_result.get("space_group"),
            "unit_cell": phase_result.get("unit_cell"),
            "phase_composition": phase_result["phase_composition"],
            "solution_strategy": phase_result.get("solution_strategy"),
        }
        for phase_result in refinement_result["phase_results"]
    ]

    result = {
        "requested_elements": requested_elements,
        "wavelength": active_wavelength,
        "indexing_peaks": indexing_peaks,
        "candidate_cells": candidate_cells,
        "phase_results": phase_results,
        "cif_exports": refinement_result["cif_exports"],
        "phase_overlay_path": phase_overlay_path,
        "weighted_r_factor": refinement_result["weighted_r_factor"],
        "warnings": warnings,
        "solution_mode": "template_refinement",
        "project_path": refinement_result["project_path"],
    }
    result["summary"] = _build_structure_summary(
        candidate_cells,
        phase_results,
        refinement_result["weighted_r_factor"],
        solution_mode="template_refinement",
    )
    return _to_builtin(result)


def search_cod_by_indexing_or_refine(
    xrd_source: str | os.PathLike[str] | Any,
    elements: Mapping[str, float] | Sequence[str] | str | None,
    *,
    fallback_phase_models: Sequence[PhaseModel | Mapping[str, Any] | str] | None = None,
    gsas2_path: str | None = None,
    instrument_parameter_path: str | None = None,
    instrument_label: str = "CuKa lab data",
    wavelength: float | None = None,
    limits: tuple[float, float] | None = None,
    background_coeffs: int = DEFAULT_BACKGROUND_COEFFS,
    refinement_cycles: int = DEFAULT_REFINEMENT_CYCLES,
    max_indexing_peaks: int = DEFAULT_INDEXING_PEAKS,
    bravais_search: Sequence[str] | None = None,
    volume_guess: float = 200.0,
    export_dir: str | os.PathLike[str] | None = None,
    project_path: str | None = None,
    refine_atom_flags: str | None = None,
    cod_length_tol_angstrom: float = 0.08,
    cod_angle_tol_deg: float = 1.5,
    cod_volume_tol_fraction: float = 0.12,
    cod_candidate_cells: int = 5,
    cod_hit_limit: int = 5,
    cod_score_threshold: float = 7.0,
    include_theoretical_cod: bool = False,
    use_gsas_peak_finder: bool = True,
) -> dict[str, Any]:
    """
    Search the COD using indexed cell candidates and requested elements.

    If a plausible COD match is found, returns COD metadata and downloads the
    best matching CIF. Otherwise, falls back to ``derive_structures_from_xrd()``
    using the previously defined refinement pathway.
    """

    requested_elements = _normalize_optional_elements(elements)
    indexing_result = derive_structures_from_xrd(
        xrd_source,
        requested_elements,
        phase_models=None,
        gsas2_path=gsas2_path,
        instrument_parameter_path=instrument_parameter_path,
        instrument_label=instrument_label,
        wavelength=wavelength,
        limits=limits,
        background_coeffs=background_coeffs,
        refinement_cycles=refinement_cycles,
        max_indexing_peaks=max_indexing_peaks,
        bravais_search=bravais_search,
        volume_guess=volume_guess,
        use_gsas_peak_finder=use_gsas_peak_finder,
    )

    ranked_candidates = _rank_cod_query_candidates(indexing_result.get("candidate_cells", []))

    export_root = Path(export_dir).expanduser().resolve() if export_dir else _media_subdir("derived_cifs")
    export_root.mkdir(parents=True, exist_ok=True)

    warnings = list(indexing_result.get("warnings", []))
    cod_matches: list[dict[str, Any]] = []

    for candidate in ranked_candidates[:cod_candidate_cells]:
        params = _build_cod_search_params(
            candidate,
            requested_elements,
            length_tol_angstrom=cod_length_tol_angstrom,
            angle_tol_deg=cod_angle_tol_deg,
            volume_tol_fraction=cod_volume_tol_fraction,
            include_theoretical=include_theoretical_cod,
        )
        try:
            entries = _fetch_cod_entries(params)
        except Exception as exc:
            warnings.append(f"COD search failed for {candidate['bravais_name']}: {exc}")
            continue

        for entry in entries:
            try:
                scored = _score_cod_entry(entry, candidate, requested_elements)
            except Exception:
                continue
            scored["matched_candidate_cell"] = candidate
            cod_matches.append(scored)

    deduped_matches: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for match in sorted(cod_matches, key=lambda item: item["score"], reverse=True):
        cod_id = match.get("cod_id")
        if not cod_id or cod_id in seen_ids:
            continue
        seen_ids.add(cod_id)
        deduped_matches.append(match)

    top_matches = deduped_matches[:cod_hit_limit]
    best_match = top_matches[0] if top_matches else None

    if best_match and float(best_match["score"]) >= float(cod_score_threshold):
        downloaded_cif_path = None
        cod_id = best_match.get("cod_id")
        if cod_id:
            try:
                downloaded_cif_path = _download_cod_cif(cod_id, export_root)
            except Exception as exc:
                warnings.append(f"COD CIF download failed for {cod_id}: {exc}")

        result = {
            "status": "cod_match",
            "requested_elements": requested_elements,
            "indexing": indexing_result,
            "cod_match": {
                "cod_id": cod_id,
                "score": best_match["score"],
                "cell_distance": best_match["cell_distance"],
                "formula": best_match["formula"],
                "space_group": best_match["space_group"],
                "downloaded_cif_path": downloaded_cif_path,
                "cif_url": COD_ENTRY_URL_TEMPLATE.format(cod_id=cod_id) if cod_id else None,
                "matched_candidate_cell": best_match["matched_candidate_cell"],
                "entry": best_match["entry"],
            },
            "cod_matches": top_matches,
            "warnings": warnings,
        }
        result["summary"] = {
            "status": "cod_match",
            "best_candidate_cell": best_match["matched_candidate_cell"],
            "cod_id": cod_id,
            "formula": best_match["formula"],
            "space_group": best_match["space_group"],
            "downloaded_cif_path": downloaded_cif_path,
            "cod_score": best_match["score"],
            "cell_distance": best_match["cell_distance"],
        }
        return _to_builtin(result)

    warnings.append("No suitable COD match found; falling back to local refinement pathway.")
    fallback = derive_structures_from_xrd(
        xrd_source,
        requested_elements,
        phase_models=fallback_phase_models,
        gsas2_path=gsas2_path,
        instrument_parameter_path=instrument_parameter_path,
        instrument_label=instrument_label,
        wavelength=wavelength,
        limits=limits,
        background_coeffs=background_coeffs,
        refinement_cycles=refinement_cycles,
        max_indexing_peaks=max_indexing_peaks,
        bravais_search=bravais_search,
        volume_guess=volume_guess,
        export_dir=str(export_root),
        project_path=project_path,
        refine_atom_flags=refine_atom_flags,
        use_gsas_peak_finder=use_gsas_peak_finder,
    )
    fallback["status"] = "fallback_refinement"
    fallback["cod_matches"] = top_matches
    fallback["warnings"] = _dedupe_preserving_order(
        warnings + list(fallback.get("warnings", []))
    )
    fallback_summary = dict(fallback.get("summary", {}))
    fallback_summary["status"] = "fallback_refinement"
    fallback_summary["cod_matches_checked"] = len(top_matches)
    fallback["summary"] = fallback_summary
    return _to_builtin(fallback)


__all__ = [
    "COD_ENTRY_URL_TEMPLATE",
    "COD_SEARCH_URL",
    "PhaseModel",
    "RietveldRefinementError",
    "derive_structures_from_xrd",
    "refine_element_amounts",
    "search_cod_by_indexing_or_refine",
]
