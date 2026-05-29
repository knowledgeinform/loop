"""
GSAS-II helpers for XRD peak finding and peak-overlay rendering.

This module is intentionally import-safe for Django: GSAS-II is only imported
inside helper functions, so the app can still start on machines where GSAS-II
is not installed.
"""

from __future__ import annotations

import argparse
import base64
from pathlib import Path

import numpy as np
from scipy.signal import find_peaks, peak_prominences, peak_widths, savgol_filter

try:
    from .gsas_runtime import (
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
    from .utils import render_overlay_plot, xrd_parse
except ImportError:
    from gsas_runtime import (  # type: ignore
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
    from utils import render_overlay_plot, xrd_parse


DEFAULT_MAX_SEEDED_PEAKS = 24
DEFAULT_SIGNIFICANCE_SIGMAS = 3.0
DEFAULT_MIN_PEAK_SCORE = 4.5
DEFAULT_CLUSTER_DEGREES = 3.0
DEFAULT_SECONDARY_CLUSTER_RATIO = 0.6


def _normalize_pattern(df) -> tuple[np.ndarray, np.ndarray]:
    """
    Validate and normalize the DataFrame columns expected by this module.
    """

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
        raise ValueError("Need at least 5 data points to identify peaks.")

    order = np.argsort(theta)
    return theta[order], intensity[order]


def _odd_window(size: int, minimum: int, maximum: int) -> int:
    """
    Return an odd window size clamped to the given bounds.
    """

    window = max(minimum, min(size, maximum))
    if window % 2 == 0:
        window += 1 if window < maximum else -1
    return max(3, min(window, size if size % 2 == 1 else max(3, size - 1)))


def _build_local_trend(theta: np.ndarray, intensity: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Estimate a smooth local trend and residual for noisy diffraction patterns.

    We intentionally compare each point against a broader local neighborhood so
    slowly varying ramps or rough backgrounds do not get treated as true peaks.
    """

    count = intensity.size
    if count < 7:
        trend = np.asarray(intensity, dtype=float)
        return trend, np.zeros_like(trend)

    smooth_window = _odd_window(max(int(count * 0.015), 7), minimum=7, maximum=61)
    baseline_window = _odd_window(max(int(count * 0.09), 31), minimum=31, maximum=301)

    smoothed = savgol_filter(intensity, smooth_window, polyorder=2, mode="interp")
    baseline = savgol_filter(smoothed, baseline_window, polyorder=2, mode="interp")
    residual = smoothed - baseline
    return baseline, residual


def _estimate_local_noise(residual: np.ndarray, center_index: int, radius: int) -> float:
    """
    Robustly estimate local noise with MAD over a neighborhood.
    """

    start = max(0, center_index - radius)
    stop = min(residual.size, center_index + radius + 1)
    neighborhood = residual[start:stop]
    if neighborhood.size == 0:
        return 0.0

    median = float(np.median(neighborhood))
    mad = float(np.median(np.abs(neighborhood - median)))
    return max(mad * 1.4826, 1e-9)


def _merge_peak_candidates(
    candidates: list[dict[str, float]],
    cluster_degrees: float = DEFAULT_CLUSTER_DEGREES,
) -> list[dict[str, float]]:
    """
    Merge nearby candidate spikes and keep the dominant local representatives.
    """

    if not candidates:
        return []

    clusters: list[list[dict[str, float]]] = []
    for candidate in sorted(candidates, key=lambda item: item["two_theta"]):
        if not clusters or candidate["two_theta"] - clusters[-1][-1]["two_theta"] > cluster_degrees:
            clusters.append([candidate])
        else:
            clusters[-1].append(candidate)

    selected: list[dict[str, float]] = []
    for cluster in clusters:
        ranked = sorted(cluster, key=lambda item: item["score"], reverse=True)
        primary = ranked[0]
        selected.append(primary)
        for alternate in ranked[1:]:
            if abs(alternate["two_theta"] - primary["two_theta"]) < 2.0:
                continue
            if alternate["score"] < DEFAULT_SECONDARY_CLUSTER_RATIO * primary["score"]:
                continue
            selected.append(alternate)
            break

    return sorted(selected, key=lambda item: item["two_theta"])


def _seed_peaks(
    theta: np.ndarray,
    intensity: np.ndarray,
    max_peaks: int = DEFAULT_MAX_SEEDED_PEAKS,
) -> list[tuple[float, float]]:
    """
    Use SciPy to generate stable initial guesses for GSAS-II peak fitting.
    """

    intensity = np.asarray(intensity, dtype=float)
    theta = np.asarray(theta, dtype=float)

    baseline, residual = _build_local_trend(theta, intensity)
    dynamic_range = float(np.max(residual) - np.min(residual))
    if dynamic_range <= 0:
        return []

    global_noise = max(
        float(np.median(np.abs(residual - np.median(residual)))) * 1.4826,
        1e-9,
    )
    prominence = max(global_noise * 2.5, dynamic_range * 0.12, 20.0, 1e-9)
    step = float(np.median(np.diff(theta)))
    distance_degrees = 0.25
    distance_points = max(1, int(round(distance_degrees / step))) if step > 0 else 1

    indices, _ = find_peaks(residual, prominence=prominence, distance=distance_points)
    if indices.size == 0:
        return []

    prominences = peak_prominences(residual, indices)[0]
    widths = peak_widths(residual, indices, rel_height=0.5)[0] * step
    neighborhood_radius = max(distance_points * 3, 10)
    candidates: list[dict[str, float]] = []
    for idx, candidate_prominence, candidate_width in zip(indices, prominences, widths):
        local_noise = _estimate_local_noise(residual, int(idx), neighborhood_radius)
        local_height = float(residual[idx])
        significance = local_height / local_noise if local_noise > 0 else 0.0
        local_contrast = local_height / max(float(baseline[idx]), 1.0)
        score = float(candidate_prominence) * max(float(candidate_width), 0.08)
        if (
            significance < DEFAULT_SIGNIFICANCE_SIGMAS
            or local_contrast < 0.012
            or score < DEFAULT_MIN_PEAK_SCORE
        ):
            continue
        candidates.append(
            {
                "two_theta": float(theta[idx]),
                "intensity": float(intensity[idx]),
                "score": score,
                "significance": significance,
                "contrast": local_contrast,
            }
        )

    if not candidates:
        return []

    selected = _merge_peak_candidates(candidates)
    if len(selected) > max_peaks:
        selected = sorted(selected, key=lambda item: item["score"], reverse=True)[:max_peaks]
        selected.sort(key=lambda item: item["two_theta"])

    return [(candidate["two_theta"], candidate["intensity"]) for candidate in selected]


def _refine_seeded_peaks(
    theta: np.ndarray,
    intensity: np.ndarray,
    seeded: list[tuple[float, float]],
    refined_entries: list[list[float]] | list[tuple[float, ...]] | None,
) -> list[dict[str, float]]:
    """
    Keep only the peaks we explicitly seeded, optionally replacing each with the
    nearest GSAS-refined solution.
    """

    if not seeded:
        return []

    refined_pool: list[dict[str, float]] = []
    for entry in refined_entries or []:
        if not entry:
            continue
        position = float(entry[0])
        area = float(entry[1]) if len(entry) > 1 else float(np.interp(position, theta, intensity))
        refined_pool.append(
            {
                "two_theta": position,
                "intensity": float(np.interp(position, theta, intensity)),
                "area": area,
            }
        )

    aligned: list[dict[str, float]] = []
    used_indices: set[int] = set()
    for peak_theta, peak_height in seeded:
        best_index = None
        best_delta = None
        for idx, candidate in enumerate(refined_pool):
            if idx in used_indices:
                continue
            delta = abs(candidate["two_theta"] - peak_theta)
            if delta > 0.75:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_index = idx

        if best_index is not None:
            used_indices.add(best_index)
            aligned.append(refined_pool[best_index])
        else:
            aligned.append(
                {
                    "two_theta": float(peak_theta),
                    "intensity": float(peak_height),
                    "area": float(peak_height),
                }
            )

    return sorted(aligned, key=lambda peak: float(peak["two_theta"]))


def _refine_local_peak_position(theta: np.ndarray, residual: np.ndarray, index: int) -> float:
    """
    Use a 3-point quadratic fit on the residual to nudge the peak center.
    """

    if index <= 0 or index >= residual.size - 1:
        return float(theta[index])

    y0, y1, y2 = float(residual[index - 1]), float(residual[index]), float(residual[index + 1])
    denom = y0 - 2.0 * y1 + y2
    if abs(denom) < 1e-12:
        return float(theta[index])

    offset = 0.5 * (y0 - y2) / denom
    offset = max(-1.0, min(1.0, offset))
    step = float(theta[index + 1] - theta[index])
    return float(theta[index] + offset * step)


def _local_peak_area(theta: np.ndarray, residual: np.ndarray, index: int, radius: int) -> float:
    """
    Integrate positive residual around a peak until it blends back into trend.
    """

    left = index
    while left > 0 and index - left < radius and residual[left] > 0:
        if residual[left - 1] > residual[left] and residual[left - 1] <= 0:
            break
        left -= 1

    right = index
    last = residual.size - 1
    while right < last and right - index < radius and residual[right] > 0:
        if residual[right + 1] > residual[right] and residual[right + 1] <= 0:
            break
        right += 1

    segment_theta = theta[left : right + 1]
    segment_residual = np.clip(residual[left : right + 1], 0.0, None)
    if segment_theta.size < 2:
        return max(float(segment_residual[0]) if segment_residual.size else 0.0, 0.0)
    return float(np.trapz(segment_residual, segment_theta))


def find_local_peaks(
    df,
    *,
    max_peaks: int = DEFAULT_MAX_SEEDED_PEAKS,
) -> tuple[list[dict[str, float]], bytes]:
    """
    Fast trend-aware peak finding without invoking GSAS-II refinement.

    This is intended for page rendering, where responsive recalculation matters
    more than producing a GSAS project artifact.
    """

    theta, intensity = _normalize_pattern(df)
    baseline, residual = _build_local_trend(theta, intensity)
    seeded = _seed_peaks(theta, intensity, max_peaks=max_peaks)
    if not seeded:
        return [], b""

    step = float(np.median(np.diff(theta))) if theta.size > 1 else 0.1
    radius = max(10, int(round(0.8 / max(step, 1e-6))))

    peaks: list[dict[str, float]] = []
    for peak_theta, _peak_height in seeded:
        index = int(np.searchsorted(theta, peak_theta))
        index = max(0, min(index, theta.size - 1))
        refined_theta = _refine_local_peak_position(theta, residual, index)
        area = _local_peak_area(theta, residual, index, radius)
        peaks.append(
            {
                "two_theta": refined_theta,
                "intensity": float(np.interp(refined_theta, theta, intensity)),
                "area": area,
            }
        )

    return sorted(peaks, key=lambda peak: float(peak["two_theta"])), b""


def find_gsas_peaks(
    df,
    *,
    gsas2_path: str | None = None,
    instrument_parameter_path: str | None = None,
    instrument_label: str = "CuKa lab data",
    limits: tuple[float, float] | None = None,
    max_peaks: int = DEFAULT_MAX_SEEDED_PEAKS,
    project_path: str | None = None,
) -> tuple[list[dict[str, float]], bytes]:
    """
    Fit or seed XRD peaks with GSAS-II and return peak metadata plus GPX bytes.

    Returns:
    - peaks: list of ``{"two_theta": ..., "intensity": ..., "area": ...}``
    - gpx_bytes: serialized project file contents (empty if not written)
    """

    G2sc = configure_gsas(gsas2_path)
    theta, intensity = _normalize_pattern(df)
    sigma = np.ones_like(theta, dtype=float)

    xye_path = write_temp_xye(theta, intensity, sigma)
    instprm_path, instprm_is_temp = resolve_instrument_parameter_file(
        instrument_parameter_path=instrument_parameter_path,
        instrument_label=instrument_label,
        gsas2_path=gsas2_path,
        missing_message=(
            "No GSAS-II instrument parameter file is configured. Set "
            "GSAS2_INSTPRM_PATH or use a GSAS-II install that exposes defaultIparms."
        ),
    )
    gpx_path, gpx_is_temp = prepare_project_path(project_path)

    try:
        project = new_project(G2sc, gpx_path)
        histogram = project.add_powder_histogram(xye_path, iparams=instprm_path, fmthint="xye")

        fit_limits = list(limits) if limits else [float(theta.min()), float(theta.max())]
        histogram.set_refinements(
            {
                "Limits": fit_limits,
                "Background": {
                    "type": "chebyschev-1",
                    "no. coeffs": 3,
                    "refine": True,
                },
            }
        )
        clear_sample_scale_refinement(histogram)
        set_project_cycles(project, 2)

        seeded = _seed_peaks(theta, intensity, max_peaks=max_peaks)
        for peak_theta, peak_height in seeded:
            histogram.add_peak(max(peak_height, 1.0), ttheta=peak_theta)

        if seeded:
            histogram.set_peakFlags(area=True)
            histogram.refine_peaks()
            histogram.set_peakFlags(area=True, pos=True)
            histogram.refine_peaks()

        refined_entries = list(getattr(histogram, "PeakList", []) or [])
        peaks = _refine_seeded_peaks(theta, intensity, seeded, refined_entries)

        project.save()
        return peaks, read_project_bytes(gpx_path)
    finally:
        cleanup_paths(
            (xye_path, True),
            (instprm_path, instprm_is_temp),
            (gpx_path, gpx_is_temp),
        )


def peak_finder_fast(df, *, max_peaks: int = 50) -> tuple[list[dict[str, float]], bytes, str]:
    """SciPy-only peak seeding plus overlay plot — fast enough for interactive HTTP.

    Full GSAS-II refinement (:func:`peak_finder`) can take minutes per pattern; the
    trial detail page uses this by default. Use the **Run full GSAS-II refinement**
    control (adds ``?refine_gsas=1``) or set ``LOOP_GSAS_FULL_SYNC=1`` to force the
    slow path without clicking.
    """

    theta, intensity = _normalize_pattern(df)
    seeded = _seed_peaks(theta, intensity, max_peaks=max_peaks)
    peaks: list[dict[str, float]] = [
        {"two_theta": t, "intensity": h, "area": h} for t, h in seeded
    ]
    return peaks, b"", render_overlay_plot(df, peaks)


def peak_finder(
    df, *,
    gsas2_path: str | None = None,
    instrument_parameter_path: str | None = None,
    instrument_label: str = "CuKa lab data",
    limits: tuple[float, float] | None = None,
    max_peaks: int = DEFAULT_MAX_SEEDED_PEAKS,
    project_path: str | None = None,
    use_gsas: bool = False,
    overlay_output_path: str | None = None,
) -> tuple[list[dict[str, float]], bytes, str]:
    """
    Compatibility wrapper that also returns a rendered overlay plot.
    """

    if use_gsas:
        peaks, gpx_bytes = find_gsas_peaks(
            df,
            gsas2_path=gsas2_path,
            instrument_parameter_path=instrument_parameter_path,
            instrument_label=instrument_label,
            limits=limits,
            max_peaks=max_peaks,
            project_path=project_path,
        )
    else:
        peaks, gpx_bytes = find_local_peaks(
            df,
            max_peaks=max_peaks,
        )
    overlay_uri = render_overlay_plot(df, peaks)
    if overlay_output_path:
        encoded = overlay_uri.split(",", 1)[1]
        output_path = Path(overlay_output_path).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(base64.b64decode(encoded))
    return peaks, gpx_bytes, overlay_uri


def main(argv: list[str] | None = None) -> int:
    """
    Small CLI for testing peak overlays without wiring into Django views first.
    """

    parser = argparse.ArgumentParser(description="Detect and render XRD peaks with GSAS-II.")
    parser.add_argument("csv_path", help="Path to an XRD CSV containing Angle,Intensity columns.")
    parser.add_argument(
        "--png-out",
        help="Optional output path for the rendered peak-overlay image.",
    )
    parser.add_argument(
        "--gpx-out",
        help="Optional output path for the GSAS-II project file.",
    )
    parser.add_argument(
        "--gsas2-path",
        help="Optional path to a GSAS-II checkout root if it is not importable.",
    )
    parser.add_argument(
        "--instprm",
        help="Optional instrument parameter file. Falls back to GSAS2_INSTPRM_PATH or GSAS-II defaults.",
    )
    parser.add_argument(
        "--instrument-label",
        default="CuKa lab data",
        help="Default GSAS-II instrument preset label to use when no instprm file is supplied.",
    )
    args = parser.parse_args(argv)

    _, df = xrd_parse(args.csv_path)
    peaks, gpx_bytes, overlay_uri = peak_finder(
        df,
        gsas2_path=args.gsas2_path,
        instrument_parameter_path=args.instprm,
        instrument_label=args.instrument_label,
        project_path=args.gpx_out,
        use_gsas=True,
    )

    print(f"Detected {len(peaks)} peaks.")
    for peak in peaks:
        print(f"{peak['two_theta']:.4f}\t{peak['intensity']:.2f}")

    if args.gpx_out and gpx_bytes:
        Path(args.gpx_out).write_bytes(gpx_bytes)
        print(f"Wrote GSAS-II project: {args.gpx_out}")

    if args.png_out:
        encoded = overlay_uri.split(",", 1)[1]
        Path(args.png_out).write_bytes(base64.b64decode(encoded))
        print(f"Wrote overlay image: {args.png_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
