"""
Shared GSAS-II runtime helpers used by peak-finding and refinement workflows.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


class GSASRuntimeError(RuntimeError):
    """Raised when the local GSAS-II runtime cannot be prepared safely."""


def resolve_gsas_search_root(gsas2_path: str | None = None) -> Path | None:
    raw_path = gsas2_path or os.getenv("GSAS2_PATH") or os.getenv("GSASII_PATH")
    if not raw_path:
        return None

    candidate = Path(raw_path).expanduser().resolve()
    if candidate.name == "GSASII" and (candidate / "__init__.py").exists():
        return candidate.parent
    if (candidate / "GSASII" / "__init__.py").exists():
        return candidate
    return None


def import_gsas_modules(gsas2_path: str | None = None):
    try:
        from GSASII import GSASIIpath  # type: ignore
        from GSASII import GSASIIscriptable as G2sc  # type: ignore

        return GSASIIpath, G2sc
    except ModuleNotFoundError as exc:
        search_root = resolve_gsas_search_root(gsas2_path)
        if search_root is None:
            raise GSASRuntimeError(
                "GSAS-II could not be imported. Install GSAS-II into the active "
                "Python environment or set GSAS2_PATH to the GSAS-II checkout root."
            ) from exc
        if str(search_root) not in sys.path:
            sys.path.insert(0, str(search_root))
        from GSASII import GSASIIpath  # type: ignore
        from GSASII import GSASIIscriptable as G2sc  # type: ignore

        return GSASIIpath, G2sc


def configure_gsas(gsas2_path: str | None = None):
    GSASIIpath, G2sc = import_gsas_modules(gsas2_path)
    search_root = resolve_gsas_search_root(gsas2_path)
    if search_root is not None:
        bin_dir = search_root / "GSASII-bin"
        if bin_dir.exists():
            try:
                GSASIIpath.SetBinaryPath(str(bin_dir))
            except Exception:
                pass
    return G2sc


def new_project(G2sc: Any, gpx_path: str):
    try:
        return G2sc.G2Project(newgpx=gpx_path)
    except TypeError:
        try:
            return G2sc.G2Project(gpx_path, new=True)
        except TypeError:
            Path(gpx_path).touch()
            return G2sc.G2Project(gpx_path)


def write_temp_xye(theta: np.ndarray, intensity: np.ndarray, sigma: np.ndarray) -> str:
    handle = tempfile.NamedTemporaryFile("w", suffix=".xye", delete=False)
    try:
        for angle, y_obs, sig in zip(theta, intensity, sigma):
            handle.write(f"{angle:.6f} {y_obs:.6f} {sig:.6f}\n")
        return handle.name
    finally:
        handle.close()


def write_default_instprm(label: str = "CuKa lab data", gsas2_path: str | None = None) -> str:
    import_gsas_modules(gsas2_path)
    from GSASII import defaultIparms as dIP  # type: ignore

    try:
        index = dIP.defaultIparm_lbl.index(label)
    except ValueError as exc:
        available = ", ".join(dIP.defaultIparm_lbl)
        raise GSASRuntimeError(
            f"Unknown GSAS-II default instrument label '{label}'. Available labels: {available}"
        ) from exc

    handle = tempfile.NamedTemporaryFile("w", suffix=".instprm", delete=False)
    try:
        handle.writelines(dIP.defaultIparms[index])
        return handle.name
    finally:
        handle.close()


def resolve_instrument_parameter_file(
    instrument_parameter_path: str | None = None,
    instrument_label: str = "CuKa lab data",
    gsas2_path: str | None = None,
    *,
    missing_message: str | None = None,
) -> tuple[str, bool]:
    if instrument_parameter_path:
        return str(Path(instrument_parameter_path).expanduser().resolve()), False

    env_instprm = os.getenv("GSAS2_INSTPRM_PATH")
    if env_instprm:
        return str(Path(env_instprm).expanduser().resolve()), False

    try:
        return write_default_instprm(label=instrument_label, gsas2_path=gsas2_path), True
    except Exception as exc:
        raise GSASRuntimeError(
            missing_message
            or "No GSAS-II instrument parameter file is configured. Set "
            "GSAS2_INSTPRM_PATH or provide instrument_parameter_path explicitly."
        ) from exc


def prepare_project_path(project_path: str | None = None) -> tuple[str, bool]:
    if project_path:
        return str(Path(project_path).expanduser().resolve()), False

    handle = tempfile.NamedTemporaryFile(suffix=".gpx", delete=False)
    try:
        return handle.name, True
    finally:
        handle.close()


def read_project_bytes(project_path: str) -> bytes:
    try:
        return Path(project_path).read_bytes()
    except OSError:
        return b""


def cleanup_paths(*path_specs: tuple[str | None, bool]) -> None:
    for path_str, should_remove in path_specs:
        if should_remove and path_str:
            try:
                os.remove(path_str)
            except OSError:
                pass


def set_project_cycles(project: Any, cycles: int) -> None:
    if hasattr(project, "set_Controls"):
        try:
            project.set_Controls("cycles", max(1, int(cycles)))
        except Exception:
            pass


def clear_sample_scale_refinement(histogram: Any) -> None:
    try:
        histogram.clear_refinements({"Sample Parameters": ["Scale"]})
    except Exception:
        pass


def read_powder_pattern(
    datafile_path: str,
    fmthint: str | None = None,
    gsas2_path: str | None = None,
):
    """Read a powder diffraction pattern from a (possibly binary) instrument
    file via GSAS-II's importers and return a pandas DataFrame with ``Angle``
    and ``Intensity`` columns.

    Used for vendor formats the text parsers cannot read (e.g. Bruker ``.raw``).
    GSAS-II selects an importer by matching ``fmthint`` against reader format
    names; for Bruker RAW the format name is "Bruker RAW", so we try ``"RAW"``
    and fall back to ``"Bruker"``. Raises :class:`GSASRuntimeError` when GSAS-II
    is unavailable or no importer can read the file, so callers can degrade
    gracefully.
    """
    import pandas as pd

    G2sc = configure_gsas(gsas2_path)

    project_path, remove_project = prepare_project_path()
    instprm_path, remove_instprm = resolve_instrument_parameter_file(gsas2_path=gsas2_path)
    try:
        project = new_project(G2sc, project_path)
        hints = [fmthint] if fmthint else ["RAW", "Bruker"]
        histogram = None
        last_error: Exception | None = None
        for hint in hints:
            try:
                histogram = project.add_powder_histogram(
                    str(datafile_path), iparams=instprm_path, fmthint=hint
                )
            except Exception as exc:  # importer mismatch / parse failure
                last_error = exc
                histogram = None
            if histogram is not None:
                break

        if histogram is None:
            raise GSASRuntimeError(
                f"GSAS-II could not import the powder pattern '{datafile_path}'."
            ) from last_error

        x = np.asarray(histogram.getdata("X"), dtype=float)
        y = np.asarray(histogram.getdata("Yobs"), dtype=float)
        if x.size == 0 or y.size == 0:
            raise GSASRuntimeError(
                f"GSAS-II returned an empty pattern for '{datafile_path}'."
            )
        return pd.DataFrame({"Angle": x, "Intensity": y})
    finally:
        cleanup_paths((project_path, remove_project), (instprm_path, remove_instprm))
