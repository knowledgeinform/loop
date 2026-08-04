"""Build downloadable ``.zip`` archives of a composition, recipe, or trial.
Archives mirror the catalog hierarchy; visibility is enforced per node."""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import BinaryIO, Optional

from . import xrd_store
from .auid import is_recipe_id, split_recipe_id
from .documents import (
    Material,
    find_embedded_trial,
    get_recipe,
    get_recipes_for_material,
)
from .permissions import is_visible_to_user, visible_recipe_children
from .raw_db import RawFile
from .xrd_store import _utc_iso


def _sanitize(text: str) -> str:
    """Filesystem-safe token: colons (from AUIDs) become hyphens."""
    return (text or "").replace(":", "-")


def _embedded_to_dict(doc) -> dict:
    """Serialize a MongoEngine (embedded) document to a plain JSON-able dict."""
    raw = doc.to_mongo().to_dict()
    raw.pop("_cls", None)
    return raw


def _trial_file_hash(trial):
    exp = getattr(trial, "exp_condition", None)
    if exp is None:
        return None
    return (getattr(exp, "additional_params", None) or {}).get("file_hash")


def _original_names(trials) -> dict:
    """Map file_hash -> original upload filename, fetched in one query."""
    hashes = [h for h in (_trial_file_hash(t) for t in trials) if h]
    if not hashes:
        return {}
    return {
        row.id: row.original_filename
        for row in RawFile.objects(id__in=hashes).only("original_filename")
        if row.original_filename
    }


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, indent=2, default=str).encode("utf-8")


def _add_trial_to_zip(zf, prefix, recipe_auid, trial, notes, original_names=None) -> None:
    """Write ``<prefix>trial.json`` and, when present, the trial's raw/ folder."""
    zf.writestr(f"{prefix}trial.json", _json_bytes(_embedded_to_dict(trial)))

    raw_path = xrd_store.resolve_raw_path(recipe_auid, trial.trial_id)
    if not raw_path:
        return

    file_hash = _trial_file_hash(trial)

    # Original upload, named as it was uploaded when we know the name.
    original_name = _sanitize(Path(raw_path).name)
    uploaded_name = (original_names or {}).get(file_hash)
    if uploaded_name:
        original_name = _sanitize(Path(uploaded_name).name)
    try:
        zf.writestr(f"{prefix}raw/{original_name}", Path(raw_path).read_bytes())
    except OSError as exc:
        notes.append(f"trial {trial.trial_id}: could not read raw file ({exc})")
        return

    # Derived artifacts — cache-only; downloads never run the build pipeline.
    entry = xrd_store.get_cached(
        recipe_auid, trial.trial_id, file_hash, source_path=raw_path
    )
    if entry is None:
        notes.append(f"trial {trial.trial_id}: derived artifacts not yet built")
        return
    pattern_path = xrd_store.trial_path(recipe_auid, trial.trial_id) / "pattern.csv"
    if pattern_path.is_file():
        zf.writestr(f"{prefix}raw/pattern.csv", pattern_path.read_bytes())
    zf.writestr(f"{prefix}raw/overlay.png", entry.overlay_png_bytes)
    zf.writestr(f"{prefix}raw/peaks.json", _json_bytes(entry.peaks))


def build_trial_zip(recipe_id, trial_id, user_affiliations) -> Optional[tuple[BinaryIO, str]]:
    """Zip a single trial directory. ``None`` if missing or not visible."""
    recipe = get_recipe(recipe_id)
    if recipe is None:
        return None
    trial = find_embedded_trial(recipe, trial_id)
    if trial is None:
        return None
    if not is_visible_to_user(getattr(trial, "visibility_affiliations", None), user_affiliations):
        return None

    notes: list = []
    buffer = tempfile.TemporaryFile()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        _add_trial_to_zip(zf, "", recipe.id, trial, notes, _original_names([trial]))
        zf.writestr("manifest.json", _json_bytes({
            "level": "trial",
            "recipe_auid": recipe.id,
            "material_auid": recipe.material_auid,
            "trial_id": trial_id,
            "generated_at": _utc_iso(),
            "notes": notes,
            "schema": "LOOP directory export v1",
        }))

    buffer.seek(0)
    filename = f"trial_{_sanitize(recipe.id)}-{_sanitize(trial_id)}.zip"
    return buffer, filename


