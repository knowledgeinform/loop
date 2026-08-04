"""Unified per-trial XRD file store: one folder per trial under
``MEDIA_ROOT/xrd/<material>/<recipe>/<trial_id>/`` holding the raw upload plus
lazily-built derived artifacts. Reads fall back to the older layouts."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from django.conf import settings

import logging

from catalog.auid import is_recipe_id, split_recipe_id
from catalog.utils import parse_xrd_file, render_xrd_plot
from catalog.gsas_tools import peak_finder, peak_finder_fast
from catalog.raw_db import RawFile, record_derived_file
from catalog.upload_archive import add_files

logger = logging.getLogger(__name__)

UNIFIED_SUBDIR = "xrd"
LEGACY_SUBDIR = "xrd_data"
_LEGACY_EXTENSIONS = (".csv", ".txt", ".asc", ".xy", ".raw")


def _recipe_segments(recipe_auid: str) -> tuple[str, ...]:
    """On-disk path segments for a recipe AUID: material / recipe; other values are one segment."""
    if is_recipe_id(recipe_auid):
        material, short = split_recipe_id(recipe_auid)
        return (material, f"R:{short}")
    return (recipe_auid,)


def trial_path(recipe_auid: str, trial_id: str) -> Path:
    """Absolute per-trial folder under MEDIA_ROOT (no side effects)."""
    return Path(settings.MEDIA_ROOT).joinpath(
        UNIFIED_SUBDIR, *_recipe_segments(recipe_auid), trial_id
    )


def _trial_rel(recipe_auid: str, trial_id: str) -> str:
    """Media-relative POSIX path to the per-trial folder."""
    return "/".join((UNIFIED_SUBDIR, *_recipe_segments(recipe_auid), trial_id))


def trial_dir(recipe_auid: str, trial_id: str) -> Path:
    """Resolve (and create) the per-trial folder under MEDIA_ROOT."""
    path = trial_path(recipe_auid, trial_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def analysis_path(recipe_auid: str, trial_id: str, analysis_id: str) -> Path:
    """Absolute folder for one persisted analysis under the trial XRD tree."""
    return trial_path(recipe_auid, trial_id) / "analyses" / analysis_id


def analysis_dir(recipe_auid: str, trial_id: str, analysis_id: str) -> Path:
    """Resolve (and create) the per-analysis folder under the trial XRD tree."""
    path = analysis_path(recipe_auid, trial_id, analysis_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _media_url(rel_path: str) -> str:
    base = settings.MEDIA_URL
    if not base.endswith("/"):
        base += "/"
    return base + rel_path.lstrip("/")


@dataclass
class StoredRaw:
    raw_path: str
    ext: str
    sha256: str
    media_url: str


@dataclass
class CacheEntry:
    peaks: list
    overlay_png_bytes: bytes
    overlay_url: str
    pattern_url: str
    from_cache: bool
    plot_style: Optional[str]
    variant: str


def store_raw_file(recipe_auid: str, trial_id: str, uploaded_file) -> StoredRaw:
    """Write the original upload as ``raw.<ext>`` in the per-trial folder."""
    ext = os.path.splitext(getattr(uploaded_file, "name", "") or "")[1].lower() or ".csv"
    folder = trial_dir(recipe_auid, trial_id)
    raw_path = folder / f"raw{ext}"
    # Drop any previous raw.<otherext> so resolve_raw_path can't pick a stale file.
    for stale in folder.glob("raw.*"):
        if stale != raw_path:
            stale.unlink(missing_ok=True)
    hasher = hashlib.sha256()
    with open(raw_path, "wb+") as destination:
        for chunk in uploaded_file.chunks():
            destination.write(chunk)
            hasher.update(chunk)
    try:
        uploaded_file.seek(0)
    except Exception:
        pass
    rel = f"{_trial_rel(recipe_auid, trial_id)}/raw{ext}"
    return StoredRaw(str(raw_path), ext, hasher.hexdigest(), _media_url(rel))


def resolve_raw_path(recipe_auid: str, trial_id: str) -> Optional[str]:
    """Return the raw file path, preferring the unified folder, else older layouts."""
    material = _recipe_segments(recipe_auid)[0]
    unified = trial_path(recipe_auid, trial_id)
    # Pre-recipe-nesting unified layout: xrd/<material>/<trial_id>/raw.*
    flat = Path(settings.MEDIA_ROOT) / UNIFIED_SUBDIR / material / trial_id
    for folder in (unified, flat):
        if folder.is_dir():
            for candidate in sorted(folder.glob("raw.*")):
                return str(candidate)
    # Legacy single-file layout was keyed by the material AUID only.
    legacy_dir = Path(settings.MEDIA_ROOT) / LEGACY_SUBDIR / material
    for ext in _LEGACY_EXTENSIONS:
        candidate = legacy_dir / f"{trial_id}{ext}"
        if candidate.is_file():
            return str(candidate)
    return None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _hash_file(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _rel_under_media(path: Path) -> str:
    return str(path.relative_to(Path(settings.MEDIA_ROOT))).replace(os.sep, "/")


def write_analysis_summary(recipe_auid: str, trial_id: str, analysis_id: str, payload) -> None:
    """Persist a compact per-trial analysis summary without creating a new store."""
    folder = trial_dir(recipe_auid, trial_id)
    analyses_dir = folder / "analyses"
    analyses_dir.mkdir(parents=True, exist_ok=True)

    summary_index_path = analyses_dir / "index.json"
    summary_index = _read_json(summary_index_path) or {}
    summary_index[str(analysis_id)] = payload
    _write_json(summary_index_path, summary_index)

    trial_index_path = folder / "index.json"
    trial_index = _read_json(trial_index_path) or {}
    analyses = dict(trial_index.get("analyses") or {})
    analyses[str(analysis_id)] = payload
    trial_index["analyses"] = analyses
    _write_json(trial_index_path, trial_index)


def get_cached(
    recipe_auid: str,
    trial_id: str,
    file_hash: Optional[str] = None,
    variant: str = "fast",
    *,
    source_path: Optional[str] = None,
) -> Optional[CacheEntry]:
    """Return the cached entry for a trial without building, or None.

    Staleness keys on ``file_hash`` (or a hash of the raw file when falsy).
    Reflection cards are always served as the ``"stick"`` variant.
    """
    folder = trial_path(recipe_auid, trial_id)
    index = _read_json(folder / "index.json") or {}
    if not index:
        return None

    key = file_hash
    if not key:
        src = source_path or resolve_raw_path(recipe_auid, trial_id)
        key = _hash_file(src) if src else None
    if key and index.get("file_hash") != key:
        return None

    effective_variant = "stick" if index.get("plot_style") == "stick" else variant
    overlay_path = folder / f"{effective_variant}.png"
    peaks_path = folder / f"{effective_variant}.peaks.json"
    pattern_path = folder / "pattern.csv"
    if (
        effective_variant not in (index.get("variants") or {})
        or not overlay_path.is_file()
        or not peaks_path.is_file()
    ):
        return None
    return CacheEntry(
        peaks=_read_json(peaks_path) or [],
        overlay_png_bytes=overlay_path.read_bytes(),
        overlay_url=_media_url(_rel_under_media(overlay_path)),
        pattern_url=_media_url(_rel_under_media(pattern_path)),
        from_cache=True,
        plot_style=index.get("plot_style"),
        variant=effective_variant,
    )


def get_or_build(
    recipe_auid: str,
    trial_id: str,
    file_hash: Optional[str],
    variant: str = "fast",
    *,
    source_path: Optional[str] = None,
) -> CacheEntry:
    """Return cached processed artifacts for a trial, building them on a miss.

    ``variant`` is ``"fast"`` (scipy) or ``"gsas"`` (full refinement).
    """
    folder = trial_dir(recipe_auid, trial_id)
    index_path = folder / "index.json"
    index = _read_json(index_path) or {}

    src = source_path or resolve_raw_path(recipe_auid, trial_id)
    if not src:
        raise FileNotFoundError(f"No raw XRD file for {recipe_auid}/{trial_id}")

    key = file_hash or _hash_file(src)

    cached = get_cached(recipe_auid, trial_id, key, variant, source_path=src)
    if cached is not None:
        return cached

    pattern_path = folder / "pattern.csv"

    # --- build ---
    _, df = parse_xrd_file(src, src)
    plot_style = df.attrs.get("plot_style")
    df[["Angle", "Intensity"]].to_csv(pattern_path, index=False)

    if plot_style == "stick":
        built_variant = "stick"
        peaks: list = []
        overlay_uri = render_xrd_plot(df, encode_base64=True)
    elif variant == "gsas":
        built_variant = "gsas"
        peaks, _, overlay_uri = peak_finder(df, use_gsas=True)
    else:
        built_variant = "fast"
        peaks, _, overlay_uri = peak_finder_fast(df)

    overlay_path = folder / f"{built_variant}.png"
    peaks_path = folder / f"{built_variant}.peaks.json"
    png_bytes = base64.b64decode(overlay_uri.split(",", 1)[1])
    overlay_path.write_bytes(png_bytes)
    _write_json(peaks_path, peaks)

    if index.get("file_hash") != key:
        index["variants"] = {}
    index["file_hash"] = key
    index["generated_at"] = _utc_iso()
    index["n_points"] = int(len(df))
    index["plot_style"] = plot_style
    index.setdefault("variants", {})[built_variant] = {"n_peaks": len(peaks)}
    _write_json(index_path, index)

    _register_and_archive(
        file_hash=file_hash,
        recipe_auid=recipe_auid,
        trial_id=trial_id,
        built_variant=built_variant,
        pattern_path=pattern_path,
        overlay_path=overlay_path,
        peaks_path=peaks_path,
    )

    return CacheEntry(
        peaks=peaks,
        overlay_png_bytes=png_bytes,
        overlay_url=_media_url(_rel_under_media(overlay_path)),
        pattern_url=_media_url(_rel_under_media(pattern_path)),
        from_cache=False,
        plot_style=plot_style,
        variant=built_variant,
    )


def read_manifest(recipe_auid: str, trial_id: str, file_hash: Optional[str]) -> dict:
    """Build (if needed) and return a JSON-serialisable manifest for a trial.

    Includes the raw URL, the pattern URL, per-variant overlay/peaks info, and
    the default-variant peaks inline.
    """
    entry = get_or_build(recipe_auid, trial_id, file_hash)
    folder = trial_dir(recipe_auid, trial_id)
    index = _read_json(folder / "index.json") or {}

    raw_path = resolve_raw_path(recipe_auid, trial_id)
    raw_url = _media_url(_rel_under_media(Path(raw_path))) if raw_path and Path(raw_path).is_relative_to(Path(settings.MEDIA_ROOT)) else None

    variants = {}
    for name, info in (index.get("variants") or {}).items():
        variants[name] = {
            "overlay_url": _media_url(f"{_trial_rel(recipe_auid, trial_id)}/{name}.png"),
            "peaks_url": _media_url(f"{_trial_rel(recipe_auid, trial_id)}/{name}.peaks.json"),
            "n_peaks": info.get("n_peaks", 0),
        }

    return {
        "trial_id": trial_id,
        "file_hash": index.get("file_hash"),
        "generated_at": index.get("generated_at"),
        "n_points": index.get("n_points"),
        "raw_url": raw_url,
        "pattern_url": entry.pattern_url,
        "variants": variants,
        "peaks": entry.peaks,
    }


def _register_and_archive(
    *,
    file_hash: Optional[str],
    recipe_auid: str,
    trial_id: str,
    built_variant: str,
    pattern_path: Path,
    overlay_path: Path,
    peaks_path: Path,
) -> None:
    """Index derived artifacts in loop_raw and back-fill the archive snapshot.

    Non-fatal: any failure is logged and swallowed so the page/upload succeeds.
    """
    if not file_hash:
        return

    artifacts = [
        ("pattern", None, pattern_path),
        ("overlay", built_variant, overlay_path),
        ("peaks", built_variant, peaks_path),
    ]
    try:
        for kind, variant, path in artifacts:
            rel = _rel_under_media(path)
            record_derived_file(
                file_hash=file_hash,
                kind=kind,
                variant=variant,
                stored_path=rel,
                url=_media_url(rel),
                size_bytes=path.stat().st_size,
                sha256=_hash_file(str(path)),
                generated_at=_utc_iso(),
            )
    except Exception:
        logger.warning("xrd_store: failed to register derived files for %s", file_hash, exc_info=True)

    try:
        row = RawFile.objects(id=file_hash).first()
        archive_folder = getattr(row, "archive_folder", None) if row else None
        if archive_folder:
            add_files(archive_folder, [str(p) for _, _, p in artifacts])
    except Exception:
        logger.warning("xrd_store: failed to back-fill archive for %s", file_hash, exc_info=True)