def _recipe_folder_name(recipe_auid: str) -> str:
    """``M:...:R:7f8e`` -> ``R-7f8e``; fall back to the whole sanitized AUID."""
    if not is_recipe_id(recipe_auid):
        return _sanitize(recipe_auid)
    return "R-" + _sanitize(split_recipe_id(recipe_auid)[1])


def _add_recipe_subtree(zf, prefix, recipe, user_affiliations, notes) -> bool:
    """Write a recipe's json + visible trials under ``prefix``. Returns whether
    anything visible was written."""
    visible_trials, visible_lits, recipe_visible = visible_recipe_children(
        recipe, user_affiliations
    )
    if not (recipe_visible or visible_trials or visible_lits):
        return False

    recipe_doc = _embedded_to_dict(recipe)
    recipe_doc["literature"] = [_embedded_to_dict(lit) for lit in visible_lits]
    recipe_doc["trials"] = [t.trial_id for t in visible_trials]  # bodies live in trials/
    zf.writestr(f"{prefix}recipe.json", _json_bytes(recipe_doc))

    original_names = _original_names(visible_trials)
    for trial in visible_trials:
        _add_trial_to_zip(
            zf, f"{prefix}trials/{_sanitize(trial.trial_id)}/",
            recipe.id, trial, notes, original_names,
        )
    return True


def build_recipe_zip(recipe_id, user_affiliations) -> Optional[tuple[BinaryIO, str]]:
    """Zip a recipe directory. ``None`` if missing or nothing visible."""
    recipe = get_recipe(recipe_id)
    if recipe is None:
        return None

    notes: list = []
    buffer = tempfile.TemporaryFile()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        wrote = _add_recipe_subtree(zf, "", recipe, user_affiliations, notes)
        if wrote:
            zf.writestr("manifest.json", _json_bytes({
                "level": "recipe",
                "recipe_auid": recipe.id,
                "material_auid": recipe.material_auid,
                "generated_at": _utc_iso(),
                "notes": notes,
                "schema": "LOOP directory export v1",
            }))

    if not wrote:
        buffer.close()
        return None
    buffer.seek(0)
    return buffer, f"recipe_{_sanitize(recipe.id)}.zip"


def build_composition_zip(material_auid, user_affiliations) -> Optional[tuple[BinaryIO, str]]:
    """Zip a whole composition directory. ``None`` if missing or nothing visible."""
    material = Material.objects(id=material_auid).first()
    if material is None:
        return None

    material_visible = is_visible_to_user(
        material.default_visibility_affiliations, user_affiliations
    )

    notes: list = []
    buffer = tempfile.TemporaryFile()
    wrote_any = False
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for recipe in get_recipes_for_material(material_auid):
            folder = f"recipes/{_recipe_folder_name(recipe.id)}/"
            if _add_recipe_subtree(zf, folder, recipe, user_affiliations, notes):
                wrote_any = True

        if wrote_any or material_visible:
            material_doc = _embedded_to_dict(material)
            material_doc["dft_calculations"] = [
                _embedded_to_dict(d) for d in (material.dft_calculations or [])
                if is_visible_to_user(getattr(d, "visibility_affiliations", None), user_affiliations)
            ]
            zf.writestr("material.json", _json_bytes(material_doc))
            zf.writestr("manifest.json", _json_bytes({
                "level": "composition",
                "material_auid": material.id,
                "generated_at": _utc_iso(),
                "notes": notes,
                "schema": "LOOP directory export v1",
            }))

    if not (wrote_any or material_visible):
        buffer.close()
        return None
    buffer.seek(0)
    return buffer, f"composition_{_sanitize(material.id)}.zip"
