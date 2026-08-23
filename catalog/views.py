"""
Catalog views for the nested Mongo topology.

The catalog exposes two collections to the UI:

* ``materials`` — one document per (composition, structure_family), with
  embedded DFT calculations.
* ``recipes`` — one document per synthesis methodology inside a material,
  with embedded trials and literature.

All cross-record URLs use AUIDs (see :mod:`catalog.auid`). The composition
page is rebuilt on read by :func:`catalog.aggregation.composition_view`; all
writes mutate the enclosing material/recipe document in place.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.views.decorators.cache import never_cache
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.http import FileResponse, Http404, HttpResponseRedirect, JsonResponse
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.http import require_POST
from django.views.defaults import page_not_found as django_page_not_found
from django.views.defaults import server_error as django_server_error
from django.views.generic import CreateView, TemplateView
from django.core.files.storage import default_storage

from . import aggregation as aggregation_mod
from . import api_download
from . import auid as auid_mod
from .permissions import (
    AFFILIATION_CANONICAL,
    canonical_affiliation as _canonical_affiliation,
    is_visible_to_user as _is_visible_to_user,
    normalize_visibility_tags as _normalize_visibility_tags,
    recipe_or_children_visible,
    visible_recipe_children,
)
from .auid import lit_auid as _lit_auid
from . import signals
from . import vector_search as vector_search_mod
from .documents import (
    AFFILIATION_VALUES,
    DOIMapping,
    EmbeddedDFT,
    EmbeddedLiterature,
    EmbeddedTrial,
    ExpCondition,
    Material,
    MLEmbedding,
    Recipe,
    UserPrecursor,
    UserProtocol,
    VISIBILITY_DEFAULT,
    XRDAnalysisJob,
    XRDAnalysisReview,
    XRD_ANALYSIS_PHASE_STATE_VALUES,
    XRD_ANALYSIS_REVIEW_STATUS_VALUES,
    _normalize_structure_family,
    compute_material_auid,
    compute_recipe_auid,
    find_embedded_dft,
    find_embedded_literature,
    find_embedded_trial,
    get_material,
    get_recipe,
    get_recipes_for_material,
    get_user_affiliations,
    normalize_elements_payload,
)
from .forms import LiteratureDataForm, SignupForm
from .forms import BatchExperimentalUploadForm, BatchLiteratureUploadForm
from .gsas_tools import peak_finder, peak_finder_fast
from .raw_db import record_raw_file
from .prediction_table import format_composition, screen_3d_transition_metal_oxides
from .upload_archive import archive_upload
from .utils import parse_xrd_file, render_xrd_plot, xrd_parse
from .services.batch_experiment_upload import (
    commit_batch,
    create_preview,
    generate_manifest_template_csv,
    persist_uploaded_file,
)
from .services import batch_literature_upload
from catalog import xrd_store
from catalog.xrd_analysis import (
    XRDAnalysisJobError,
    allowed_analysis_artifact_names,
    assemble_repository_xrd_input,
    load_persisted_xrd_analysis,
    to_jsonable,
    validate_persisted_xrd_analysis,
)


# =============================================================================
# Generic helpers
# =============================================================================

STRUCTURE_FAMILY_SPACEGROUPS = {
    "rocksalt":   ["unknown", "Fm-3m (#225)"],
    "spinel":     ["unknown", "Fd-3m (#227)"],
    "pyrochlore": ["unknown", "Fd-3m (#227)"],
    "perovskite": ["unknown", "Pm-3m (#221)", "R-3c (#167)", "Pnma (#62)", "P4/mmm (#123)"],
    "fluorite":   ["unknown", "Fm-3m (#225)"],
    "other":      ["unknown"],
    "unknown":    ["unknown"],
}

STRUCTURE_FAMILY_SITES = {
    "rocksalt":   ["unknown", "A-site (cation)", "X-site (anion)"],
    "spinel":     ["unknown", "A-site (tet, 8a)", "B-site (oct, 16d)", "X-site (anion, 32e)"],
    "pyrochlore": ["unknown", "A-site (16d)", "B-site (16c)", "X-site (48f)", "X'-site (8b)"],
    "perovskite": ["unknown", "A-site", "B-site", "X-site (anion)"],
    "fluorite":   ["unknown", "A-site (cation, 4a)", "X-site (anion, 8c)"],
    "other":      ["unknown", "A-site", "B-site", "C-site", "X-site"],
    "unknown":    ["unknown", "A-site", "B-site", "X-site (anion)"],
}


def _safe_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _safe_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _group_extended_data(record) -> list:
    """
    Organize an EmbeddedDFT record's extended_data dict into labelled display groups.
    Returns a list of (group_name, [(key, value), ...]) tuples, skipping empty groups.
    """
    raw = dict(record.extended_data or {})

    groups = [
        ("Electronic", ["Egap_DOS", "Egap_DOS_net", "Egap_DOS_type", "spinD", "spinF",
                        "eentropy_atom", "eentropy_cell", "valence_cell_iupac",
                        "valence_cell_std", "VEC", "scintillation_attenuation_length"]),
        ("Elastic", ["ael_compliance_tensor", "ael_stiffness_tensor",
                     "ael_bulk_modulus_reuss", "ael_bulk_modulus_voigt",
                     "ael_shear_modulus_reuss", "ael_shear_modulus_voigt",
                     "ael_speed_sound_average", "ael_speed_sound_longitudinal",
                     "ael_speed_sound_transverse", "ael_pughs_modulus_ratio",
                     "ael_applied_pressure", "ael_average_external_pressure"]),
        ("Thermal", ["agl_acoustic_debye", "agl_bulk_modulus_isothermal_300K",
                     "agl_bulk_modulus_static_300K", "agl_poisson_ratio_source",
                     "agl_vibrational_entropy_300K_atom", "agl_vibrational_entropy_300K_cell",
                     "agl_vibrational_free_energy_300K_atom", "agl_vibrational_free_energy_300K_cell",
                     "agl_heat_capacity_Cp_300K", "agl_heat_capacity_Cv_300K",
                     "heat_capacity_Cv_atom_qha_300K", "heat_capacity_Cp_atom_qha_300K",
                     "heat_capacity_Cv_cell_qha_300K", "heat_capacity_Cp_cell_qha_300K",
                     "entropy_vibrational_atom_apl_300K", "entropy_vibrational_cell_apl_300K",
                     "entropic_temperature", "entropy_forming_ability"]),
        ("Structural", ["Bravais_lattice_orig", "Bravais_lattice_relax",
                        "Bravais_superlattice_orig", "Bravais_superlattice_relax",
                        "point_group_Hermann_Mauguin", "point_group_Schoenflies",
                        "Wyckoff_letters", "Wyckoff_multiplicities", "Wyckoff_site_symmetries",
                        "spacegroup_orig", "geometry", "geometry_orig",
                        "positions_cartesian", "positions_fractional",
                        "aflow_prototype_label_orig", "aflow_prototype_label_relax",
                        "aflow_prototype_params_list_orig", "aflow_prototype_params_list_relax",
                        "aflow_prototype_params_values_orig",
                        "aflow_prototype_params_values_values_relax"]),
        ("Thermodynamic", ["enthalpy_atom", "enthalpy_cell", "enthalpy_formation_cell",
                           "enthalpy_formation_cce_0K_cell", "enthalpy_formation_cce_0K_atom",
                           "enthalpy_formation_cce_300K_cell", "enthalpy_formation_cce_300K_atom",
                           "energy_atom", "energy_cell", "PV_atom", "PV_cell",
                           "volume_cell", "volume_atom", "reciprocal_volume_cell",
                           "density", "S_config_atom", "S_config_partial_atom",
                           "Hmix_miedema", "ground_state", "Pulay_stress",
                           "pressure", "pressure_final", "pressure_residual"]),
        ("Computational Details", ["code", "dft_type", "energy_cutoff",
                                   "kpoints", "kpoints_relax", "kpoints_static",
                                   "kpoints_bands_nkpts", "kpoints_bands_path",
                                   "ldau_type", "ldau_u", "ldau_j", "ldau_l", "ldau_TLUJ",
                                   "species_pp", "species_pp_AUID", "species_pp_ZVAL",
                                   "species_pp_version", "metagga", "loop",
                                   "calculation_cores", "calculation_memory", "calculation_time",
                                   "node_CPU_Model", "node_CPU_MHz", "node_CPU_Cores", "node_RAM_GB",
                                   "aflow_version", "aflowlib_version", "aflowlib_date",
                                   "data_api", "data_source", "catalog", "files", "forces",
                                   "bader_atomic_volumes", "bader_net_charges",
                                   "spin_cell", "spinD", "spinF"]),
    ]

    result = []
    seen_keys = set()
    for group_name, keys in groups:
        items = []
        for k in keys:
            if k in raw and raw[k] is not None and raw[k] != "":
                items.append((k, raw[k]))
                seen_keys.add(k)
        if items:
            result.append((group_name, items))

    # Anything not yet categorized goes into "Other"
    other_items = [
        (k, v) for k, v in raw.items()
        if k not in seen_keys and k != "auid" and v is not None and v != ""
    ]
    if other_items:
        result.append(("Other", other_items))

    return result


def _compute_uploaded_file_sha256(uploaded_file):
    hasher = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        hasher.update(chunk)
    uploaded_file.seek(0)
    return hasher.hexdigest()


def _parse_elements_payload(raw_value):
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if isinstance(payload, dict):
        if all(isinstance(k, str) for k in payload.keys()):
            return payload
        payload = payload.get("elements", [])
    elements = {}
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                symbol = item.get("symbol") or item.get("element")
                ratio = item.get("ratio") or item.get("value")
            elif isinstance(item, (list, tuple)) and item:
                symbol = item[0]
                ratio = item[1] if len(item) > 1 else None
            else:
                continue
            if not symbol:
                continue
            parsed = _safe_float(ratio)
            elements[str(symbol)] = parsed if parsed is not None else ratio
    return elements


def _parse_composition_query(raw_value):
    if not raw_value:
        return []
    try:
        payload = json.loads(raw_value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(payload, dict):
        payload = payload.get("elements", [])
    summary = []
    for item in payload:
        symbol = None
        ratio = None
        if isinstance(item, dict):
            symbol = item.get("symbol") or item.get("element")
            ratio = item.get("ratio") or item.get("value")
        elif isinstance(item, (list, tuple)):
            if item:
                symbol = item[0]
            if len(item) > 1:
                ratio = item[1]
        else:
            symbol = str(item)
        if not symbol:
            continue
        try:
            cleaned_ratio = None
            if ratio is not None and ratio != "":
                ratio_num = float(ratio)
                cleaned_ratio = int(ratio_num) if ratio_num.is_integer() else ratio_num
        except (TypeError, ValueError):
            cleaned_ratio = None
        summary.append({"symbol": symbol, "ratio": cleaned_ratio})
    return summary


_RATIO_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)\s*$")


def _normalize_ratio(value):
    """Normalize a user-entered ratio string to canonical ``x:y`` form.

    Empty values round-trip as empty (the field is optional). Any non-empty
    value that does not match ``<number>:<number>`` raises ``ValueError`` so
    the view can surface a friendly message; otherwise the returned string
    is whitespace-stripped and rewritten with a single colon separator.
    """
    if value is None:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    match = _RATIO_RE.match(raw)
    if not match:
        raise ValueError(
            f"Ratio must be in x:y form (e.g. 10:1); got {raw!r}."
        )
    return f"{match.group(1)}:{match.group(2)}"


def _parse_element_sites(request) -> dict:
    raw = request.POST.get("element_sites", "{}")
    try:
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.items() if k and v} if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _parse_synthesis_steps_from_request(request):
    step_count = _safe_int(request.POST.get("step_count")) or 1
    synthesis_steps = []
    for i in range(1, step_count + 1):
        step_type = request.POST.get(f"step_{i}_type", "other")
        step_data = {
            "step_number": i,
            "step_type": step_type,
            "notes": request.POST.get(f"step_{i}_notes", ""),
        }
        if step_type == "ball_milling":
            bpr_raw = request.POST.get(f"step_{i}_bpr", "")
            try:
                bpr_normalized = _normalize_ratio(bpr_raw)
            except ValueError as exc:
                raise ValueError(f"Step {i} ball:powder ratio: {exc}") from exc
            step_data.update({
                "milling_time_hours": _safe_float(request.POST.get(f"step_{i}_milling_time")),
                "milling_rpm": _safe_float(request.POST.get(f"step_{i}_milling_rpm")),
                "ball_powder_ratio": bpr_normalized,
                "atmosphere": request.POST.get(f"step_{i}_atmosphere", ""),
                "jar_material": request.POST.get(f"step_{i}_jar_material", ""),
                "ball_material": request.POST.get(f"step_{i}_ball_material", ""),
                "process_control_agent": request.POST.get(f"step_{i}_pca", ""),
            })
        elif step_type == "weighing":
            step_data.update({
                "total_mass_g": _safe_float(request.POST.get(f"step_{i}_total_mass")),
                "precursors": request.POST.get(f"step_{i}_precursors", ""),
            })
            precursors_json_raw = request.POST.get(f"step_{i}_precursors_json") or ""
            if precursors_json_raw:
                try:
                    parsed = json.loads(precursors_json_raw)
                except (TypeError, ValueError):
                    parsed = []
                if isinstance(parsed, list):
                    cleaned_list = []
                    for item in parsed:
                        if not isinstance(item, dict):
                            continue
                        row = {
                            k: str(item.get(k) or "").strip()
                            for k in ("cas_number", "name", "formula", "purity", "supplier", "notes")
                            if item.get(k)
                        }
                        if row:
                            cleaned_list.append(row)
                    if cleaned_list:
                        step_data["precursors_list"] = cleaned_list
        elif step_type == "mixing":
            step_data.update({
                "mixing_time_min": _safe_float(request.POST.get(f"step_{i}_mixing_time")),
                "mixing_method": request.POST.get(f"step_{i}_mixing_method", ""),
            })
        elif step_type == "pelletizing":
            step_data.update({
                "pressure_mpa": _safe_float(request.POST.get(f"step_{i}_press_pressure")),
                "hold_time_min": _safe_float(request.POST.get(f"step_{i}_press_time")),
                "die_diameter_mm": _safe_float(request.POST.get(f"step_{i}_die_diameter")),
                "lubricant": request.POST.get(f"step_{i}_lubricant", ""),
            })
        elif step_type == "heat_treatment":
            step_data.update({
                "max_temp_c": _safe_float(request.POST.get(f"step_{i}_max_temp")),
                "ramp_rate_c_min": _safe_float(request.POST.get(f"step_{i}_ramp_rate")),
                "hold_time_hours": _safe_float(request.POST.get(f"step_{i}_hold_time")),
                "atmosphere": request.POST.get(f"step_{i}_heat_atmosphere", ""),
                "furnace_type": request.POST.get(f"step_{i}_furnace_type", ""),
                "o2_partial_pressure_bar": _safe_float(request.POST.get(f"step_{i}_o2_pressure")),
            })
        elif step_type == "annealing":
            step_data.update({
                "temperature_c": _safe_float(request.POST.get(f"step_{i}_anneal_temp")),
                "duration_hours": _safe_float(request.POST.get(f"step_{i}_anneal_time")),
                "atmosphere": request.POST.get(f"step_{i}_anneal_atmosphere", ""),
            })
        elif step_type == "arc_melting":
            step_data.update({
                "current_a": _safe_float(request.POST.get(f"step_{i}_arc_current")),
                "number_of_remelts": _safe_int(request.POST.get(f"step_{i}_remelts")),
                "hearth_material": request.POST.get(f"step_{i}_hearth", ""),
                "atmosphere": request.POST.get(f"step_{i}_arc_atmosphere", ""),
            })
        elif step_type == "quenching":
            step_data.update({
                "quenching_medium": request.POST.get(f"step_{i}_quench_medium", ""),
                "medium_temperature_c": _safe_float(request.POST.get(f"step_{i}_quench_temp")),
            })
        elif step_type == "cooling":
            step_data.update({
                "cooling_method": request.POST.get(f"step_{i}_cool_method", ""),
                "cooling_rate_c_min": _safe_float(request.POST.get(f"step_{i}_cool_rate")),
            })
        elif step_type == "grinding":
            step_data.update({
                "grinding_method": request.POST.get(f"step_{i}_grind_method", ""),
                "final_particle_size": request.POST.get(f"step_{i}_particle_size", ""),
            })
        elif step_type == "xrd_measurement":
            step_data.update({
                "radiation": request.POST.get(f"step_{i}_xrd_radiation", ""),
                "two_theta_range": request.POST.get(f"step_{i}_xrd_range", ""),
                "step_size_deg": _safe_float(request.POST.get(f"step_{i}_xrd_step")),
                "scan_speed_deg_min": _safe_float(request.POST.get(f"step_{i}_xrd_speed")),
            })
        elif step_type == "other":
            step_data.update({"description": request.POST.get(f"step_{i}_other_desc", "")})
        step_data = {k: v for k, v in step_data.items() if v is not None and v != ""}
        synthesis_steps.append(step_data)
    return synthesis_steps


def _elements_to_selection_list(elements_dict):
    if not elements_dict:
        return []
    positive_values = [float(v) for v in elements_dict.values() if _safe_float(v) and float(v) > 0]
    if not positive_values:
        return []
    min_ratio = min(positive_values)
    selections = []
    for el, raw in elements_dict.items():
        val = _safe_float(raw)
        if val is None or val <= 0:
            continue
        normalized = val / min_ratio
        nearest_int = round(normalized)
        if abs(normalized - nearest_int) < 0.02:
            ratio = int(nearest_int)
        else:
            ratio = round(normalized, 2)
        selections.append([el, ratio])
    return selections


def _display_elements(elements_dict):
    """Return element->display ratio map for detail page rendering."""
    if not elements_dict:
        return {}
    positive_values = [float(v) for v in elements_dict.values() if _safe_float(v) and float(v) > 0]
    if not positive_values:
        return dict(elements_dict)
    min_ratio = min(positive_values)
    display = {}
    for el, raw in elements_dict.items():
        val = _safe_float(raw)
        if val is None or val <= 0:
            display[el] = raw
            continue
        normalized = val / min_ratio
        nearest_int = round(normalized)
        if abs(normalized - nearest_int) < 0.02:
            display[el] = int(nearest_int)
        else:
            display[el] = round(normalized, 2)
    return display


def _nominal_composition_html(display_elements):
    """HTML fragment El<sub>x</sub>… for the composition hero (oxides: O last)."""
    if not display_elements:
        return mark_safe("")
    keys = list(display_elements.keys())
    if "O" in keys:
        ordered = sorted(k for k in keys if k != "O")
        ordered.append("O")
    else:
        ordered = sorted(keys)
    parts = []
    for el in ordered:
        amt = display_elements.get(el)
        if amt is None:
            continue
        parts.append(format_html("{}<sub>{}</sub>", el, amt))
    return mark_safe("".join(str(p) for p in parts))


def _extract_trial_temperatures(trial):
    """Extract synthesis temperatures (°C) from a trial document or dict."""
    temps = []
    exp_condition = _get(trial, "exp_condition", None) or {}
    temp_profile = _get(exp_condition, "temp_profile", None) or []
    for point in temp_profile:
        if isinstance(point, dict):
            val = _safe_float(point.get("max_temp_c"))
            if val is not None:
                temps.append(val)
    additional = _get(exp_condition, "additional_params", None) or {}
    for step in additional.get("synthesis_steps", []) or []:
        if not isinstance(step, dict):
            continue
        for key in ("temperature_c", "max_temp_c"):
            val = _safe_float(step.get(key))
            if val is not None:
                temps.append(val)
    return temps


def _format_temperature_display(temps):
    if not temps:
        return "—"
    low = min(temps)
    high = max(temps)
    if abs(low - high) < 1e-6:
        return f"{low:.0f}" if float(low).is_integer() else f"{low:.1f}"
    low_str = f"{low:.0f}" if float(low).is_integer() else f"{low:.1f}"
    high_str = f"{high:.0f}" if float(high).is_integer() else f"{high:.1f}"
    return f"{low_str}–{high_str}"


def _get(obj, name, default=None):
    """Attribute-or-dict accessor for documents and aggregation dicts alike."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


# =============================================================================
# Visibility + affiliation helpers
# =============================================================================

def _user_affiliations(user):
    return _normalize_visibility_tags(get_user_affiliations(user) or list(VISIBILITY_DEFAULT))


def _get_visibility_affiliations_for_create(user):
    return _normalize_visibility_tags(_user_affiliations(user) or list(VISIBILITY_DEFAULT))


def _is_uploader_or_superuser(user, uploader):
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(uploader) and str(user.username).strip().lower() == str(uploader).strip().lower()


def _user_is_approved(user):
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(settings, "APPROVED_BYPASS_SUPERUSERS", True) and (
        getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
    ):
        return True
    group_name = getattr(settings, "APPROVED_GROUP_NAME", "Approved")
    return user.groups.filter(name=group_name).exists()


def _user_can_submit_xrd_analysis(user):
    return _user_is_approved(user)


def _user_can_review_xrd_analysis(user):
    return _user_is_approved(user)


# =============================================================================
# File-hash dedup across embedded trials
# =============================================================================

def _find_duplicate_experiment_file_hash(file_hash):
    """Find a (material_auid, recipe_auid, trial_id) tuple that already has this file.

    Returns ``(material_auid, recipe_auid, trial_id)`` or ``(None, None, None)``.
    """
    if not file_hash:
        return None, None, None
    hit = Recipe.objects(__raw__={"trials.file_hash": file_hash}).only(
        "material_auid", "trials"
    ).first()
    if hit is None:
        return None, None, None
    for trial in hit.trials or []:
        if trial.file_hash == file_hash:
            return hit.material_auid, hit.id, trial.trial_id
    return hit.material_auid, hit.id, None


# =============================================================================
# Material + recipe upsert helpers
# =============================================================================

def _upsert_material(
    *,
    material_auid,
    elements,
    structure_family,
    default_visibility_affiliations=None,
):
    """Ensure a Material document exists for ``material_auid`` with these fields."""
    element_symbols = sorted(str(k) for k in (elements or {}).keys())
    existing = Material.objects(id=material_auid).first()
    if existing is None:
        material = Material(
            id=material_auid,
            elements=elements,
            element_symbols=element_symbols,
            num_elements=len(element_symbols),
            structure_family=structure_family,
            default_visibility_affiliations=list(default_visibility_affiliations or []),
        )
        material.save()
        return material
    # Backfill missing invariants if a doc was created before fields existed.
    dirty = False
    if not existing.elements:
        existing.elements = elements
        dirty = True
    if not existing.element_symbols:
        existing.element_symbols = element_symbols
        dirty = True
    if not existing.num_elements:
        existing.num_elements = len(element_symbols)
        dirty = True
    if not existing.structure_family:
        existing.structure_family = structure_family
        dirty = True
    if dirty:
        existing.save()
    return existing


def _upsert_recipe(
    *,
    recipe_auid,
    material_auid,
    elements,
    structure_family,
    synthesis_steps,
    visibility_affiliations=None,
):
    """Ensure a Recipe document exists for ``recipe_auid`` with these fields."""
    element_symbols = sorted(str(k) for k in (elements or {}).keys())
    existing = Recipe.objects(id=recipe_auid).first()
    if existing is None:
        recipe = Recipe(
            id=recipe_auid,
            material_auid=material_auid,
            elements=elements,
            element_symbols=element_symbols,
            num_elements=len(element_symbols),
            structure_family=structure_family,
            synthesis_steps=list(synthesis_steps or []),
            visibility_affiliations=list(visibility_affiliations or []),
        )
        recipe.save()
        return recipe
    dirty = False
    if not existing.material_auid:
        existing.material_auid = material_auid
        dirty = True
    if not existing.elements:
        existing.elements = elements
        dirty = True
    if not existing.element_symbols:
        existing.element_symbols = element_symbols
        dirty = True
    if not existing.num_elements:
        existing.num_elements = len(element_symbols)
        dirty = True
    if not existing.structure_family:
        existing.structure_family = structure_family
        dirty = True
    if not existing.synthesis_steps and synthesis_steps:
        existing.synthesis_steps = list(synthesis_steps)
        dirty = True
    if dirty:
        existing.save()
    return existing


def _next_trial_id(recipe_auid):
    """Return the next sequential trial_id (``"1"``, ``"2"``, …) within a recipe.

    Trials are numbered per recipe: each recipe's trials count up from 1
    independently. The id is the smallest positive integer not already taken by
    a trial in this recipe, so gaps left by deletions are reused.
    """
    taken = set()
    recipe = Recipe.objects(id=recipe_auid).only("trials").first()
    if recipe is not None:
        for trial in recipe.trials or []:
            taken.add(trial.trial_id or "")
    n = 1
    while str(n) in taken:
        n += 1
    return str(n)


# =============================================================================
# Persistence cores (shared by the manual add forms and batch uploads)
# =============================================================================

class DuplicateFileError(Exception):
    """Raised when an uploaded raw-data file hash already exists in the catalog."""

    def __init__(self, message, *, material_auid=None, recipe_auid=None, trial_id=None):
        super().__init__(message)
        self.material_auid = material_auid
        self.recipe_auid = recipe_auid
        self.trial_id = trial_id


class DuplicateRecordError(Exception):
    """Raised when an identical record has already been uploaded to the catalog."""

    def __init__(self, message, *, material_auid=None, recipe_auid=None):
        super().__init__(message)
        self.material_auid = material_auid
        self.recipe_auid = recipe_auid


def _trial_content_hash(
    *, material_auid, recipe_auid, phase_status, spacegroup, element_sites, raw_data_type, notes, file_hash
):
    """Deterministic hash of a trial's stored payload (excludes ids/dates).

    This is a provenance fingerprint, not a uniqueness key for deduping trial
    submissions. Repeated experiments can legitimately share the same recipe
    AUID and other metadata, so duplicate rejection should key off the raw file
    hash instead.
    """
    payload = {
        "material_auid": material_auid,
        "recipe_auid": recipe_auid,
        "phase_status": phase_status or "",
        "spacegroup": spacegroup or "",
        "element_sites": {k: (element_sites or {})[k] for k in sorted(element_sites or {})},
        "raw_data_type": raw_data_type or "",
        "notes": notes or "",
        "file_hash": file_hash or "",
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _rel_media_path(abs_path: str) -> str:
    return os.path.relpath(abs_path, settings.MEDIA_ROOT).replace(os.sep, "/")


def _versioned_media_url(url, file_hash):
    """Append a cache-busting version so a re-upload invalidates cached plots."""
    if url and file_hash:
        return f"{url}?v={file_hash[:12]}"
    return url


def persist_experimental_trial(
    *,
    user,
    request,
    raw_elements,
    structure_family,
    synthesis_steps,
    phase_status,
    spacegroup,
    element_sites,
    raw_data_type,
    notes,
    csv_file=None,
    edit_recipe=None,
    edit_trial_record=None,
    edit_recipe_id=None,
    edit_trial_id=None,
    reject_duplicates=False,
    source_batch_id=None,
):
    """Create or update an ``EmbeddedTrial``; shared by the form view and batch upload.

    ``request`` is used only to build the absolute raw-data URL. Returns
    ``{"material_auid", "recipe_auid", "trial_id", "plot_image", "warnings"}`` and
    raises :class:`DuplicateFileError` if the CSV hash already exists elsewhere.
    """
    warnings = []
    plot_image = None
    csv_path = None

    has_csv = bool(csv_file and getattr(csv_file, "name", ""))
    csv_sha256 = None
    if has_csv:
        csv_sha256 = _compute_uploaded_file_sha256(csv_file)
        dup_material, dup_recipe, dup_trial_id = _find_duplicate_experiment_file_hash(csv_sha256)
        same_trial_reupload = (
            edit_trial_record is not None
            and dup_recipe == edit_recipe_id
            and dup_trial_id == edit_trial_id
        )
        if dup_material is not None and dup_trial_id is not None and not same_trial_reupload:
            raise DuplicateFileError(
                (
                    "Duplicate raw data file detected by hash. "
                    f"This file already exists in {dup_material} "
                    f"(trial {dup_trial_id})."
                ),
                material_auid=dup_material,
                recipe_auid=dup_recipe,
                trial_id=dup_trial_id,
            )

    material_auid = compute_material_auid(raw_elements, structure_family)
    recipe_auid = compute_recipe_auid(material_auid, synthesis_steps)

    if edit_trial_record is not None:
        trial_id = edit_trial_record.trial_id
        trial_date = edit_trial_record.trial_date or timezone.now()
    else:
        trial_date = timezone.now()
        trial_id = _next_trial_id(recipe_auid)

    additional_params = {"synthesis_steps": synthesis_steps}
    if source_batch_id:
        additional_params["source_batch_id"] = source_batch_id
    raw_data_link = None

    if has_csv:
        additional_params["file_hash"] = csv_sha256
        stored = xrd_store.store_raw_file(recipe_auid, trial_id, csv_file)
        csv_path = stored.raw_path
        raw_data_link = request.build_absolute_uri(stored.media_url)
        csv_file.seek(0)
        try:
            # Binary formats (.raw) parse from the written path; text uses the upload.
            xrd_source = csv_path if stored.ext in (".raw",) else csv_file
            metadata, df = parse_xrd_file(xrd_source, csv_file.name)
            plot_image = render_xrd_plot(df, encode_base64=True)
            additional_params["xrd_metadata"] = metadata
        except Exception as exc:
            warnings.append(f"Could not parse XRD file for plotting: {exc}")
    elif edit_trial_record is not None:
        prev_condition = getattr(edit_trial_record, "exp_condition", None)
        prev_additional = getattr(prev_condition, "additional_params", {}) or {}
        if prev_additional.get("file_hash"):
            additional_params["file_hash"] = prev_additional["file_hash"]
        for key in ("xrd_metadata", "diffraction_metadata"):
            if prev_additional.get(key):
                additional_params[key] = prev_additional[key]
        raw_data_link = getattr(edit_trial_record, "raw_data_link", None) or None

    exp_condition = ExpCondition(additional_params=additional_params)
    visibility = _get_visibility_affiliations_for_create(user)

    _upsert_material(
        material_auid=material_auid,
        elements=raw_elements,
        structure_family=structure_family,
        default_visibility_affiliations=visibility,
    )

    target_recipe = _upsert_recipe(
        recipe_auid=recipe_auid,
        material_auid=material_auid,
        elements=raw_elements,
        structure_family=structure_family,
        synthesis_steps=synthesis_steps,
        visibility_affiliations=visibility,
    )

    # If editing and the recipe has changed, remove the old embedded trial.
    if edit_recipe is not None and edit_recipe.id != target_recipe.id:
        edit_recipe.trials = [
            t for t in (edit_recipe.trials or []) if t.trial_id != edit_trial_id
        ]
        edit_recipe.save()

    phase_status_raw = (
        phase_status
        if phase_status in ("single_phase", "multi_phase", "not_confirmed")
        else "not_confirmed"
    )
    success_value = {
        "single_phase": True,
        "multi_phase": False,
        "not_confirmed": None,
    }[phase_status_raw]

    content_hash = _trial_content_hash(
        material_auid=material_auid,
        recipe_auid=recipe_auid,
        phase_status=phase_status_raw,
        spacegroup=spacegroup,
        element_sites=element_sites,
        raw_data_type=raw_data_type,
        notes=notes,
        file_hash=additional_params.get("file_hash"),
    )

    if (
        reject_duplicates
        and edit_trial_record is None
        and any(getattr(t, "content_hash", None) == content_hash for t in (target_recipe.trials or []))
    ):
        raise DuplicateRecordError(
            "An identical trial already exists in this recipe.",
            material_auid=material_auid,
            recipe_auid=recipe_auid,
        )

    new_trial = EmbeddedTrial(
        trial_id=trial_id,
        trial_date=trial_date,
        phase_status=phase_status_raw,
        success=success_value,
        exp_condition=exp_condition,
        raw_data_type=raw_data_type,
        file_hash=additional_params.get("file_hash"),
        content_hash=content_hash,
        experimenter=user.username if user.is_authenticated else "",
        notes=notes,
        spacegroup=spacegroup,
        element_sites=element_sites,
        visibility_affiliations=visibility,
    )
    if raw_data_link:
        new_trial.raw_data_link = raw_data_link

    # Upsert the trial by trial_id inside the target recipe.
    target_recipe.trials = [
        t for t in (target_recipe.trials or []) if t.trial_id != trial_id
    ] + [new_trial]
    target_recipe.save()

    archive_folder = archive_upload(
        upload_type="trial",
        username=user.username if user.is_authenticated else "anonymous",
        timestamp=timezone.localtime(trial_date),
        metadata={
            "type": "trial",
            "trial_id": trial_id,
            "material_auid": material_auid,
            "recipe_auid": recipe_auid,
            "experimenter": user.username if user.is_authenticated else "",
            "trial_date": trial_date.isoformat(),
            "phase_status": phase_status_raw,
            "raw_data_type": raw_data_type,
            "elements": raw_elements,
            "structure_family": structure_family,
            "synthesis_steps": synthesis_steps,
            "notes": notes,
            "file_hash": additional_params.get("file_hash"),
            "has_csv": has_csv,
            "source_batch_id": source_batch_id,
        },
        media_src_path=csv_path if has_csv else None,
        media_dest_filename=os.path.basename(csv_path) if has_csv else None,
    )

    # Idempotent Raw DB entry for the uploaded file.
    if additional_params.get("file_hash"):
        stored_path = _rel_media_path(csv_path) if has_csv else None
        record_raw_file(
            file_hash=additional_params["file_hash"],
            material_auid=material_auid,
            recipe_auid=recipe_auid,
            trial_id=trial_id,
            original_filename=getattr(csv_file, "name", None) if has_csv else None,
            stored_path=stored_path,
            content_type=getattr(csv_file, "content_type", None) if has_csv else None,
            size_bytes=getattr(csv_file, "size", None) if has_csv else None,
            uploaded_by=user.username if user.is_authenticated else None,
            elements=raw_elements,
            structure_family=structure_family,
            archive_folder=archive_folder,
        )

    return {
        "material_auid": material_auid,
        "recipe_auid": recipe_auid,
        "trial_id": trial_id,
        "plot_image": plot_image,
        "warnings": warnings,
    }


def persist_literature_entry(
    *,
    user,
    raw_elements,
    structure_family,
    synthesis_steps,
    doi,
    synthesis_successful,
    title,
    authors,
    journal,
    year,
    findings,
    spacegroup,
    element_sites,
    existing_recipe=None,
    existing_literature=None,
    edit_lit_id=None,
    edit_doi=None,
    legacy=None,
    reject_duplicates=False,
):
    """Create or update an ``EmbeddedLiterature``; shared by the form view and batch upload.

    ``legacy`` carries the form-only legacy milling fields (``milling_time``,
    ``milling_rpm``, ``atmosphere``, ``cooling_method``); batch uploads pass none.
    Returns ``{"material_auid", "recipe_auid", "lit_id"}``.
    """
    legacy = legacy or {}
    additional_params = {"synthesis_steps": synthesis_steps} if synthesis_steps else {}

    legacy_milling_time = _safe_float(legacy.get("milling_time"))
    legacy_milling_rpm = _safe_float(legacy.get("milling_rpm"))
    legacy_atmosphere = (legacy.get("atmosphere") or "").strip()
    raw_cooling = (legacy.get("cooling_method") or "").strip()
    allowed_cooling = {"air_quench", "furnace_cool", "quench", "slow_cool", "other"}
    legacy_cooling = raw_cooling if raw_cooling in allowed_cooling else None

    exp_condition = None
    if synthesis_steps or any(
        v not in (None, "") for v in (legacy_milling_time, legacy_milling_rpm, legacy_atmosphere, legacy_cooling)
    ):
        kwargs = {"additional_params": additional_params}
        if legacy_milling_time is not None:
            kwargs["milling_time_hours"] = legacy_milling_time
        if legacy_milling_rpm is not None:
            kwargs["milling_rpm"] = legacy_milling_rpm
        if legacy_atmosphere:
            kwargs["atmosphere"] = legacy_atmosphere
        if legacy_cooling:
            kwargs["cooling_method"] = legacy_cooling
        exp_condition = ExpCondition(**kwargs)

    material_auid = compute_material_auid(raw_elements, structure_family)
    recipe_auid = compute_recipe_auid(material_auid, synthesis_steps or [])

    # Reject a paper that was already uploaded for this material (batch path only;
    # the manual form upserts by DOI so re-saving is an intentional edit).
    if reject_duplicates and existing_literature is None:
        dup = Recipe.objects(
            __raw__={"material_auid": material_auid, "literature.lit_id": _lit_auid(doi)}
        ).only("id").first()
        if dup is not None:
            raise DuplicateRecordError(
                f"This DOI ({doi}) has already been uploaded for {material_auid}.",
                material_auid=material_auid,
                recipe_auid=recipe_auid,
            )

    visibility = _get_visibility_affiliations_for_create(user)

    _upsert_material(
        material_auid=material_auid,
        elements=raw_elements,
        structure_family=structure_family,
        default_visibility_affiliations=visibility,
    )

    target_recipe = _upsert_recipe(
        recipe_auid=recipe_auid,
        material_auid=material_auid,
        elements=raw_elements,
        structure_family=structure_family,
        synthesis_steps=synthesis_steps,
        visibility_affiliations=visibility,
    )

    new_lit_id = _lit_auid(doi)
    new_lit = EmbeddedLiterature(
        lit_id=new_lit_id,
        doi=doi,
        title=title,
        authors=authors,
        journal=journal,
        year=year,
        synthesis_successful=synthesis_successful,
        exp_condition=exp_condition,
        spacegroup=spacegroup,
        element_sites=element_sites,
        notes=findings,
        extracted_by=user.username if user.is_authenticated else "anonymous",
        visibility_affiliations=visibility,
    )

    # If editing and the recipe has changed, drop the old lit from the old recipe.
    if (
        existing_recipe is not None
        and existing_literature is not None
        and existing_recipe.id != target_recipe.id
    ):
        old_lit_id = getattr(existing_literature, "lit_id", None) or edit_lit_id
        old_doi_norm = (edit_doi or "").strip().lower()
        existing_recipe.literature = [
            lit for lit in (existing_recipe.literature or [])
            if getattr(lit, "lit_id", None) != old_lit_id
            and (lit.doi or "").strip().lower() != old_doi_norm
        ]
        existing_recipe.save()

    doi_norm = doi.strip().lower()
    target_recipe.literature = [
        lit for lit in (target_recipe.literature or [])
        if getattr(lit, "lit_id", None) != new_lit_id
        and (lit.doi or "").strip().lower() != doi_norm
    ] + [new_lit]
    target_recipe.save()

    if doi:
        DOIMapping.objects(doi=doi).update_one(
            set_on_insert__title=title,
            add_to_set__material_auids=material_auid,
            upsert=True,
        )

    archive_upload(
        upload_type="literature",
        username=user.username if user.is_authenticated else "anonymous",
        timestamp=timezone.localtime(timezone.now()),
        metadata={
            "type": "literature",
            "doi": doi,
            "material_auid": material_auid,
            "recipe_auid": recipe_auid,
            "extracted_by": user.username if user.is_authenticated else "anonymous",
            "title": title,
            "authors": authors,
            "journal": journal,
            "year": year,
            "synthesis_successful": synthesis_successful,
            "notes": findings,
            "elements": raw_elements,
            "structure_family": structure_family,
            "synthesis_steps": synthesis_steps,
        },
    )

    return {
        "material_auid": material_auid,
        "recipe_auid": recipe_auid,
        "lit_id": new_lit_id,
    }


# =============================================================================
# Top-level + auth views
# =============================================================================

def index(request):
    num_visits = request.session.get("num_visits", 0) + 1
    request.session["num_visits"] = num_visits
    # Full-database totals for everyone (Browse still respects org visibility).
    context = {"num_visits": num_visits, **aggregation_mod.catalog_landing_page_totals()}
    return render(request, "index.html", context=context)


def add_data(request):
    return render(request, "catalog/add_data.html")


def predict(request):
    context = screen_3d_transition_metal_oxides(
        user_affiliations=_user_affiliations(request.user),
    )
    return render(request, 'catalog/predict.html', context)


@login_required
def account(request):
    selected_affiliations = _user_affiliations(request.user)
    return render(
        request,
        "catalog/account.html",
        {"selected_affiliations": selected_affiliations},
    )


class SignUpView(CreateView):
    form_class = SignupForm
    template_name = "registration/signup.html"

    def get_success_url(self):
        return reverse("signup_pending")

    def form_valid(self, form):
        self.object = form.save(commit=True)

        uidb64 = urlsafe_base64_encode(force_bytes(self.object.pk))
        token = default_token_generator.make_token(self.object)
        activate_url = self.request.build_absolute_uri(
            reverse("activate", args=[uidb64, token])
        )

        user_email = form.cleaned_data.get("email")
        subject = "Verify your email for Loop"

        text_body = (
            f"Hi {self.object.username},\n\n"
            f"Please verify your email by clicking the link below:\n{activate_url}\n\n"
            f"After verifying, email the site admin at loop@mintaka.arch.jhu.edu to request access.\n"
            f"You won't see protected pages until your account is approved."
        )
        html_body = (
            f"<p>Hi {self.object.username},</p>"
            f"<p>Please verify your email by clicking the link below:<br>"
            f'<a href="{activate_url}">{activate_url}</a></p>'
            f"<p>After verifying, email the site admin at "
            f'<a href="mailto:loop@mintaka.arch.jhu.edu">loop@mintaka.arch.jhu.edu</a> to request access.<br>'
            f"You won't see protected pages until your account is approved.</p>"
        )

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user_email],
            reply_to=["loop@mintaka.arch.jhu.edu"],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=True)

        admin_link = self.request.build_absolute_uri(
            reverse("admin:auth_user_change", args=[self.object.pk])
        )
        admin_body = f"user {self.object.username} signed up: {admin_link} with address {user_email}"
        admin_msg = EmailMessage(
            subject="New signup",
            body=admin_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=["loop@mintaka.arch.jhu.edu"],
            reply_to=[user_email],
        )
        admin_msg.send(fail_silently=False)
        return HttpResponseRedirect(self.get_success_url())


class SignupPendingView(TemplateView):
    template_name = "registration/signup_pending.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            if request.user.groups.filter(name=settings.APPROVED_GROUP_NAME).exists():
                return redirect("index")
            return redirect("awaiting_approval")
        return super().dispatch(request, *args, **kwargs)


class SignupCompleteView(TemplateView):
    template_name = "registration/signup_complete.html"

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            if request.user.groups.filter(name=settings.APPROVED_GROUP_NAME).exists():
                return redirect("index")
            return redirect("awaiting_approval")
        return super().dispatch(request, *args, **kwargs)


def activate(request, uidb64, token):
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None

    if user and default_token_generator.check_token(user, token):
        from django.contrib.auth.models import Group
        email_group, _ = Group.objects.get_or_create(name="email_confirmed")
        user.groups.add(email_group)
        return redirect("signup_complete")
    return render(request, "registration/activation_invalid.html", status=400)


# =============================================================================
# Browse
# =============================================================================

_recipe_visible_for_browse = recipe_or_children_visible


def _format_composition_compact(display_elements: Optional[Dict[str, Any]]) -> str:
    if not display_elements:
        return ""
    parts = []
    for el, ratio in display_elements.items():
        if ratio is not None and ratio != "":
            parts.append(f"{el}:{ratio}")
        else:
            parts.append(str(el))
    return " ".join(parts)


def _format_atomic_fractions(elements: Optional[Dict[str, Any]]) -> str:
    """Atomic fractions, e.g. ``Fe 0.43 - O 0.57`` for Fe3O4.

    The row label already carries the stoichiometry, so repeating the raw counts
    underneath it says the same thing twice. Fractions answer a different
    question: what share of the atoms each element is. That is the number that
    matters for a high-entropy oxide, where equimolar cations are the point and a
    formula string hides how far off equimolar a sample actually sits.

    Returns "" when every element has the same fraction. An equimolar row would
    read 0.20 five times, which is the redundancy this replaced.
    """
    values: Dict[str, float] = {}
    for symbol, raw in (elements or {}).items():
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if number > 0:
            values[str(symbol)] = number
    if len(values) < 2:
        return ""
    total = sum(values.values())
    if total <= 0:
        return ""
    fractions = {symbol: value / total for symbol, value in values.items()}
    if max(fractions.values()) - min(fractions.values()) < 1e-9:
        return ""
    ordered = sorted(symbol for symbol in fractions if symbol != "O")
    if "O" in fractions:
        ordered.append("O")
    return " \u00b7 ".join(f"{symbol} {fractions[symbol]:.2f}" for symbol in ordered)


def _format_steps_preview(steps, max_len: int = 96) -> str:
    if not steps:
        return "—"
    try:
        text = json.dumps(steps, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(steps)
    if len(text) > max_len:
        return text[: max_len - 1] + "…"
    return text


def _extract_temperatures_from_exp_dict(exp_condition) -> List[float]:
    """Temperatures (°C) from a plain dict exp_condition (trial or literature)."""
    if not exp_condition or not isinstance(exp_condition, dict):
        return []
    temps: List[float] = []
    temp_profile = exp_condition.get("temp_profile") or []
    for point in temp_profile:
        if isinstance(point, dict):
            val = _safe_float(point.get("max_temp_c"))
            if val is not None:
                temps.append(val)
    additional = exp_condition.get("additional_params") or {}
    for step in additional.get("synthesis_steps", []) or []:
        if not isinstance(step, dict):
            continue
        for key in ("temperature_c", "max_temp_c"):
            val = _safe_float(step.get(key))
            if val is not None:
                temps.append(val)
    return temps


def browse_data(request):
    """Browse / search: materials, flat recipes, literature rows, or DFT rows."""
    view_mode = (request.GET.get("view") or "materials").strip().lower()
    if view_mode not in ("materials", "recipes", "literature", "computational"):
        view_mode = "materials"

    structure_family = request.GET.get("structure_family")
    search_query = request.GET.get("search", "").strip()
    composition_json = request.GET.get("composition", "")
    has_literature = request.GET.get("has_literature")
    has_experiments = request.GET.get("has_experiments")
    has_computational = request.GET.get("has_computational")
    steps_q = (request.GET.get("steps_q") or "").strip().lower()
    affiliation_filters = [
        canonical
        for canonical in (
            _canonical_affiliation(a) for a in request.GET.getlist("affiliations")
        )
        if canonical is not None
    ]
    temp_filter_enabled = request.GET.get("temp_filter_enabled") == "true"
    temp_min = _safe_float(request.GET.get("temp_min"))
    temp_max = _safe_float(request.GET.get("temp_max"))

    composition_summary = _parse_composition_query(composition_json)
    elements = [item["symbol"] for item in composition_summary] if composition_summary else None

    has_lit = has_literature == "true" if has_literature else None
    has_exp = has_experiments == "true" if has_experiments else None
    has_comp = has_computational == "true" if has_computational else None

    user_affiliations = _user_affiliations(request.user)

    semantic_auids: Optional[list] = None
    semantic_score_by_auid: Dict[str, float] = {}
    search_mode = "none"
    semantic_error: Optional[str] = None
    if search_query:
        try:
            ranked = vector_search_mod.semantic_material_auids(
                search_query,
                limit=200,
                user_affiliations=user_affiliations,
            )
            semantic_auids = [auid for auid, _score in ranked]
            semantic_score_by_auid = {auid: float(score) for auid, score in ranked}
            search_mode = "semantic"
        except vector_search_mod.VectorSearchUnavailable as exc:
            semantic_auids = None
            search_mode = "auid"
            semantic_error = str(exc)

    material_auid_query = search_query if search_mode == "auid" else None
    material_auid_in = semantic_auids if search_mode == "semantic" else None

    temp_filter_active = (
        temp_filter_enabled
        and temp_min is not None
        and temp_max is not None
    )

    page = _safe_int(request.GET.get("page")) or 1
    default_per_page = 25 if view_mode == "materials" else 30
    req_per_page = _safe_int(request.GET.get("per_page"))
    per_page = req_per_page if req_per_page and req_per_page > 0 else default_per_page
    per_page = min(per_page, aggregation_mod.BROWSE_MATERIALS_MAX_LIMIT)

    filtered_rows: List[Dict[str, Any]] = []
    browse_materials_db_paginated = False
    materials_agg_total: Optional[int] = None

    if view_mode == "materials":
        db_page_ok = (
            not affiliation_filters
            and not temp_filter_active
            and material_auid_in is None
        )
        bm_kwargs = dict(
            elements=elements,
            structure_family=structure_family if structure_family else None,
            has_experiments=has_exp,
            has_literature=has_lit,
            has_computational=has_comp,
            user_affiliations=user_affiliations,
            material_auid_query=material_auid_query,
            material_auid_in=material_auid_in,
        )
        if db_page_ok:
            bm = aggregation_mod.browse_materials(
                **bm_kwargs,
                skip=(page - 1) * per_page,
                limit=per_page,
            )
            browse_materials_db_paginated = True
            materials_agg_total = bm.total_count
            rows = bm.rows
        else:
            bm = aggregation_mod.browse_materials(**bm_kwargs)
            rows = bm.rows

        recipes_by_m: Dict[str, List] = {}
        dft_orgs_by_m: Dict[str, List] = {}
        if rows:
            m_auids = [r["material_auid"] for r in rows]
            for recipe in Recipe.objects(material_auid__in=m_auids).only("material_auid", "trials", "literature"):
                recipes_by_m.setdefault(recipe.material_auid, []).append(recipe)
            for mat in Material.objects(id__in=m_auids).only("id", "dft_calculations"):
                orgs: list = []
                for dft in mat.dft_calculations or []:
                    if not _is_visible_to_user(
                        getattr(dft, "visibility_affiliations", None), user_affiliations
                    ):
                        continue
                    for tag in _normalize_visibility_tags(
                        getattr(dft, "visibility_affiliations", None)
                    ):
                        if tag not in orgs:
                            orgs.append(tag)
                if orgs:
                    dft_orgs_by_m[mat.id] = orgs

        for row in rows:
            material_auid = row["material_auid"]
            trial_orgs: list = []
            trial_temps: list = []
            trial_count_visible = 0
            for recipe in recipes_by_m.get(material_auid, []):
                for trial in recipe.trials or []:
                    if not _is_visible_to_user(
                        getattr(trial, "visibility_affiliations", None), user_affiliations
                    ):
                        continue
                    trial_count_visible += 1
                    for tag in _normalize_visibility_tags(
                        getattr(trial, "visibility_affiliations", None)
                    ):
                        if tag not in trial_orgs:
                            trial_orgs.append(tag)
                    trial_temps.extend(_extract_trial_temperatures(trial))
                for lit in recipe.literature or []:
                    if not _is_visible_to_user(
                        getattr(lit, "visibility_affiliations", None), user_affiliations
                    ):
                        continue
                    for tag in _normalize_visibility_tags(
                        getattr(lit, "visibility_affiliations", None)
                    ):
                        if tag not in trial_orgs:
                            trial_orgs.append(tag)

            if has_exp is True and trial_count_visible == 0:
                continue

            if affiliation_filters and not any(tag in trial_orgs for tag in affiliation_filters):
                continue

            if temp_filter_active:
                if not trial_temps:
                    continue
                if not any(temp_min <= t <= temp_max for t in trial_temps):
                    continue

            out_row = {
                "material_auid": material_auid,
                "elements": row.get("elements") or {},
                "element_symbols": row.get("element_symbols") or [],
                "structure_family": row.get("structure_family"),
                "num_elements": row.get("num_elements"),
                "trial_count": trial_count_visible,
                "literature_count": row.get("literature_count") or 0,
                "comp_count": row.get("computational_count") or 0,
                "organizations": trial_orgs + [t for t in dft_orgs_by_m.get(material_auid, []) if t not in trial_orgs],
                "temperature_display": _format_temperature_display(trial_temps),
                "display_elements": _display_elements(row.get("elements") or {}),
                "composition_compact": _format_composition_compact(
                    _display_elements(row.get("elements") or {})
                ),
                # Human-readable formula. This is what browse rows show; the AUID
                # is content-derived and means nothing to a reader, so it moves to
                # the link target and the hover title instead of the row itself.
                "composition_display": format_composition(row.get("elements") or {}),
                # Second line: fractions, not counts. Counts restate the label.
                "composition_fractions": _format_atomic_fractions(row.get("elements") or {}),
            }
            if search_mode == "semantic":
                out_row["semantic_score"] = semantic_score_by_auid.get(material_auid)
            filtered_rows.append(out_row)

    elif view_mode == "recipes":
        mat_rows = aggregation_mod.browse_materials(
            elements=elements,
            structure_family=structure_family if structure_family else None,
            has_experiments=has_exp,
            has_literature=has_lit,
            has_computational=has_comp,
            user_affiliations=user_affiliations,
            material_auid_query=material_auid_query,
            material_auid_in=material_auid_in,
        ).rows
        material_order = [r["material_auid"] for r in mat_rows]
        meta_by_m = {r["material_auid"]: r for r in mat_rows}
        if not material_order:
            filtered_rows = []
        else:
            recipes_by_m: Dict[str, List] = {}
            for recipe in (
                Recipe.objects(material_auid__in=material_order)
                .order_by("-created_at")
            ):
                recipes_by_m.setdefault(recipe.material_auid, []).append(recipe)

            for material_auid in material_order:
                meta = meta_by_m.get(material_auid) or {}
                for recipe in recipes_by_m.get(material_auid, []):
                    if not _recipe_visible_for_browse(recipe, user_affiliations):
                        continue
                    steps = list(recipe.synthesis_steps or [])
                    if steps_q:
                        hay = aggregation_mod.synthesis_steps_search_text(steps)
                        if steps_q not in hay:
                            continue

                    trial_orgs: list = []
                    trial_temps: list = []
                    trial_count_visible = 0
                    for trial in recipe.trials or []:
                        if not _is_visible_to_user(
                            getattr(trial, "visibility_affiliations", None), user_affiliations
                        ):
                            continue
                        trial_count_visible += 1
                        for tag in _normalize_visibility_tags(
                            getattr(trial, "visibility_affiliations", None)
                        ):
                            if tag not in trial_orgs:
                                trial_orgs.append(tag)
                        trial_temps.extend(_extract_trial_temperatures(trial))

                    if has_exp is True and trial_count_visible == 0:
                        continue

                    lit_visible = 0
                    for lit in recipe.literature or []:
                        if _is_visible_to_user(
                            getattr(lit, "visibility_affiliations", None), user_affiliations
                        ):
                            lit_visible += 1

                    if affiliation_filters and not any(tag in trial_orgs for tag in affiliation_filters):
                        continue

                    if temp_filter_active:
                        if not trial_temps:
                            continue
                        if not any(temp_min <= t <= temp_max for t in trial_temps):
                            continue

                    out_row = {
                        "recipe_auid": recipe.id,
                        "material_auid": material_auid,
                        "structure_family": meta.get("structure_family") or recipe.structure_family,
                        "element_symbols": meta.get("element_symbols") or list(recipe.element_symbols or []),
                        "num_elements": meta.get("num_elements") or recipe.num_elements,
                        "trial_count": trial_count_visible,
                        "literature_count": lit_visible,
                        "organizations": trial_orgs,
                        "temperature_display": _format_temperature_display(trial_temps),
                        "steps_preview": _format_steps_preview(steps),
                        "display_elements": _display_elements(dict(meta.get("elements") or recipe.elements or {})),
                    }
                    if search_mode == "semantic":
                        out_row["semantic_score"] = semantic_score_by_auid.get(material_auid)
                    filtered_rows.append(out_row)

    elif view_mode == "literature":
        lit_rows = aggregation_mod.browse_literature_flat(
            elements=elements,
            structure_family=structure_family if structure_family else None,
            has_experiments=has_exp,
            has_literature=has_lit,
            has_computational=has_comp,
            user_affiliations=user_affiliations,
            material_auid_query=material_auid_query,
            material_auid_in=material_auid_in,
        )
        lit_stage1: List[Dict[str, Any]] = []
        for row in lit_rows:
            if not _is_visible_to_user(row.get("lit_visibility"), user_affiliations):
                continue
            lit_tags = _normalize_visibility_tags(row.get("lit_visibility"))
            if affiliation_filters and not any(tag in lit_tags for tag in affiliation_filters):
                continue
            lit_stage1.append(row)
        recipe_ids_lit = list({r.get("recipe_auid") for r in lit_stage1 if r.get("recipe_auid")})
        recipes_by_id_lit: Dict[str, Any] = {}
        if recipe_ids_lit:
            recipes_by_id_lit = {
                r.id: r
                for r in Recipe.objects(id__in=recipe_ids_lit).only("literature", "trials", "synthesis_steps")
            }
        for row in lit_stage1:
            recipe = recipes_by_id_lit.get(row.get("recipe_auid"))
            if steps_q:
                if not recipe:
                    continue
                hay = aggregation_mod.synthesis_steps_search_text(list(recipe.synthesis_steps or []))
                if steps_q not in hay:
                    continue

            lit_temps: List[float] = []
            if recipe:
                for lit in recipe.literature or []:
                    if getattr(lit, "doi", None) == row.get("doi"):
                        ec = getattr(lit, "exp_condition", None)
                        if ec is not None:
                            ec_dict = ec.to_mongo().to_dict() if hasattr(ec, "to_mongo") else ec
                            lit_temps.extend(_extract_temperatures_from_exp_dict(ec_dict))
                        break
                if not lit_temps:
                    for trial in recipe.trials or []:
                        if not _is_visible_to_user(
                            getattr(trial, "visibility_affiliations", None), user_affiliations
                        ):
                            continue
                        lit_temps.extend(_extract_trial_temperatures(trial))

            if temp_filter_active:
                if not lit_temps:
                    continue
                if not any(temp_min <= t <= temp_max for t in lit_temps):
                    continue

            doi_val = row.get("doi") or ""
            lit_id_val = row.get("lit_id") or (_lit_auid(doi_val) if doi_val else "")
            out_row = {
                "material_auid": row.get("material_auid"),
                "recipe_auid": row.get("recipe_auid"),
                "structure_family": row.get("structure_family"),
                "element_symbols": row.get("element_symbols") or [],
                "num_elements": row.get("num_elements"),
                "lit_id": lit_id_val,
                "doi": doi_val,
                "title": row.get("title") or "",
                "journal": row.get("journal") or "",
                "year": row.get("year"),
                "temperature_display": _format_temperature_display(lit_temps),
                "organizations": _normalize_visibility_tags(row.get("lit_visibility")),
            }
            if search_mode == "semantic":
                out_row["semantic_score"] = semantic_score_by_auid.get(row.get("material_auid"))
            filtered_rows.append(out_row)

    else:  # computational
        comp_rows = aggregation_mod.browse_computational_flat(
            elements=elements,
            structure_family=structure_family if structure_family else None,
            has_experiments=has_exp,
            has_literature=has_lit,
            has_computational=has_comp,
            user_affiliations=user_affiliations,
            material_auid_query=material_auid_query,
            material_auid_in=material_auid_in,
        )
        for row in comp_rows:
            if not _is_visible_to_user(row.get("dft_visibility"), user_affiliations):
                continue
            dft_tags = _normalize_visibility_tags(row.get("dft_visibility"))
            if affiliation_filters and not any(tag in dft_tags for tag in affiliation_filters):
                continue

            out_row = {
                "material_auid": row.get("material_auid"),
                "comp_auid": row.get("comp_auid"),
                "structure_family": row.get("structure_family"),
                "element_symbols": row.get("element_symbols") or [],
                "num_elements": row.get("num_elements"),
                "dft_source": row.get("dft_source"),
                "dft_formation_energy_ev": row.get("dft_formation_energy_ev"),
                "dft_hull_distance_ev": row.get("dft_hull_distance_ev"),
                "dft_bandgap_ev": row.get("dft_bandgap_ev"),
                "organizations": _normalize_visibility_tags(row.get("dft_visibility")),
            }
            if search_mode == "semantic":
                out_row["semantic_score"] = semantic_score_by_auid.get(row.get("material_auid"))
            filtered_rows.append(out_row)

    if view_mode == "materials" and browse_materials_db_paginated and materials_agg_total is not None:
        total = materials_agg_total
        composition_rows_page = filtered_rows
    else:
        total = len(filtered_rows)
        start = (page - 1) * per_page
        end = start + per_page
        composition_rows_page = filtered_rows[start:end]

    query_params = request.GET.copy()
    query_params.pop("page", None)
    query_string = query_params.urlencode()

    context = {
        "composition_rows": composition_rows_page,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page if total else 0,
        "query_string": query_string,
        "browse_materials_db_paginated": browse_materials_db_paginated,
        "structure_families": ["unknown", "rocksalt", "pyrochlore", "spinel", "perovskite", "fluorite", "other"],
        "composition_summary": composition_summary,
        "view_mode": view_mode,
        "filters": {
            "structure_family": structure_family,
            "search": search_query,
            "has_literature": has_literature,
            "has_experiments": has_experiments,
            "has_computational": has_computational,
            "affiliations": affiliation_filters,
            "temp_filter_enabled": temp_filter_enabled,
            "steps_q": request.GET.get("steps_q") or "",
        },
        "user_affiliations": user_affiliations,
        "affiliation_choices": AFFILIATION_VALUES,
        "search_mode": search_mode,
        "semantic_error": semantic_error,
    }

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        results_html = render_to_string("catalog/_browse_results.html", context, request=request)
        return JsonResponse({
            "results_html": results_html,
            "query_string": query_string,
            "page": page,
            "search_mode": search_mode,
            "view_mode": view_mode,
        })

    return render(request, "catalog/browse_data.html", context)


# =============================================================================
# Composition (class-level) detail — joined view
# =============================================================================

def composition_detail(request, material_auid):
    """Class-level joined view for a material_auid."""
    if not auid_mod.is_material_auid(material_auid):
        return render(request, "404.html", {"message": f"Material {material_auid} not found."}, status=404)

    user_affiliations = _user_affiliations(request.user)
    view = aggregation_mod.composition_view(material_auid, user_affiliations=user_affiliations)
    if not view.get("exists"):
        return render(request, "404.html", {"message": f"Material {material_auid} not found."}, status=404)

    trials = view.get("trials") or []
    literature_list = view.get("literature") or []
    computational_list = view.get("computational") or []
    annotation = view.get("annotation") or {}
    recipes = view.get("recipes") or []

    literature_rows = []
    for lit in literature_list:
        doi_val = _get(lit, "doi")
        lit_id_val = _get(lit, "lit_id") or (_lit_auid(doi_val) if doi_val else "")
        literature_rows.append({
            "doi": doi_val,
            "lit_id": lit_id_val,
            "recipe_auid": _get(lit, "recipe_auid"),
            "literature": lit,
            "organizations": _normalize_visibility_tags(_get(lit, "visibility_affiliations")),
        })

    experiment_rows = []
    for trial in trials:
        experiment_rows.append({
            "trial_id": _get(trial, "trial_id"),
            "recipe_auid": _get(trial, "recipe_auid"),
            "exp": trial,
            "organizations": _normalize_visibility_tags(_get(trial, "visibility_affiliations")),
        })

    computational_rows = []
    for comp in computational_list:
        computational_rows.append({
            "comp_auid": _get(comp, "comp_auid"),
            "dft_source": _get(comp, "dft_source"),
            "dft_formation_energy_ev": _get(comp, "dft_formation_energy_ev"),
            "dft_hull_distance_ev": _get(comp, "dft_hull_distance_ev"),
            "dft_bandgap_ev": _get(comp, "dft_bandgap_ev"),
            "uploaded_by": _get(comp, "uploaded_by"),
            "organizations": _normalize_visibility_tags(_get(comp, "visibility_affiliations")),
            "spacegroup": _get(comp, "spacegroup"),
            "element_sites": _get(comp, "element_sites") or {},
        })

    display_elements = _display_elements(view.get("elements") or {})
    num_elements = view.get("num_elements") or 0
    recipe_groups_enriched = _enrich_recipe_groups_for_composition(view.get("recipe_groups"))

    can_modify_annotation = bool(getattr(request.user, "is_authenticated", False))
    can_delete_material = bool(getattr(request.user, "is_superuser", False))

    context = {
        "view": view,
        "material_auid": material_auid,
        # Formula shown as the page title. The AUID stays on the page, demoted to
        # a small line beneath it, because it is what a reader cites or passes to
        # the API even though it tells them nothing about the material.
        "composition_display": format_composition(view.get("elements") or {}),
        "elements": display_elements,
        "nominal_composition_html": _nominal_composition_html(display_elements),
        "structure_family": view.get("structure_family"),
        "num_elements": num_elements,
        "annotation": annotation or None,
        "visible_literature_rows": literature_rows,
        "visible_literature_count": len(literature_rows),
        "experiment_rows": experiment_rows,
        "computational_rows": computational_rows,
        "computational_records": computational_list,
        "ml_embeddings": view.get("ml_embeddings") or [],
        "recipe_groups": recipe_groups_enriched,
        "recipes": recipes,
        "can_modify_annotation": can_modify_annotation,
        "can_delete_material": can_delete_material,
        "material_created_at": view.get("material_created_at"),
    }
    return render(request, "catalog/composition_detail.html", context)


# =============================================================================
# Recipe detail
# =============================================================================

def _build_step_display(step, idx):
    """Flatten a stored synthesis step dict into the template shape.

    The ``precursors_list`` key is lifted into its own structured field so the
    template can render it as a small table rather than dumping a repr into the
    generic label/value list.
    """
    step_number = step.get("step_number") or idx
    step_type = str(step.get("step_type") or "unspecified")
    step_type_label = step_type.replace("_", " ").strip().title()
    details = []
    precursors_list = None
    for key, value in step.items():
        if key in {"step_number", "step_type"} or value in (None, "", [], {}):
            continue
        if key == "precursors_list" and isinstance(value, list):
            cleaned = [item for item in value if isinstance(item, dict) and item]
            if cleaned:
                precursors_list = cleaned
            continue
        label = str(key).replace("_", " ").strip().title()
        details.append({"label": label, "value": value})
    return {
        "step_number": step_number,
        "step_type_label": step_type_label,
        "details": details,
        "precursors_list": precursors_list,
    }


def _render_synthesis_steps(additional):
    return [
        _build_step_display(step, idx)
        for idx, step in enumerate(additional.get("synthesis_steps") or [], start=1)
        if isinstance(step, dict)
    ]


def _render_steps_list(steps):
    return [
        _build_step_display(step, idx)
        for idx, step in enumerate(steps or [], start=1)
        if isinstance(step, dict)
    ]


def _trial_row_context(trial):
    """Serialize a trial (embedded doc or aggregation dict) for the shared
    _trial_rows.html partial — the single producer of its expected keys."""
    return {
        "trial_id": _get(trial, "trial_id"),
        "trial_date": _get(trial, "trial_date"),
        "phase_status": _get(trial, "phase_status"),
        "success": _get(trial, "success"),
        "spacegroup": _get(trial, "spacegroup"),
        "element_sites": _get(trial, "element_sites") or {},
        "experimenter": _get(trial, "experimenter"),
        "raw_data_link": _get(trial, "raw_data_link"),
        "raw_data_type": _get(trial, "raw_data_type"),
        "notes": _get(trial, "notes"),
        "organizations": _normalize_visibility_tags(_get(trial, "visibility_affiliations")),
    }


def _enrich_recipe_groups_for_composition(recipe_groups):
    """Add ``steps_preview``, ``step_highlights``, and ``synthesis_step_count`` for the composition page."""
    out = []
    for group in recipe_groups or []:
        steps = group.get("synthesis_steps") or []
        step_dicts = [s for s in steps if isinstance(s, dict)]
        preview = _format_steps_preview(step_dicts, max_len=140)
        displayed = _render_steps_list(steps)
        highlights = []
        seen = set()
        for d in displayed:
            label = (d.get("step_type_label") or "").strip()
            if label and label.lower() != "unspecified" and label not in seen:
                seen.add(label)
                highlights.append(label)
            if len(highlights) >= 5:
                break
        # Normalize each trial through the shared row serializer (keeps extra
        # aggregation keys) and order newest-first within a route.
        trials = [
            {**dict(t), **_trial_row_context(t)} for t in (group.get("trials") or [])
        ]

        def _trial_sort_key(t):
            v = _get(t, "trial_date") or _get(t, "created_at")
            return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else "")

        trials.sort(key=_trial_sort_key, reverse=True)
        row = dict(group)
        row["trials"] = trials
        row["steps_preview"] = preview if preview != "—" else ""
        row["step_highlights"] = highlights
        row["synthesis_step_count"] = len(step_dicts)
        out.append(row)
    return out


def recipe_detail(request, recipe_id):
    """Render a single recipe with its embedded trials and literature."""
    if not auid_mod.is_recipe_id(recipe_id):
        return render(request, "404.html", {"message": f"Recipe {recipe_id} not found."}, status=404)

    recipe = get_recipe(recipe_id)
    if recipe is None:
        return render(request, "404.html", {"message": f"Recipe {recipe_id} not found."}, status=404)

    user_affiliations = _user_affiliations(request.user)

    trials = [
        _trial_row_context(trial)
        for trial in (recipe.trials or [])
        if _is_visible_to_user(trial.visibility_affiliations, user_affiliations)
    ]
    literature = [
        lit for lit in (recipe.literature or [])
        if _is_visible_to_user(lit.visibility_affiliations, user_affiliations)
    ]

    context = {
        "recipe": recipe,
        "recipe_id": recipe.id,
        "material_auid": recipe.material_auid,
        "elements": _display_elements(recipe.elements or {}),
        "structure_family": recipe.structure_family,
        "trials": trials,
        "literature": literature,
        "synthesis_steps_display": _render_steps_list(recipe.synthesis_steps),
    }
    return render(request, "catalog/recipe_detail.html", context)


# =============================================================================
# Embedded-record detail pages
# =============================================================================

def literature_detail(request, recipe_id, lit_id):
    if not auid_mod.is_recipe_id(recipe_id):
        raise Http404(f"Recipe {recipe_id} not found.")

    recipe = get_recipe(recipe_id)
    if recipe is None:
        return render(request, "404.html", {"message": f"Recipe {recipe_id} not found."}, status=404)

    record = find_embedded_literature(recipe, lit_id)
    if record is None:
        return render(request, "404.html", {"message": f"Literature entry {lit_id} not found."}, status=404)

    user_affiliations = _user_affiliations(request.user)
    if not _is_visible_to_user(record.visibility_affiliations, user_affiliations):
        return render(request, "404.html", {"message": f"Literature entry {lit_id} not found."}, status=404)

    exp_condition = getattr(record, "exp_condition", None)
    additional = getattr(exp_condition, "additional_params", {}) or {}
    synthesis_steps_display = _render_synthesis_steps(additional)
    if not synthesis_steps_display and recipe.synthesis_steps:
        synthesis_steps_display = _render_steps_list(recipe.synthesis_steps)

    record_lit_id = getattr(record, "lit_id", None) or lit_id

    context = {
        "material_auid": recipe.material_auid,
        "recipe_id": recipe.id,
        "record": record,
        "lit_id": record_lit_id,
        "doi": record.doi,
        "elements": _display_elements(recipe.elements or {}),
        "structure_family": recipe.structure_family,
        "synthesis_steps_display": synthesis_steps_display,
        "can_modify_literature": _is_uploader_or_superuser(request.user, getattr(record, "extracted_by", "")),
        "can_delete_literature": _is_uploader_or_superuser(request.user, getattr(record, "extracted_by", "")),
    }
    return render(request, "catalog/literature_detail.html", context)


def computational_detail(request, material_auid, comp_auid):
    material = get_material(material_auid)
    if material is None:
        return render(request, "404.html", {"message": f"Material {material_auid} not found."}, status=404)

    record = find_embedded_dft(material, comp_auid)
    if record is None:
        return render(request, "404.html", {"message": f"Computational entry {comp_auid} not found."}, status=404)

    user_affiliations = _user_affiliations(request.user)
    if not _is_visible_to_user(record.visibility_affiliations, user_affiliations):
        return render(request, "404.html", {"message": f"Computational entry {comp_auid} not found."}, status=404)

    context = {
        "material_auid": material_auid,
        "comp_auid": comp_auid,
        "record": record,
        "elements": _display_elements(material.elements or {}),
        "structure_family": material.structure_family,
        "dft_metadata_items": (record.dft_metadata or {}).items(),
        "ml_prediction_items": (record.ml_predictions or {}).items(),
        "extended_data_groups": _group_extended_data(record),
        "can_modify_computational": _is_uploader_or_superuser(request.user, getattr(record, "uploaded_by", "")),
        "can_delete_computational": _is_uploader_or_superuser(request.user, getattr(record, "uploaded_by", "")),
    }
    return render(request, "catalog/computational_detail.html", context)


def _resolve_visible_trial(request, recipe_id, trial_id):
    """Return ``(recipe, trial)`` when the trial exists and is visible to the
    requesting user, else ``(None, None)``."""
    if not auid_mod.is_recipe_id(recipe_id):
        return None, None
    recipe = get_recipe(recipe_id)
    if recipe is None:
        return None, None
    record = find_embedded_trial(recipe, trial_id)
    if record is None:
        return None, None
    if not _is_visible_to_user(record.visibility_affiliations, _user_affiliations(request.user)):
        return None, None
    return recipe, record


def _xrd_analysis_detail_path(recipe_id, trial_id, analysis_id):
    return reverse(
        "xrd_analysis_detail",
        kwargs={"recipe_id": recipe_id, "trial_id": trial_id, "analysis_id": analysis_id},
    )


def _xrd_analysis_review_path(recipe_id, trial_id, analysis_id):
    return reverse(
        "xrd_analysis_review_create",
        kwargs={"recipe_id": recipe_id, "trial_id": trial_id, "analysis_id": analysis_id},
    )


def _xrd_analysis_submit_path(recipe_id, trial_id):
    return reverse(
        "api-v1-experiment-xrd-analysis-submit",
        kwargs={"recipe_auid": recipe_id, "trial_id": trial_id},
    )


def _xrd_analysis_status_path(job_id):
    return reverse("api-v1-xrd-analysis-job-detail", kwargs={"job_id": str(job_id)})


def _xrd_analysis_result_path(analysis_id):
    return reverse("api-v1-xrd-analysis-result", kwargs={"analysis_id": str(analysis_id)})


def _xrd_analysis_artifact_path(analysis_id, artifact_name):
    return reverse(
        "api-v1-xrd-analysis-artifact",
        kwargs={"analysis_id": str(analysis_id), "artifact_name": artifact_name},
    )


def _latest_trial_xrd_analysis_job(recipe_id, trial_id):
    try:
        return XRDAnalysisJob.objects(recipe_auid=recipe_id, trial_id=trial_id).order_by("-created_at").first()
    except Exception:
        return None


def _analysis_job_for_trial(recipe_id, trial_id, analysis_id):
    try:
        return XRDAnalysisJob.objects(
            recipe_auid=recipe_id,
            trial_id=trial_id,
            analysis_id=analysis_id,
        ).first()
    except Exception:
        return None


def _load_visible_persisted_analysis(recipe, trial, analysis_id):
    try:
        context = assemble_repository_xrd_input(
            recipe.id,
            trial.trial_id,
            recipe=recipe,
            trial=trial,
            require_accessible_raw_file=False,
        )
    except XRDAnalysisJobError as exc:
        return None, {
            "code": exc.code,
            "detail": exc.message,
            "validation_state": None,
        }
    validation = validate_persisted_xrd_analysis(
        context.analysis_input,
        analysis_id=analysis_id,
    )
    if not validation.valid:
        return None, {
            "code": validation.failure_code or "analysis_result_integrity_failed",
            "detail": validation.detail or "Persisted XRD analysis artifacts failed validation.",
            "validation_state": validation,
        }
    try:
        persisted = load_persisted_xrd_analysis(
            context.analysis_input,
            analysis_id=analysis_id,
            cache_validation=validation,
        )
    except Exception as exc:
        return None, {
            "code": "analysis_result_load_failed",
            "detail": str(exc),
            "validation_state": validation,
        }
    return persisted, None


def _human_phase_status_meta(record):
    phase_status = str(getattr(record, "phase_status", "") or "").strip()
    success = getattr(record, "success", None)
    if phase_status == "single_phase":
        return {"label": "Single phase", "badge_class": "bg-success"}
    if phase_status == "multi_phase":
        return {"label": "Multi-phase / failed", "badge_class": "bg-danger"}
    if phase_status == "not_confirmed":
        return {"label": "Not yet confirmed", "badge_class": "bg-secondary"}
    if success is True:
        return {"label": "Success", "badge_class": "bg-success"}
    if success is False:
        return {"label": "Failed", "badge_class": "bg-danger"}
    return {"label": "Not yet confirmed", "badge_class": "bg-secondary"}


def _automated_phase_state_meta(phase_state):
    normalized = str(phase_state or "").strip().lower()
    if normalized == "likely single-phase":
        return {"label": "Likely single-phase", "badge_class": "bg-success"}
    if normalized == "likely multiphase":
        return {"label": "Likely multiphase", "badge_class": "bg-warning text-dark"}
    if normalized == "unresolved":
        return {"label": "Unresolved", "badge_class": "bg-secondary"}
    if normalized == "insufficient-quality data":
        return {"label": "Insufficient-quality data", "badge_class": "bg-danger"}
    return {"label": "Not available", "badge_class": "bg-secondary"}


def _job_status_meta(job, *, phase_state=None):
    if job is None:
        return {"label": "Not started", "badge_class": "bg-secondary"}
    if job.status == "succeeded":
        meta = _automated_phase_state_meta(phase_state)
        meta["job_status"] = "succeeded"
        return meta
    if job.status == "failed":
        return {"label": "Analysis failed", "badge_class": "bg-danger", "job_status": "failed"}
    if job.status == "running":
        return {"label": "Running", "badge_class": "bg-primary", "job_status": "running"}
    return {"label": "Queued", "badge_class": "bg-info text-dark", "job_status": "queued"}


def _target_structure_rows(recipe, record):
    rows = [
        ("Structure family", getattr(recipe, "structure_family", None) or "—"),
        ("Expected space group", getattr(record, "spacegroup", None) or "—"),
    ]
    element_sites = getattr(record, "element_sites", None) or {}
    if isinstance(element_sites, dict) and element_sites:
        rows.append(
            (
                "Expected site assignments",
                ", ".join(f"{symbol}: {site}" for symbol, site in element_sites.items()),
            )
        )
    return rows


def _analysis_warning_codes(job, persisted_payload):
    codes = []
    for warning in (getattr(job, "warnings", None) or []):
        code = warning.get("code") if isinstance(warning, dict) else None
        if code and code not in codes:
            codes.append(code)
    for warning in (persisted_payload.get("warnings") or []):
        code = warning.get("code")
        if code and code not in codes:
            codes.append(code)
    return codes


def _artifact_rows(job, persisted):
    rows = []
    for artifact in getattr(persisted, "persisted_artifacts", ()) or ():
        if isinstance(artifact, dict):
            relative_path = str(artifact.get("relative_path") or "")
            artifact_type = artifact.get("artifact_type")
            content_type = artifact.get("content_type")
            size_bytes = artifact.get("size_bytes")
            sha256 = artifact.get("sha256")
        else:
            relative_path = str(getattr(artifact, "relative_path", "") or "")
            artifact_type = getattr(artifact, "artifact_type", None)
            content_type = getattr(artifact, "content_type", None)
            size_bytes = getattr(artifact, "size_bytes", None)
            sha256 = getattr(artifact, "sha256", None)
        artifact_name = Path(relative_path).name
        if artifact_name not in allowed_analysis_artifact_names():
            continue
        rows.append(
            {
                "name": artifact_name,
                "artifact_type": artifact_type,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "download_url": _xrd_analysis_artifact_path(job.analysis_id, artifact_name),
            }
        )
    return sorted(rows, key=lambda item: item["name"])


def _collect_review_history(analysis_id):
    try:
        reviews = list(XRDAnalysisReview.objects(analysis_id=analysis_id).order_by("-created_at"))
    except Exception:
        return [], None
    rows = []
    active_review = None
    for review in reviews:
        row = {
            "id": str(getattr(review, "id", "") or ""),
            "review_status": review.review_status,
            "reviewed_phase_state": review.reviewed_phase_state,
            "selected_hypothesis_id": review.selected_hypothesis_id,
            "added_candidate_identifiers": list(review.added_candidate_identifiers or []),
            "confidence": review.confidence,
            "notes": review.notes,
            "reviewer_username": review.reviewer_username,
            "reviewer_display_name": review.reviewer_display_name or review.reviewer_username,
            "reviewer_organization": review.reviewer_organization,
            "supersedes_review_id": review.supersedes_review_id,
            "is_active": bool(review.is_active),
            "created_at": review.created_at,
            "updated_at": review.updated_at,
        }
        rows.append(row)
        if row["is_active"] and active_review is None:
            active_review = row
    return rows, active_review


def _hypothesis_options(result_payload):
    options = []
    for collection_name, label in (
        ("successful_single_phase_hypotheses", "Single-phase hypothesis"),
        ("successful_two_phase_hypotheses", "Two-phase hypothesis"),
    ):
        for hypothesis in result_payload.get(collection_name) or []:
            hypothesis_id = hypothesis.get("hypothesis_id")
            if not hypothesis_id:
                continue
            candidate_ids = []
            if collection_name == "successful_single_phase_hypotheses":
                if hypothesis.get("candidate_id"):
                    candidate_ids = [hypothesis["candidate_id"]]
            else:
                candidate_ids = list(hypothesis.get("candidate_ids") or [])
            options.append(
                {
                    "hypothesis_id": hypothesis_id,
                    "label": f"{label}: {', '.join(candidate_ids) if candidate_ids else hypothesis_id}",
                }
            )
    return options


def _find_hypothesis_by_id(result_payload, hypothesis_id):
    if not hypothesis_id:
        return None, None
    for collection_name in ("successful_single_phase_hypotheses", "successful_two_phase_hypotheses"):
        for hypothesis in result_payload.get(collection_name) or []:
            if hypothesis.get("hypothesis_id") == hypothesis_id:
                return collection_name, hypothesis
    return None, None


def _selected_model_payload(result_payload):
    selected = result_payload.get("selected_best_model") or {}
    hypothesis_id = selected.get("hypothesis_id")
    if hypothesis_id:
        _, hypothesis = _find_hypothesis_by_id(result_payload, hypothesis_id)
        if hypothesis is not None:
            return selected.get("model_type"), hypothesis
    if selected.get("model_type") == "two_phase" and result_payload.get("best_two_phase_hypothesis"):
        return "two_phase", result_payload["best_two_phase_hypothesis"]
    if selected.get("model_type") == "single_phase" and result_payload.get("best_single_phase_hypothesis"):
        return "single_phase", result_payload["best_single_phase_hypothesis"]
    if result_payload.get("best_single_phase_hypothesis"):
        return "single_phase", result_payload["best_single_phase_hypothesis"]
    if result_payload.get("best_two_phase_hypothesis"):
        return "two_phase", result_payload["best_two_phase_hypothesis"]
    return None, None


def _candidate_label(candidate):
    candidate_id = candidate.get("candidate_id") or "candidate"
    formula = candidate.get("formula")
    if formula:
        return f"{candidate_id} ({formula})"
    return candidate_id


def _candidate_rows(result_payload):
    candidates = result_payload.get("ranked_candidate_shortlist") or result_payload.get("phase_candidates") or []
    rows = []
    for index, candidate in enumerate(candidates, start=1):
        rows.append(
            {
                "rank": index,
                "candidate_id": candidate.get("candidate_id"),
                "label": _candidate_label(candidate),
                "source": candidate.get("source"),
                "formula": candidate.get("formula"),
                "space_group": candidate.get("space_group"),
                "structure_family": candidate.get("structure_family"),
                "combined_pre_rank_score": candidate.get("combined_pre_rank_score"),
                "intended_structure_match": candidate.get("intended_structure_match"),
                "duplicate_cluster_id": candidate.get("duplicate_cluster_id"),
            }
        )
    return rows


def _phase_evidence_rows(model_type, hypothesis):
    if not hypothesis:
        return []
    if model_type == "single_phase":
        return [
            {
                "candidate_id": hypothesis.get("candidate_id"),
                "source": hypothesis.get("candidate_source"),
                "scale_factor": hypothesis.get("phase_scale_factor"),
                "expected_reflection_count": len(hypothesis.get("expected_reflections") or []),
                "unsupported_region_count": len(hypothesis.get("unsupported_strong_predicted_regions") or []),
                "lattice_parameters": hypothesis.get("refined_lattice_parameters") or {},
            }
        ]
    rows = []
    for phase_result in hypothesis.get("phase_results") or []:
        rows.append(
            {
                "candidate_id": phase_result.get("candidate_id"),
                "source": phase_result.get("candidate_source"),
                "scale_factor": phase_result.get("scale_factor"),
                "expected_reflection_count": len(phase_result.get("expected_reflections") or []),
                "unsupported_region_count": len(phase_result.get("unsupported_predicted_regions") or []),
                "lattice_parameters": phase_result.get("refined_lattice_parameters") or {},
            }
        )
    return rows


def _data_quality_rows(result_payload):
    qc = result_payload.get("quality_control") or {}
    if not qc:
        return []
    rows = [
        ("Status", qc.get("status") or "—"),
        ("Pattern type", qc.get("pattern_type") or "—"),
        ("Usable point count", qc.get("usable_point_count")),
        ("Coordinate range", _range_display(qc.get("coordinate_min"), qc.get("coordinate_max"))),
        ("Range width", qc.get("range_width")),
        ("Median step size", qc.get("median_step_size")),
        ("Step-size variation", qc.get("step_size_variation")),
        ("Invalid rows removed", _fraction_percent(qc.get("fraction_invalid_rows_removed"))),
        ("Duplicate coordinates", qc.get("duplicate_count")),
        ("Negative-intensity fraction", _fraction_percent(qc.get("negative_intensity_fraction"))),
        ("Non-positive intensity fraction", _fraction_percent(qc.get("non_positive_intensity_fraction"))),
        ("Approximate signal-to-noise", qc.get("approximate_signal_to_noise")),
        ("Detected peak regions", qc.get("detectable_peak_region_count")),
        ("Missing intervals", qc.get("missing_interval_count")),
        ("Clipping detected", "Yes" if qc.get("clipping_detected") else "No"),
    ]
    return [(label, value if value not in (None, "", []) else "—") for label, value in rows]


def _range_display(minimum, maximum):
    if minimum in (None, "") or maximum in (None, ""):
        return "—"
    return f"{minimum:.4f} to {maximum:.4f}"


def _fraction_percent(value):
    if value in (None, ""):
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return value


def _flatten_warnings(*warning_lists):
    rows = []
    for warning_list in warning_lists:
        for warning in warning_list or []:
            if isinstance(warning, dict):
                rows.append(warning)
    return rows


def _provenance_rows(manifest_payload, result_payload):
    rows = [
        ("Analysis ID", manifest_payload.get("analysis_id") or result_payload.get("analysis_id") or "—"),
        ("Algorithm version", manifest_payload.get("algorithm_version") or result_payload.get("algorithm_version") or "—"),
        ("Configuration version", manifest_payload.get("configuration_version") or result_payload.get("configuration_version") or "—"),
        ("Configuration hash", manifest_payload.get("configuration_hash") or "—"),
        ("Reference snapshot", manifest_payload.get("reference_phase_snapshot_version") or "—"),
        ("GSAS-II version", manifest_payload.get("gsasii_version") or "—"),
        ("Python version", manifest_payload.get("python_version") or "—"),
        ("Raw file hash", manifest_payload.get("raw_file_hash") or "—"),
        ("Parsing method", manifest_payload.get("parsing_method") or "—"),
        ("Candidate simulation method", manifest_payload.get("candidate_simulation_method") or "—"),
        ("Refinement method", manifest_payload.get("refinement_method") or "—"),
        ("Completed at", manifest_payload.get("completed_at") or "—"),
    ]
    return rows


def _plot_series_points(xs, ys, *, x_min, x_max, y_min, y_max, width=960, height=320):
    if not xs or not ys or x_max <= x_min or y_max <= y_min:
        return ""
    points = []
    for x_value, y_value in zip(xs, ys):
        try:
            x_numeric = float(x_value)
            y_numeric = float(y_value)
        except (TypeError, ValueError):
            continue
        x_pos = ((x_numeric - x_min) / (x_max - x_min)) * width
        y_pos = height - (((y_numeric - y_min) / (y_max - y_min)) * height)
        points.append(f"{x_pos:.2f},{y_pos:.2f}")
    return " ".join(points)


def _normalized_pattern_plot_context(result_payload):
    model_type, hypothesis = _selected_model_payload(result_payload)
    parsed_pattern = result_payload.get("parsed_pattern") or {}
    observed_x = list(parsed_pattern.get("normalized_two_theta") or [])
    observed_y = list(parsed_pattern.get("original_intensities") or parsed_pattern.get("normalized_intensities") or [])
    if hypothesis:
        observed_x = list(hypothesis.get("observed_two_theta") or observed_x)
        observed_y = list(hypothesis.get("observed_intensities") or observed_y)
    calculated_y = list(hypothesis.get("calculated_total_pattern") or []) if hypothesis else []
    background_y = list(hypothesis.get("calculated_background") or []) if hypothesis else []
    difference_y = list(hypothesis.get("difference_pattern") or []) if hypothesis else []
    if not observed_x or not observed_y:
        return None

    x_min = min(observed_x)
    x_max = max(observed_x)
    primary_candidates = [float(value) for value in observed_y if isinstance(value, (int, float))]
    primary_candidates.extend(
        float(value)
        for value in calculated_y + background_y
        if isinstance(value, (int, float))
    )
    primary_max = max(primary_candidates) if primary_candidates else 1.0
    primary_max = primary_max or 1.0
    difference_abs = max((abs(float(value)) for value in difference_y if isinstance(value, (int, float))), default=1.0) or 1.0

    observed_norm = [float(value) / primary_max for value in observed_y]
    calculated_norm = [float(value) / primary_max for value in calculated_y] if calculated_y else []
    background_norm = [float(value) / primary_max for value in background_y] if background_y else []
    difference_norm = [0.16 + (float(value) / difference_abs) * 0.12 for value in difference_y] if difference_y else []

    series = []
    for series_id, label, values, color, visible in (
        ("observed", "Observed pattern", observed_norm, "#0f172a", True),
        ("calculated", "Calculated model", calculated_norm, "#0f766e", True),
        ("background", "Calculated background", background_norm, "#b45309", False),
        ("difference", "Difference pattern", difference_norm, "#dc2626", True),
    ):
        if not values:
            continue
        series.append(
            {
                "id": series_id,
                "label": label,
                "color": color,
                "visible": visible,
                "points": _plot_series_points(
                    observed_x,
                    values,
                    x_min=x_min,
                    x_max=x_max,
                    y_min=0.0,
                    y_max=1.05,
                ),
            }
        )

    reflections = []
    if hypothesis:
        expected_reflections = []
        if model_type == "single_phase":
            expected_reflections = hypothesis.get("expected_reflections") or []
        elif model_type == "two_phase":
            for phase_result in hypothesis.get("phase_results") or []:
                expected_reflections.extend(phase_result.get("expected_reflections") or [])
        for reflection in expected_reflections:
            two_theta = reflection.get("two_theta")
            try:
                two_theta_value = float(two_theta)
            except (TypeError, ValueError):
                continue
            if x_max <= x_min:
                continue
            x_pos = ((two_theta_value - x_min) / (x_max - x_min)) * 960
            reflections.append(
                {
                    "x": f"{x_pos:.2f}",
                    "two_theta": two_theta_value,
                    "intensity": reflection.get("predicted_intensity"),
                    "hkl": f"({reflection.get('h')}{reflection.get('k')}{reflection.get('l')})",
                }
            )

    return {
        "x_min": x_min,
        "x_max": x_max,
        "series": series,
        "reflections": reflections,
    }


def _latest_analysis_card_context(request, recipe, record):
    job = _latest_trial_xrd_analysis_job(recipe.id, record.trial_id)
    persisted = None
    persisted_error = None
    result_payload = {}
    summary_payload = {}
    active_review = None
    if job is not None and job.status == "succeeded":
        persisted, persisted_error = _load_visible_persisted_analysis(recipe, record, job.analysis_id)
        if persisted is not None:
            result_payload = to_jsonable(persisted.result)
            summary_payload = to_jsonable(persisted.summary)
            _, active_review = _collect_review_history(job.analysis_id)
    phase_state = result_payload.get("phase_state") or summary_payload.get("phase_state")
    status_meta = _job_status_meta(job, phase_state=phase_state)
    can_submit = bool(_user_can_submit_xrd_analysis(request.user))
    return {
        "job": job,
        "persisted": persisted,
        "persisted_error": persisted_error,
        "summary": summary_payload,
        "result": result_payload,
        "phase_state_meta": _automated_phase_state_meta(phase_state) if phase_state else None,
        "status_meta": status_meta,
        "detail_url": _xrd_analysis_detail_path(recipe.id, record.trial_id, job.analysis_id) if job else None,
        "submit_url": _xrd_analysis_submit_path(recipe.id, record.trial_id),
        "status_url": _xrd_analysis_status_path(job.id) if job else None,
        "result_url": _xrd_analysis_result_path(job.analysis_id) if job and job.status == "succeeded" else None,
        "can_submit": can_submit,
        "allow_submit_now": bool(can_submit and job is not None and job.status not in {"queued", "running"}) or bool(can_submit and job is None),
        "active_review": active_review,
    }


def trial_detail(request, recipe_id, trial_id):
    recipe, record = _resolve_visible_trial(request, recipe_id, trial_id)
    if record is None:
        return render(request, "404.html", {"message": f"Trial {trial_id} not found."}, status=404)

    exp_condition = getattr(record, "exp_condition", None)
    additional = getattr(exp_condition, "additional_params", {}) or {}
    synthesis_steps_display = _render_synthesis_steps(additional)
    if not synthesis_steps_display and recipe.synthesis_steps:
        synthesis_steps_display = _render_steps_list(recipe.synthesis_steps)

    additional_notes = []
    for key, value in additional.items():
        if key in {"synthesis_steps", "xrd_metadata", "diffraction_metadata", "file_hash"} or value in (None, "", [], {}):
            continue
        label = str(key).replace("_", " ").strip().title()
        additional_notes.append((label, value))

    metadata_rows = []
    plot_url = None
    detected_peaks = []
    plot_notice = None
    full_gsas_applied = False

    # Operator-triggered full Rietveld refinement. The automated pipeline above
    # answers a different question (which phases are present) and cannot answer
    # it until the reference library covers the sample's chemistry; this is the
    # manual fit that has always worked, so it stays reachable independently.
    use_full_gsas = request.GET.get("refine_gsas", "").strip().lower() in ("1", "true", "yes") or \
        os.environ.get("LOOP_GSAS_FULL_SYNC", "").strip().lower() in ("1", "true", "yes")

    file_hash = additional.get("file_hash")
    has_xrd_csv = bool(xrd_store.resolve_raw_path(recipe.id, trial_id))
    if has_xrd_csv:
        try:
            entry = xrd_store.get_or_build(
                recipe.id, trial_id, file_hash,
                variant="gsas" if use_full_gsas else "fast",
            )
            detected_peaks = entry.peaks
            plot_url = _versioned_media_url(entry.overlay_url, file_hash)
            full_gsas_applied = entry.variant == "gsas"
            if entry.plot_style == "stick":
                plot_notice = "Reference reflection list (calculated stick pattern)."
            elif not detected_peaks:
                plot_notice = "No peaks were detected."
        except Exception:
            plot_url = None
            detected_peaks = []
            plot_notice = "Plot is unavailable for this trial."

    if not metadata_rows:
        stored_metadata = additional.get("xrd_metadata") or additional.get("diffraction_metadata") or []
        for item in stored_metadata:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                key, value = item[0], item[1]
            elif isinstance(item, dict):
                key = item.get("key") or item.get("name") or item.get("field")
                value = item.get("value")
            else:
                continue
            key_str = str(key).strip() if key is not None else ""
            value_str = str(value).strip() if value is not None else ""
            if not key_str and not value_str:
                continue
            pretty_key = key_str.replace("_", " ").replace("-", " ").strip().title() or "Field"
            metadata_rows.append((pretty_key, value_str))

    latest_analysis = _latest_analysis_card_context(request, recipe, record)
    context = {
        "material_auid": recipe.material_auid,
        "recipe_id": recipe.id,
        "trial_id": trial_id,
        "record": record,
        "elements": _display_elements(recipe.elements or {}),
        "structure_family": recipe.structure_family,
        "metadata_rows": metadata_rows,
        "plot_url": plot_url,
        "detected_peaks": detected_peaks,
        "plot_notice": plot_notice,
        "synthesis_steps_display": synthesis_steps_display,
        "additional_notes": additional_notes,
        "can_modify_trial": _is_uploader_or_superuser(request.user, getattr(record, "experimenter", "")),
        "can_delete_trial": _is_uploader_or_superuser(request.user, getattr(record, "experimenter", "")),
        "has_xrd_csv": has_xrd_csv,
        "full_gsas_applied": full_gsas_applied,
        "human_phase_meta": _human_phase_status_meta(record),
        "target_structure_rows": _target_structure_rows(recipe, record),
        "latest_analysis": latest_analysis,
    }
    return render(request, "catalog/trial_detail.html", context)


def xrd_analysis_detail(request, recipe_id, trial_id, analysis_id):
    recipe, record = _resolve_visible_trial(request, recipe_id, trial_id)
    if record is None:
        return render(request, "404.html", {"message": f"Trial {trial_id} not found."}, status=404)

    job = _analysis_job_for_trial(recipe.id, trial_id, analysis_id)
    if job is None:
        return render(
            request,
            "404.html",
            {"message": f"XRD analysis {analysis_id} not found for trial {trial_id}."},
            status=404,
        )

    persisted = None
    persisted_error = None
    result_payload = {}
    summary_payload = {}
    manifest_payload = {}
    artifact_rows = []
    plot_context = None
    candidate_rows = []
    hypothesis_options = []
    warnings = []
    phase_rows = []
    active_review = None
    review_history = []
    model_type = None
    selected_hypothesis = None

    if job.status == "succeeded":
        persisted, persisted_error = _load_visible_persisted_analysis(recipe, record, analysis_id)
        if persisted is not None:
            result_payload = to_jsonable(persisted.result)
            summary_payload = to_jsonable(persisted.summary)
            manifest_payload = to_jsonable(persisted.reproducibility_manifest)
            artifact_rows = _artifact_rows(job, persisted)
            plot_context = _normalized_pattern_plot_context(result_payload)
            candidate_rows = _candidate_rows(result_payload)
            hypothesis_options = _hypothesis_options(result_payload)
            warnings = _flatten_warnings(
                getattr(job, "warnings", None) or [],
                result_payload.get("warnings") or [],
            )
            model_type, selected_hypothesis = _selected_model_payload(result_payload)
            phase_rows = _phase_evidence_rows(model_type, selected_hypothesis)
            review_history, active_review = _collect_review_history(analysis_id)

    status_meta = _job_status_meta(job, phase_state=result_payload.get("phase_state") if result_payload else None)
    context = {
        "material_auid": recipe.material_auid,
        "recipe_id": recipe.id,
        "trial_id": trial_id,
        "record": record,
        "analysis_id": analysis_id,
        "job": job,
        "persisted_error": persisted_error,
        "result_payload": result_payload,
        "summary_payload": summary_payload,
        "manifest_payload": manifest_payload,
        "artifact_rows": artifact_rows,
        "plot_context": plot_context,
        "candidate_rows": candidate_rows,
        "hypothesis_options": hypothesis_options,
        "selected_model_type": model_type,
        "selected_hypothesis": selected_hypothesis,
        "phase_rows": phase_rows,
        "warnings": warnings,
        "warning_codes": _analysis_warning_codes(job, result_payload),
        "data_quality_rows": _data_quality_rows(result_payload),
        "provenance_rows": _provenance_rows(manifest_payload, result_payload),
        "status_meta": status_meta,
        "human_phase_meta": _human_phase_status_meta(record),
        "target_structure_rows": _target_structure_rows(recipe, record),
        "review_status_values": XRD_ANALYSIS_REVIEW_STATUS_VALUES,
        "phase_state_values": XRD_ANALYSIS_PHASE_STATE_VALUES,
        "review_history": review_history,
        "active_review": active_review,
        "can_review_xrd_analysis": _user_can_review_xrd_analysis(request.user),
        "review_post_url": _xrd_analysis_review_path(recipe.id, trial_id, analysis_id),
        "status_url": _xrd_analysis_status_path(job.id),
        "result_url": _xrd_analysis_result_path(analysis_id) if job.status == "succeeded" else None,
    }
    return render(request, "catalog/xrd_analysis_detail.html", context)


@login_required
@require_POST
def xrd_analysis_review_create(request, recipe_id, trial_id, analysis_id):
    recipe, record = _resolve_visible_trial(request, recipe_id, trial_id)
    if record is None:
        return render(request, "404.html", {"message": f"Trial {trial_id} not found."}, status=404)
    if not _user_can_review_xrd_analysis(request.user):
        return redirect(settings.AWAITING_APPROVAL_URL_NAME)

    job = _analysis_job_for_trial(recipe.id, trial_id, analysis_id)
    if job is None or job.status != "succeeded":
        messages.error(request, "A completed automated XRD analysis is required before adding expert review.")
        return redirect(_xrd_analysis_detail_path(recipe.id, trial_id, analysis_id))

    persisted, persisted_error = _load_visible_persisted_analysis(recipe, record, analysis_id)
    if persisted is None:
        messages.error(
            request,
            (persisted_error or {}).get("detail") or "Persisted XRD analysis result is unavailable for review.",
        )
        return redirect(_xrd_analysis_detail_path(recipe.id, trial_id, analysis_id))

    review_status = str(request.POST.get("review_status") or "").strip()
    reviewed_phase_state = str(request.POST.get("reviewed_phase_state") or "").strip()
    selected_hypothesis_id = str(request.POST.get("selected_hypothesis_id") or "").strip()
    added_candidate_identifiers = [
        token.strip()
        for token in re.split(r"[\n,]+", str(request.POST.get("added_candidate_identifiers") or ""))
        if token.strip()
    ]
    confidence = str(request.POST.get("confidence") or "").strip() or None
    notes = str(request.POST.get("notes") or "").strip()

    if review_status not in XRD_ANALYSIS_REVIEW_STATUS_VALUES:
        messages.error(request, "Review status is invalid.")
        return redirect(_xrd_analysis_detail_path(recipe.id, trial_id, analysis_id))
    if reviewed_phase_state and reviewed_phase_state not in XRD_ANALYSIS_PHASE_STATE_VALUES:
        messages.error(request, "Reviewed phase state is invalid.")
        return redirect(_xrd_analysis_detail_path(recipe.id, trial_id, analysis_id))

    result_payload = to_jsonable(persisted.result)
    if selected_hypothesis_id:
        _, matched_hypothesis = _find_hypothesis_by_id(result_payload, selected_hypothesis_id)
        if matched_hypothesis is None:
            messages.error(request, "Selected hypothesis is not part of the persisted automated analysis.")
            return redirect(_xrd_analysis_detail_path(recipe.id, trial_id, analysis_id))

    previous_active = None
    try:
        previous_active = XRDAnalysisReview.objects(analysis_id=analysis_id, is_active=True).first()
    except Exception:
        previous_active = None
    if previous_active is not None:
        previous_active.is_active = False
        previous_active.save()

    review = XRDAnalysisReview(
        analysis_id=analysis_id,
        material_auid=recipe.material_auid,
        recipe_auid=recipe.id,
        trial_id=trial_id,
        reviewer_username=request.user.username,
        reviewer_display_name=request.user.get_full_name() or request.user.username,
        reviewer_organization=", ".join(_user_affiliations(request.user)),
        review_status=review_status,
        reviewed_phase_state=reviewed_phase_state or None,
        selected_hypothesis_id=selected_hypothesis_id or None,
        added_candidate_identifiers=added_candidate_identifiers,
        confidence=confidence,
        notes=notes,
        supersedes_review_id=str(getattr(previous_active, "id", "") or "") or None,
        is_active=True,
    )
    review.save()
    messages.success(request, "Expert review saved as a separate record.")
    return redirect(_xrd_analysis_detail_path(recipe.id, trial_id, analysis_id))


def trial_xrd_cache_api(request, recipe_id, trial_id):
    """JSON manifest of a trial's raw + processed XRD artifacts (built on demand)."""
    recipe, record = _resolve_visible_trial(request, recipe_id, trial_id)
    if record is None:
        return JsonResponse({"error": "not found"}, status=404)

    additional = getattr(getattr(record, "exp_condition", None), "additional_params", {}) or {}
    if not xrd_store.resolve_raw_path(recipe.id, trial_id):
        return JsonResponse({"error": "no raw XRD file for this trial"}, status=404)
    try:
        manifest = xrd_store.read_manifest(
            recipe.id, trial_id, additional.get("file_hash")
        )
    except Exception as exc:
        return JsonResponse({"error": f"could not build cache: {exc}"}, status=500)
    return JsonResponse(manifest)


def trial_xrd_thumb_api(request, recipe_id, trial_id):
    """Fast-variant XRD plot for the inline trial drawer, fetched lazily on first
    open. Returns ``ok`` even for "no data" so the client can tell it from a
    transport error."""
    recipe, record = _resolve_visible_trial(request, recipe_id, trial_id)
    if record is None:
        return JsonResponse({"ok": False, "notice": "Trial not found."}, status=404)

    if not xrd_store.resolve_raw_path(recipe.id, trial_id):
        return JsonResponse({"ok": True, "plot_url": None,
                             "notice": "No plottable XRD data for this trial."})

    additional = getattr(getattr(record, "exp_condition", None), "additional_params", {}) or {}
    try:
        entry = xrd_store.get_or_build(
            recipe.id, trial_id, additional.get("file_hash"), variant="fast",
        )
    except Exception:
        return JsonResponse({"ok": False, "notice": "Plot unavailable for this trial."})

    peaks = [
        {"two_theta": p.get("two_theta"), "intensity": p.get("intensity")}
        for p in (entry.peaks or [])[:6]
        if isinstance(p, dict)
    ]
    notice = None
    if entry.plot_style == "stick":
        notice = "Reference reflection list (calculated stick pattern)."
    elif not entry.peaks:
        notice = "No peaks were detected."
    return JsonResponse({
        "ok": True,
        "plot_url": _versioned_media_url(entry.overlay_url, additional.get("file_hash")),
        "peaks": peaks,
        "notice": notice,
    })


# =============================================================================
# Bulk literature management
# =============================================================================

@login_required
def my_literature(request):
    """List the caller's literature records with checkboxes for bulk removal.

    Exists because deletion was previously one record at a time, which does not
    scale: a single reorganisation can involve hundreds of entries spread across
    many materials, so there was no single page from which to act on them.
    """
    from catalog.services import bulk_literature_delete as bulk

    show_all = bool(request.GET.get("all")) and request.user.is_superuser
    rows = bulk.list_user_literature(request.user.username, include_all=show_all)

    return render(request, "catalog/my_literature.html", {
        "rows": rows,
        "show_all": show_all,
        "can_show_all": request.user.is_superuser,
        "max_selection": bulk.MAX_SELECTION,
        "over_limit": len(rows) > bulk.MAX_SELECTION,
    })


@login_required
@require_POST
def bulk_delete_literature(request):
    """Confirm, then delete, a multi-record selection.

    Two-step on purpose. The DOI match inherited from ``delete_literature`` can
    remove entries the user did not tick, so the count is shown before anything
    is written rather than reported afterwards.
    """
    from catalog.services import bulk_literature_delete as bulk

    selections = bulk.parse_selection(request.POST.getlist("selected"))
    if not selections:
        messages.error(request, "No records were selected.")
        return redirect("my_literature")

    if len(selections) > bulk.MAX_SELECTION:
        # Refuse clearly rather than letting Django's field cap produce an
        # uninterpretable 400 further along.
        messages.error(
            request,
            f"Select at most {bulk.MAX_SELECTION} records at a time "
            f"(you selected {len(selections)}). Delete them in batches.",
        )
        return redirect("my_literature")

    # The plan is rebuilt from the posted ids at both steps, so a tampered or
    # stale confirmation form cannot widen what gets deleted.
    if request.POST.get("confirm") != "yes":
        plan = bulk.build_plan(request.user, selections)
        if not plan.rows:
            for note in plan.denied:
                messages.error(request, f"Not allowed to delete {note}.")
            if plan.missing:
                messages.error(request, f"{len(plan.missing)} selected record(s) no longer exist.")
            if not plan.denied and not plan.missing:
                messages.error(request, "Nothing to delete.")
            return redirect("my_literature")
        return render(request, "catalog/my_literature_confirm.html", {"plan": plan})

    result = bulk.execute_plan(request.user, selections)

    if result["removed"]:
        messages.success(
            request,
            f"Deleted {result['removed']} literature record"
            f"{'' if result['removed'] == 1 else 's'} "
            f"across {result['recipes_touched']} recipe"
            f"{'' if result['recipes_touched'] == 1 else 's'}.",
        )
    else:
        messages.warning(request, "No records were deleted.")
    for note in result["denied"]:
        messages.error(request, f"Skipped {note}: not yours to delete.")
    if result["missing"]:
        messages.warning(request, f"{len(result['missing'])} record(s) had already been removed.")

    return redirect("my_literature")


# =============================================================================
# Delete routes
# =============================================================================

@login_required
@require_POST
def delete_literature(request, recipe_id, lit_id):
    recipe = get_recipe(recipe_id)
    if recipe is None:
        messages.error(request, "Recipe not found.")
        return redirect("browse_data")
    record = find_embedded_literature(recipe, lit_id)
    if record is None:
        messages.error(request, "Literature entry not found.")
        return redirect("recipe_detail", recipe_id=recipe_id)
    if not _is_uploader_or_superuser(request.user, getattr(record, "extracted_by", "")):
        messages.error(request, "You are not allowed to delete this literature entry.")
        return redirect("literature_detail", recipe_id=recipe_id, lit_id=lit_id)

    doi_norm = (record.doi or "").strip().lower()
    record_lit_id = getattr(record, "lit_id", None) or lit_id
    recipe.literature = [
        lit for lit in (recipe.literature or [])
        if getattr(lit, "lit_id", None) != record_lit_id
        and (lit.doi or "").strip().lower() != doi_norm
    ]
    recipe.save()
    messages.success(request, "Literature entry deleted.")
    return redirect("composition_detail", material_auid=recipe.material_auid)


@login_required
@require_POST
def delete_computational(request, material_auid, comp_auid):
    material = get_material(material_auid)
    if material is None:
        messages.error(request, "Material not found.")
        return redirect("browse_data")
    record = find_embedded_dft(material, comp_auid)
    if record is None:
        messages.error(request, "Computational data not found.")
        return redirect("composition_detail", material_auid=material_auid)
    if not _is_uploader_or_superuser(request.user, getattr(record, "uploaded_by", "")):
        messages.error(request, "You are not allowed to delete this computational data.")
        return redirect("computational_detail", material_auid=material_auid, comp_auid=comp_auid)

    material.dft_calculations = [
        d for d in (material.dft_calculations or []) if d.comp_auid != comp_auid
    ]
    material.save()
    signals.delete_comp_embedding(comp_auid)
    messages.success(request, "Computational data deleted.")
    return redirect("composition_detail", material_auid=material_auid)


@login_required
@require_POST
def delete_trial(request, recipe_id, trial_id):
    recipe = get_recipe(recipe_id)
    if recipe is None:
        messages.error(request, "Recipe not found.")
        return redirect("browse_data")
    record = find_embedded_trial(recipe, trial_id)
    if record is None:
        messages.error(request, "Trial not found.")
        return redirect("recipe_detail", recipe_id=recipe_id)
    if not _is_uploader_or_superuser(request.user, getattr(record, "experimenter", "")):
        messages.error(request, "You are not allowed to delete this trial.")
        return redirect("trial_detail", recipe_id=recipe_id, trial_id=trial_id)

    recipe.trials = [t for t in (recipe.trials or []) if t.trial_id != trial_id]
    recipe.save()
    # Remove the trial's XRD folder so a reused trial_id can't inherit its data.
    folder = xrd_store.trial_path(recipe.id, trial_id)
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    messages.success(request, "Trial deleted.")
    return redirect("composition_detail", material_auid=recipe.material_auid)


@login_required
@require_POST
def delete_material(request, material_auid):
    """Superuser-only: wipe a material, all its recipes, and its embeddings."""
    if not bool(getattr(request.user, "is_superuser", False)):
        messages.error(request, "Only superusers can delete material classes.")
        return redirect("composition_detail", material_auid=material_auid)

    Recipe.objects(material_auid=material_auid).delete()
    MLEmbedding.objects(material_auid=material_auid).delete()
    Material.objects(id=material_auid).delete()
    messages.success(request, f"Material {material_auid} deleted.")
    return redirect("browse_data")


# =============================================================================
# Annotation editor — in-place on the Material document
# =============================================================================

@login_required
@require_POST
def modify_material_annotation(request, material_auid):
    """Update a material-level annotation field (JSON API)."""
    material = get_material(material_auid)
    if material is None:
        return JsonResponse({"error": f"Material {material_auid} not found"}, status=404)

    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    field = data.get("field")
    value = data.get("value")
    if field not in {"notes", "display_name", "curator", "default_visibility_affiliations"}:
        return JsonResponse({"error": f"Unknown annotation field: {field}"}, status=400)

    setattr(material, field, value)
    material.save()
    return JsonResponse({"success": True, "material_auid": material_auid})


# =============================================================================
# Add / edit: experimental data
# =============================================================================

@login_required
def upload_exp_data(request):
    """Upload an experimental trial. XRD CSV is optional."""

    plot_image = None
    existing_material_auid = request.GET.get("material_auid") or request.POST.get("material_auid")
    edit_recipe_id = request.GET.get("edit_recipe_id") or request.POST.get("edit_recipe_id")
    edit_trial_id = request.GET.get("edit_trial_id") or request.POST.get("edit_trial_id")

    edit_recipe = None
    edit_trial_record = None
    locked_elements = {}
    locked_structure = None
    post_form_state = {}
    post_synthesis_steps = []

    if edit_recipe_id and edit_trial_id:
        edit_recipe = get_recipe(edit_recipe_id)
        if edit_recipe is None:
            messages.error(request, "Recipe not found for modify operation.")
            return redirect("browse_data")
        edit_trial_record = find_embedded_trial(edit_recipe, edit_trial_id)
        if edit_trial_record is None:
            messages.error(request, "Trial not found for modify operation.")
            return redirect("browse_data")
        if not _is_uploader_or_superuser(request.user, getattr(edit_trial_record, "experimenter", "")):
            messages.error(request, "Only the uploader or a superuser can modify this trial.")
            return redirect("trial_detail", recipe_id=edit_recipe_id, trial_id=edit_trial_id)
        existing_material_auid = edit_recipe.material_auid
        locked_elements = edit_recipe.elements or {}
        locked_structure = edit_recipe.structure_family
    elif existing_material_auid:
        material = get_material(existing_material_auid)
        if material is not None:
            locked_elements = material.elements or {}
            locked_structure = material.structure_family

    if request.method == "POST":
        post_form_state = {
            "raw_data_type": request.POST.get("raw_data_type", "xrd"),
            "structure_family": request.POST.get("structure_family", locked_structure or "rocksalt"),
            "phase_status": request.POST.get("phase_status", "not_confirmed"),
            "comments": request.POST.get("comments", "") or request.POST.get("notes", ""),
            "elements_json": request.POST.get("elements", "{}"),
            "spacegroup": request.POST.get("spacegroup", "unknown"),
            "element_sites_json": request.POST.get("element_sites", "{}"),
        }
        post_synthesis_steps = _parse_synthesis_steps_from_request(request)
        try:
            if edit_recipe is not None:
                raw_elements = edit_recipe.elements or {}
                structure_family = edit_recipe.structure_family
            else:
                elements_raw = _parse_elements_payload(request.POST.get("elements", "{}"))
                raw_elements = normalize_elements_payload(elements_raw)
                structure_family = _normalize_structure_family(
                    request.POST.get("structure_family", "rocksalt")
                )

            result = persist_experimental_trial(
                user=request.user,
                request=request,
                raw_elements=raw_elements,
                structure_family=structure_family,
                synthesis_steps=post_synthesis_steps,
                phase_status=request.POST.get("phase_status", "not_confirmed"),
                spacegroup=request.POST.get("spacegroup", "unknown") or "unknown",
                element_sites=_parse_element_sites(request),
                raw_data_type=request.POST.get("raw_data_type", "xrd"),
                notes=request.POST.get("comments", "") or request.POST.get("notes", ""),
                csv_file=request.FILES.get("csv_file"),
                edit_recipe=edit_recipe,
                edit_trial_record=edit_trial_record,
                edit_recipe_id=edit_recipe_id,
                edit_trial_id=edit_trial_id,
            )
            for warning in result.get("warnings", []):
                messages.warning(request, warning)
            material_auid = result["material_auid"]

            messages.success(
                request,
                f"{'Experimental data updated' if edit_trial_record is not None else 'Experimental data added'} to {material_auid}",
            )
            return redirect("composition_detail", material_auid=material_auid)

        except DuplicateFileError as exc:
            messages.error(request, str(exc))
        except Exception as exc:
            messages.error(request, f"Error saving data: {exc}")

    if post_form_state.get("phase_status"):
        initial_phase_status = post_form_state["phase_status"]
    elif edit_trial_record is not None:
        existing_phase_status = getattr(edit_trial_record, "phase_status", None)
        if existing_phase_status in ("single_phase", "multi_phase", "not_confirmed"):
            initial_phase_status = existing_phase_status
        elif edit_trial_record.success is True:
            initial_phase_status = "single_phase"
        elif edit_trial_record.success is False:
            initial_phase_status = "multi_phase"
        else:
            initial_phase_status = "not_confirmed"
    else:
        initial_phase_status = "not_confirmed"

    context = {
        "structure_families": ["unknown", "rocksalt", "pyrochlore", "spinel", "perovskite", "fluorite", "other"],
        "cooling_methods": ["air_quench", "furnace_cool", "quench", "slow_cool", "other"],
        "raw_data_types": ["xrd", "sem", "tem", "eds", "other"],
        "plot_image": plot_image.replace("data:image/png;base64,", "") if plot_image else None,
        "existing_material_auid": existing_material_auid,
        "edit_recipe_id": edit_recipe_id,
        "edit_trial_id": edit_trial_id,
        "existing_trial": edit_trial_record,
        "initial_phase_status": initial_phase_status,
        "edit_trial_synthesis_steps_json": json.dumps(
            post_synthesis_steps if request.method == "POST" else (
                list(edit_recipe.synthesis_steps or []) if edit_recipe else []
            )
        ),
        "locked_elements_json": json.dumps(_elements_to_selection_list(locked_elements)),
        "locked_elements_display": _elements_to_selection_list(locked_elements),
        "locked_structure_family": locked_structure,
        "post_form_state": post_form_state,
        "structure_family_spacegroups": json.dumps(STRUCTURE_FAMILY_SPACEGROUPS),
        "structure_family_sites": json.dumps(STRUCTURE_FAMILY_SITES),
        "existing_spacegroup": getattr(edit_trial_record, "spacegroup", None) or "",
        "existing_element_sites_json": json.dumps(getattr(edit_trial_record, "element_sites", None) or {}),
    }
    return render(request, "catalog/upload_exp_data.html", context)


# =============================================================================
# Add / edit: literature data
# =============================================================================

@login_required
def add_literature_data(request):
    existing_material_auid = request.GET.get("material_auid") or request.POST.get("material_auid")
    edit_recipe_id = request.GET.get("edit_recipe_id") or request.POST.get("edit_recipe_id")
    edit_lit_id = request.GET.get("edit_lit_id") or request.POST.get("edit_lit_id")

    existing_recipe = None
    existing_literature = None
    locked_elements = {}
    locked_structure = None

    if edit_recipe_id and edit_lit_id:
        existing_recipe = get_recipe(edit_recipe_id)
        if existing_recipe is None:
            messages.error(request, "Recipe not found for modify operation.")
            return redirect("browse_data")
        existing_literature = find_embedded_literature(existing_recipe, edit_lit_id)
        if existing_literature is None:
            messages.error(request, "Literature entry not found for modify operation.")
            return redirect("browse_data")
        if not _is_uploader_or_superuser(request.user, getattr(existing_literature, "extracted_by", "")):
            messages.error(request, "Only the uploader or a superuser can modify this literature entry.")
            return redirect("literature_detail", recipe_id=edit_recipe_id, lit_id=edit_lit_id)
        existing_material_auid = existing_recipe.material_auid
        locked_elements = existing_recipe.elements or {}
        locked_structure = existing_recipe.structure_family
    elif existing_material_auid:
        material = get_material(existing_material_auid)
        if material is not None:
            locked_elements = material.elements or {}
            locked_structure = material.structure_family

    initial = {}
    if existing_literature and request.method != "POST":
        initial = {
            "doi": existing_literature.doi,
            "title": existing_literature.title or "",
            "authors": ", ".join(existing_literature.authors or []),
            "journal": existing_literature.journal or "",
            "year": existing_literature.year,
            "synthesis_successful": "true" if existing_literature.synthesis_successful else "false",
            "findings": existing_literature.notes or "",
        }
    edit_doi = getattr(existing_literature, "doi", None) if existing_literature else None
    form = LiteratureDataForm(request.POST or None, initial=initial)

    if request.method == "POST":
        try:
            if existing_recipe is not None:
                raw_elements = existing_recipe.elements or {}
                structure_family = existing_recipe.structure_family
            elif existing_material_auid:
                # Locked composition: form does not POST periodic-table "elements".
                material = get_material(existing_material_auid)
                if material is None:
                    messages.error(
                        request,
                        f"Material {existing_material_auid} not found.",
                    )
                    raise ValueError("material_missing")
                raw_elements = normalize_elements_payload(material.elements or {})
                structure_family = _normalize_structure_family(
                    material.structure_family or "rocksalt"
                )
            else:
                elements_raw = _parse_elements_payload(request.POST.get("elements", "{}"))
                raw_elements = normalize_elements_payload(elements_raw)
                structure_family = _normalize_structure_family(
                    request.POST.get("structure_family", "rocksalt")
                )

            spacegroup = request.POST.get("spacegroup", "unknown") or "unknown"
            element_sites = _parse_element_sites(request)

            synthesis_steps = _parse_synthesis_steps_from_request(request)

            doi = (request.POST.get("doi") or "").strip()
            if not doi:
                messages.error(request, "DOI is required for a literature entry.")
                raise ValueError("doi_required")

            result = persist_literature_entry(
                user=request.user,
                raw_elements=raw_elements,
                structure_family=structure_family,
                synthesis_steps=synthesis_steps,
                doi=doi,
                synthesis_successful=request.POST.get("synthesis_successful") == "true",
                title=request.POST.get("title", ""),
                authors=[a.strip() for a in request.POST.get("authors", "").split(",") if a.strip()],
                journal=request.POST.get("journal", ""),
                year=_safe_int(request.POST.get("year")),
                findings=request.POST.get("findings", ""),
                spacegroup=spacegroup,
                element_sites=element_sites,
                existing_recipe=existing_recipe,
                existing_literature=existing_literature,
                edit_lit_id=edit_lit_id,
                edit_doi=edit_doi,
                legacy={
                    "milling_time": request.POST.get("milling_time"),
                    "milling_rpm": request.POST.get("milling_rpm"),
                    "atmosphere": request.POST.get("atmosphere"),
                    "cooling_method": request.POST.get("cooling_method"),
                },
            )
            material_auid = result["material_auid"]

            messages.success(
                request,
                f"{'Literature reference updated' if existing_literature is not None else 'Literature reference added'} to {material_auid}",
            )
            return redirect("composition_detail", material_auid=material_auid)

        except ValueError as exc:
            # Silent cases: message already set before raise.
            if str(exc) not in ("doi_required", "material_missing"):
                messages.error(request, str(exc))
        except Exception as exc:
            messages.error(request, f"Error saving data: {exc}")

    context = {
        "form": form,
        "structure_families": ["unknown", "rocksalt", "pyrochlore", "spinel", "perovskite", "fluorite", "other"],
        "cooling_methods": ["air_quench", "furnace_cool", "quench", "slow_cool", "other"],
        "existing_material_auid": existing_material_auid,
        "existing_literature": existing_literature,
        "edit_recipe_id": edit_recipe_id,
        "edit_lit_id": edit_lit_id,
        "edit_doi": edit_doi,
        "edit_literature_synthesis_steps_json": json.dumps(
            list(existing_recipe.synthesis_steps or []) if existing_recipe else []
        ),
        "locked_elements_json": json.dumps(_elements_to_selection_list(locked_elements)),
        "locked_elements_display": _elements_to_selection_list(locked_elements),
        "locked_structure_family": locked_structure,
        "structure_family_spacegroups": json.dumps(STRUCTURE_FAMILY_SPACEGROUPS),
        "structure_family_sites": json.dumps(STRUCTURE_FAMILY_SITES),
        # On POST errors preserve what the user typed; on GET use the edit record's value.
        "existing_spacegroup": (
            request.POST.get("spacegroup", "")
            if request.method == "POST"
            else (getattr(existing_literature, "spacegroup", None) or "")
        ),
        "existing_element_sites_json": (
            request.POST.get("element_sites", "{}")
            if request.method == "POST"
            else json.dumps(getattr(existing_literature, "element_sites", None) or {})
        ),
    }
    return render(request, "catalog/add_literature_data.html", context)


# =============================================================================
# Add / edit: computational data (embedded under the Material doc)
# =============================================================================

@login_required
def add_computational_data(request):
    existing_material_auid = request.GET.get("material_auid") or request.POST.get("material_auid")
    edit_comp_auid = request.GET.get("edit_comp_auid") or request.POST.get("edit_comp_auid")

    existing_material = None
    existing_comp_record = None
    locked_elements = {}
    locked_structure = None

    if edit_comp_auid and existing_material_auid:
        existing_material = get_material(existing_material_auid)
        if existing_material is None:
            messages.error(request, "Material not found for modify operation.")
            return redirect("browse_data")
        existing_comp_record = find_embedded_dft(existing_material, edit_comp_auid)
        if existing_comp_record is None:
            messages.error(request, "Computational data not found for modify operation.")
            return redirect("browse_data")
        if not _is_uploader_or_superuser(request.user, getattr(existing_comp_record, "uploaded_by", "")):
            messages.error(request, "Only the uploader or a superuser can modify this computational data.")
            return redirect(
                "computational_detail",
                material_auid=existing_material_auid,
                comp_auid=edit_comp_auid,
            )
        locked_elements = existing_material.elements or {}
        locked_structure = existing_material.structure_family
    elif existing_material_auid:
        existing_material = get_material(existing_material_auid)
        if existing_material is not None:
            locked_elements = existing_material.elements or {}
            locked_structure = existing_material.structure_family

    if request.method == "POST":
        try:
            if existing_material is not None:
                raw_elements = existing_material.elements or {}
                structure_family = existing_material.structure_family
            else:
                elements_raw = _parse_elements_payload(request.POST.get("elements", "{}"))
                raw_elements = normalize_elements_payload(elements_raw)
                structure_family = _normalize_structure_family(
                    request.POST.get("structure_family", "rocksalt")
                )

            spacegroup = request.POST.get("spacegroup", "unknown") or "unknown"
            element_sites = _parse_element_sites(request)

            material_auid = compute_material_auid(raw_elements, structure_family)

            dft_inputs = {
                "calculation_method": request.POST.get("calc_method", ""),
                "functional": request.POST.get("functional", ""),
                "pseudopotential": request.POST.get("pseudopotential", ""),
                "k_points": request.POST.get("k_points", ""),
                "cutoff_energy": request.POST.get("cutoff_energy", ""),
                "dft_source": request.POST.get("dft_source", ""),
            }

            new_comp_auid = auid_mod.comp_auid(material_auid, dft_inputs)

            visibility = _get_visibility_affiliations_for_create(request.user)

            target_material = _upsert_material(
                material_auid=material_auid,
                elements=raw_elements,
                structure_family=structure_family,
                default_visibility_affiliations=visibility,
            )

            ml_predictions = {}
            ml_pred_raw = request.POST.get("ml_predictions", "")
            if ml_pred_raw:
                try:
                    ml_predictions = json.loads(ml_pred_raw)
                except json.JSONDecodeError:
                    ml_predictions = {}

            extended_data_raw = request.POST.get("extended_data", "")
            try:
                extended_data = json.loads(extended_data_raw) if extended_data_raw else {}
            except json.JSONDecodeError:
                extended_data = {}

            new_dft = EmbeddedDFT(
                comp_auid=new_comp_auid,
                dft_source=request.POST.get("dft_source", ""),
                dft_formation_energy_ev=_safe_float(request.POST.get("formation_energy")),
                dft_hull_distance_ev=_safe_float(request.POST.get("hull_distance")),
                dft_bandgap_ev=_safe_float(request.POST.get("bandgap")),
                bandgap_type=request.POST.get("bandgap_type") or None,
                bandgap_fit_ev=_safe_float(request.POST.get("bandgap_fit_ev")),
                bulk_modulus_vrh=_safe_float(request.POST.get("bulk_modulus_vrh")),
                shear_modulus_vrh=_safe_float(request.POST.get("shear_modulus_vrh")),
                youngs_modulus_vrh=_safe_float(request.POST.get("youngs_modulus_vrh")),
                poisson_ratio=_safe_float(request.POST.get("poisson_ratio")),
                elastic_anisotropy=_safe_float(request.POST.get("elastic_anisotropy")),
                debye_temperature=_safe_float(request.POST.get("debye_temperature")),
                thermal_conductivity_300k=_safe_float(request.POST.get("thermal_conductivity_300k")),
                gruneisen_parameter=_safe_float(request.POST.get("gruneisen_parameter")),
                thermal_expansion_300k=_safe_float(request.POST.get("thermal_expansion_300k")),
                pearson_symbol=request.POST.get("pearson_symbol") or None,
                crystal_system=request.POST.get("crystal_system") or None,
                crystal_family=request.POST.get("crystal_family") or None,
                spin_atom=_safe_float(request.POST.get("spin_atom")),
                dft_metadata={k: v for k, v in dft_inputs.items() if k != "dft_source"},
                ml_predictions=ml_predictions,
                extended_data=extended_data,
                spacegroup=spacegroup,
                element_sites=element_sites,
                uploaded_by=request.user.username if request.user.is_authenticated else "",
                visibility_affiliations=visibility,
            )

            target_material.dft_calculations = [
                d for d in (target_material.dft_calculations or [])
                if d.comp_auid != new_comp_auid
                and not (
                    existing_comp_record is not None
                    and d.comp_auid == existing_comp_record.comp_auid
                )
            ] + [new_dft]
            target_material.save()

            # Embedded DFT records don't fire MongoEngine post_save; refresh
            # the corresponding MLEmbedding here. On edits where the comp_auid
            # changed we also drop the stale embedding so it isn't orphaned.
            if existing_comp_record is not None and existing_comp_record.comp_auid != new_comp_auid:
                signals.delete_comp_embedding(existing_comp_record.comp_auid)
            signals.refresh_comp_embedding(target_material, new_dft)

            archive_upload(
                upload_type="computational",
                username=request.user.username if request.user.is_authenticated else "anonymous",
                timestamp=timezone.localtime(timezone.now()),
                metadata={
                    "type": "computational",
                    "comp_auid": new_comp_auid,
                    "material_auid": material_auid,
                    "uploaded_by": request.user.username if request.user.is_authenticated else "",
                    "dft_source": request.POST.get("dft_source", ""),
                    "calculation_method": request.POST.get("calc_method", ""),
                    "functional": request.POST.get("functional", ""),
                    "pseudopotential": request.POST.get("pseudopotential", ""),
                    "k_points": request.POST.get("k_points", ""),
                    "cutoff_energy": request.POST.get("cutoff_energy", ""),
                    "formation_energy_ev": _safe_float(request.POST.get("formation_energy")),
                    "hull_distance_ev": _safe_float(request.POST.get("hull_distance")),
                    "bandgap_ev": _safe_float(request.POST.get("bandgap")),
                    "ml_predictions": ml_predictions,
                    "elements": raw_elements,
                    "structure_family": structure_family,
                },
            )

            messages.success(request, f"Computational data added to {material_auid}")
            return redirect("composition_detail", material_auid=material_auid)

        except Exception as exc:
            messages.error(request, f"Error saving data: {exc}")

    context = {
        "structure_families": ["unknown", "rocksalt", "pyrochlore", "spinel", "perovskite", "fluorite", "other"],
        "dft_sources": ["S4E", "AFLOW", "Materials Project", "OQMD", "manual", "other"],
        "calc_methods": ["DFT", "DFT+U", "hybrid", "GW", "other"],
        "functionals": ["PBE", "PBEsol", "LDA", "HSE06", "SCAN", "other"],
        "existing_material_auid": existing_material_auid,
        "existing_comp_record": existing_comp_record,
        "edit_comp_auid": edit_comp_auid,
        "locked_elements_json": json.dumps(_elements_to_selection_list(locked_elements)),
        "locked_elements_display": _elements_to_selection_list(locked_elements),
        "locked_structure_family": locked_structure,
        "structure_family_spacegroups": json.dumps(STRUCTURE_FAMILY_SPACEGROUPS),
        "structure_family_sites": json.dumps(STRUCTURE_FAMILY_SITES),
        "existing_spacegroup": (
            request.POST.get("spacegroup", "")
            if request.method == "POST"
            else (getattr(existing_comp_record, "spacegroup", None) or "")
        ),
        "existing_element_sites_json": (
            request.POST.get("element_sites", "{}")
            if request.method == "POST"
            else json.dumps(getattr(existing_comp_record, "element_sites", None) or {})
        ),
    }
    return render(request, "catalog/add_computational_data.html", context)


# =============================================================================
# API: DOI + composition normalization
# =============================================================================

def api_docs(request):
    """Legacy docs URL — the API reference now lives on the Developer Center."""
    return redirect("developer_portal")


@never_cache
def developer_portal(request):
    """Human-facing, API-first documentation entry point for LOOP."""
    from catalog.api.code_samples import rendered_samples

    api_base_url = request.build_absolute_uri(
        reverse("api-v1-version")
    ).rsplit("version/", 1)[0]
    context = {
        "hide_sidebar": True,
        "api_base_url": api_base_url,
        "materials_url": request.build_absolute_uri(reverse("api-v1-materials")),
        "python_samples": rendered_samples(api_base_url),
    }
    return render(request, "catalog/developer_docs.html", context)


@never_cache
def developer_guide(request):
    """Long-form, human-readable API integration guide."""
    from catalog.api.code_samples import rendered_samples
    from catalog.api.examples import (
        DOCUMENTED_COMPUTATIONAL_PAYLOAD,
        DOCUMENTED_EXPERIMENT_PAYLOAD,
        DOCUMENTED_LITERATURE_PAYLOAD,
        DOCUMENTED_PRECURSOR_PAYLOAD,
        DOCUMENTED_PROTOCOL_PAYLOAD,
        DOCUMENTED_VALIDATION_PAYLOAD,
    )

    api_base_url = request.build_absolute_uri(
        reverse("api-v1-version")
    ).rsplit("version/", 1)[0]
    context = {
        "hide_sidebar": True,
        "api_base_url": api_base_url,
        "materials_url": request.build_absolute_uri(reverse("api-v1-materials")),
        "python_samples": rendered_samples(api_base_url),
        "documented_experiment_json": json.dumps(DOCUMENTED_EXPERIMENT_PAYLOAD, indent=2),
        "documented_literature_json": json.dumps(DOCUMENTED_LITERATURE_PAYLOAD, indent=2),
        "documented_computational_json": json.dumps(DOCUMENTED_COMPUTATIONAL_PAYLOAD, indent=2),
        "documented_precursor_json": json.dumps(DOCUMENTED_PRECURSOR_PAYLOAD, indent=2),
        "documented_protocol_json": json.dumps(DOCUMENTED_PROTOCOL_PAYLOAD, indent=2),
        "documented_validation_json": json.dumps(DOCUMENTED_VALIDATION_PAYLOAD, indent=2),
    }
    return render(request, "catalog/developer_guide.html", context)


@never_cache
def developer_keys(request):
    """Dedicated API-key lifecycle page within the developer documentation shell."""
    from datetime import timedelta

    from catalog.api import key_service
    from catalog.api.serializers import APIKeyCreateSerializer, API_KEY_SCOPES

    can_manage_api_keys = bool(
        request.user.is_authenticated
        and (
            request.user.is_staff
            or request.user.is_superuser
            or request.user.groups.filter(name=settings.APPROVED_GROUP_NAME).exists()
        )
    )
    if request.method == "POST" and not can_manage_api_keys:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        return redirect(settings.AWAITING_APPROVAL_URL_NAME)

    # A generated secret must survive exactly one redirected GET. Rendering it
    # directly in the POST response lets a browser refresh repeat key issuance.
    new_api_key = (
        request.session.pop("developer_keys.new_api_key", None)
        if request.method == "GET"
        else None
    )
    form_errors = None
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "revoke_key":
            if key_service.revoke(
                user=request.user, key_id=request.POST.get("key_id")
            ):
                messages.success(request, "API key revoked.")
            else:
                messages.error(request, "API key not found.")
            return redirect("developer_keys")
        if action == "create_key":
            expires_raw = (request.POST.get("expires_in_days") or "").strip()
            expires_at = None
            if expires_raw:
                try:
                    expires_at = timezone.now() + timedelta(days=int(expires_raw))
                except (TypeError, ValueError):
                    form_errors = {"expires_in_days": ["Choose a valid expiration."]}
            if form_errors is None:
                serializer = APIKeyCreateSerializer(
                    data={
                        "name": request.POST.get("name"),
                        "scopes": request.POST.getlist("scopes"),
                        "expires_at": expires_at,
                    }
                )
                if serializer.is_valid():
                    _, new_api_key = key_service.issue(
                        user=request.user, **serializer.validated_data
                    )
                    request.session["developer_keys.new_api_key"] = new_api_key
                    return redirect("developer_keys")
                else:
                    form_errors = serializer.errors

    context = {
        "hide_sidebar": True,
        "api_keys": key_service.list_for_user(request.user) if can_manage_api_keys else [],
        "api_key_scopes": API_KEY_SCOPES if can_manage_api_keys else [],
        "can_manage_api_keys": can_manage_api_keys,
        "new_api_key": new_api_key,
        "form_errors": form_errors,
    }
    return render(request, "catalog/developer_keys.html", context)


def developer_docs(request):
    """Keep old human-documentation links on the unified Developer Center."""
    return redirect("developer_portal")


def search_by_doi(request):
    doi = (request.GET.get("doi") or "").strip()
    if not doi:
        return JsonResponse({"found": False, "message": "No DOI provided"})
    try:
        doi_map = DOIMapping.objects.get(doi=doi)
        return JsonResponse({
            "found": True,
            "doi": doi,
            "material_auids": list(doi_map.material_auids or []),
            "title": doi_map.title,
        })
    except DOIMapping.DoesNotExist:
        return JsonResponse({"found": False, "doi": doi})


def fetch_doi_metadata(request):
    import urllib.error
    import urllib.request

    doi = (request.GET.get("doi") or "").strip()
    if not doi:
        return JsonResponse({"error": "No DOI provided"}, status=400)

    if doi.startswith("http"):
        doi = doi.split("doi.org/")[-1] if "doi.org/" in doi else doi

    try:
        url = f"https://api.crossref.org/works/{doi}"
        req = urllib.request.Request(url, headers={"User-Agent": "LOOP/1.0 (mailto:loop@s4e.ai)"})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())

        work = data.get("message", {})
        title = ""
        if work.get("title"):
            title = work["title"][0] if isinstance(work["title"], list) else work["title"]
        journal = ""
        container = work.get("container-title", [])
        if container:
            journal = container[0] if isinstance(container, list) else container
        year = None
        for date_field in ["published-print", "published-online", "created"]:
            if date_field in work and "date-parts" in work[date_field]:
                date_parts = work[date_field]["date-parts"]
                if date_parts and date_parts[0]:
                    year = date_parts[0][0]
                    break
        authors = []
        for author in work.get("author", []):
            name_parts = []
            if author.get("given"):
                name_parts.append(author["given"])
            if author.get("family"):
                name_parts.append(author["family"])
            if name_parts:
                authors.append(" ".join(name_parts))

        return JsonResponse({
            "success": True,
            "doi": doi,
            "title": title,
            "journal": journal,
            "year": year,
            "authors": authors,
        })
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return JsonResponse({"error": "DOI not found in CrossRef"}, status=404)
        return JsonResponse({"error": f"CrossRef API error: {exc.code}"}, status=502)
    except urllib.error.URLError as exc:
        return JsonResponse({"error": f"Network error: {exc}"}, status=502)
    except Exception as exc:
        return JsonResponse({"error": f"Error fetching DOI: {exc}"}, status=500)


def normalize_composition_api(request):
    """Return the material_auid for a composition payload."""
    try:
        if request.method == "POST":
            data = json.loads(request.body or b"{}")
            elements = data.get("elements", {})
            structure_family = data.get("structure_family", "rocksalt")
        else:
            elements = json.loads(request.GET.get("elements", "{}"))
            structure_family = request.GET.get("structure_family", "rocksalt")

        raw_elements = normalize_elements_payload(elements)
        structure_family = _normalize_structure_family(structure_family)
        material_auid = compute_material_auid(raw_elements, structure_family)

        exists = Material.objects(id=material_auid).only("id").first() is not None

        return JsonResponse({
            "material_auid": material_auid,
            "elements": raw_elements,
            "element_symbols": sorted(raw_elements.keys()),
            "structure_family": structure_family,
            "exists": exists,
            "found": exists,
        })
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=400)


# Cap on rows returned by the read APIs so a broad query can't dump the catalog.
_API_SEARCH_MAX_LIMIT = 100


def search_composition_api(request):
    """Search materials by composition.

    Given a comma-separated ``elements`` list, return every visible material
    that contains all of those elements. Optional ``structure_family`` narrows
    the search; ``limit`` caps the result count (default 25, max 100). Results
    respect the caller's affiliation visibility.
    """
    raw_elements = (request.GET.get("elements") or "").strip()
    symbols = [s.strip() for s in re.split(r"[,\s]+", raw_elements) if s.strip()]
    structure_family = (request.GET.get("structure_family") or "").strip() or None

    try:
        limit = int(request.GET.get("limit") or 25)
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(limit, _API_SEARCH_MAX_LIMIT))

    if not symbols:
        return JsonResponse(
            {"error": "Provide ?elements= as a comma-separated list, e.g. Mg,Co,Ni"},
            status=400,
        )

    user_affiliations = _user_affiliations(request.user)
    result = aggregation_mod.browse_materials(
        elements=symbols,
        structure_family=structure_family,
        user_affiliations=user_affiliations,
        skip=0,
        limit=limit,
    )

    results = [
        {
            "material_auid": row.get("material_auid"),
            "element_symbols": list(row.get("element_symbols") or []),
            "structure_family": row.get("structure_family"),
            "num_elements": row.get("num_elements"),
            "recipe_count": row.get("recipe_count", 0),
            "trial_count": row.get("trial_count", 0),
            "literature_count": row.get("literature_count", 0),
        }
        for row in result.rows
    ]

    return JsonResponse({
        "query": {"elements": symbols, "structure_family": structure_family, "limit": limit},
        "count": len(results),
        "total_matches": result.total_count,
        "results": results,
    })


def composition_recipes_api(request):
    """Return the recipes recorded for a composition.

    Given a ``material_auid``, list every recipe visible to the caller, with
    each recipe's trial and literature counts.
    """
    material_auid = (request.GET.get("material_auid") or "").strip()
    if not material_auid:
        return JsonResponse(
            {"error": "Provide ?material_auid=, e.g. M:1a2b3c4d5e6f"}, status=400
        )

    user_affiliations = _user_affiliations(request.user)
    material = Material.objects(id=material_auid).only("id").first()
    if material is None:
        return JsonResponse(
            {"material_auid": material_auid, "exists": False, "count": 0, "recipes": []},
            status=404,
        )

    recipes = []
    for recipe in Recipe.objects(material_auid=material_auid).order_by("-created_at"):
        visible_trials, visible_lits, recipe_visible = visible_recipe_children(
            recipe, user_affiliations
        )
        if not (recipe_visible or visible_trials or visible_lits):
            continue
        recipes.append({
            "recipe_auid": recipe.id,
            "trial_count": len(visible_trials),
            "literature_count": len(visible_lits),
        })

    return JsonResponse({
        "material_auid": material_auid,
        "exists": True,
        "count": len(recipes),
        "recipes": recipes,
    })


def recipe_trials_api(request):
    """Return the trial IDs recorded for a recipe.

    Given a ``recipe_id`` (the composite ``M:...:R:...`` AUID), list the IDs of
    every trial visible to the caller, each with its phase status.
    """
    recipe_id = (request.GET.get("recipe_id") or "").strip()
    if not recipe_id:
        return JsonResponse(
            {"error": "Provide ?recipe_id=, e.g. M:1a2b3c4d5e6f:R:7f8e9d0c1b2a"}, status=400
        )

    user_affiliations = _user_affiliations(request.user)
    recipe = Recipe.objects(id=recipe_id).first()
    if recipe is None:
        return JsonResponse(
            {"recipe_id": recipe_id, "exists": False, "count": 0, "trial_ids": []},
            status=404,
        )

    trials = [
        {"trial_id": t.trial_id, "phase_status": getattr(t, "phase_status", None)}
        for t in (recipe.trials or [])
        if _is_visible_to_user(getattr(t, "visibility_affiliations", None), user_affiliations)
    ]

    return JsonResponse({
        "recipe_id": recipe_id,
        "exists": True,
        "count": len(trials),
        "trial_ids": [t["trial_id"] for t in trials],
        "trials": trials,
    })


def composition_download(request, material_auid):
    """Download a whole composition directory as a ``.zip`` (metadata + data files)."""
    if not auid_mod.is_material_auid(material_auid):
        return JsonResponse({"error": "not found"}, status=404)
    result = api_download.build_composition_zip(
        material_auid, _user_affiliations(request.user)
    )
    return _zip_response_or_404(result)


def recipe_download(request, recipe_id):
    """Download a whole recipe directory as a ``.zip`` (metadata + trial data files)."""
    if not auid_mod.is_recipe_id(recipe_id):
        return JsonResponse({"error": "not found"}, status=404)
    result = api_download.build_recipe_zip(
        recipe_id, _user_affiliations(request.user)
    )
    return _zip_response_or_404(result)


def trial_download(request, recipe_id, trial_id):
    """Download a single trial directory as a ``.zip`` (metadata + raw/derived files)."""
    if not auid_mod.is_recipe_id(recipe_id):
        return JsonResponse({"error": "not found"}, status=404)
    result = api_download.build_trial_zip(
        recipe_id, trial_id, _user_affiliations(request.user)
    )
    return _zip_response_or_404(result)


def _zip_response_or_404(result):
    if result is None:
        return JsonResponse({"error": "not found"}, status=404)
    zip_file, filename = result
    return FileResponse(
        zip_file, as_attachment=True, filename=filename, content_type="application/zip"
    )


# =============================================================================
# Per-user precursor library
# =============================================================================

_CAS_RE = re.compile(r"^\s*(\d{2,7})-(\d{2})-(\d)\s*$")


def _normalize_cas(raw: str) -> str:
    if not raw:
        return ""
    match = _CAS_RE.match(raw)
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _visible_precursors_qs(user):
    """Precursors visible to ``user``: shared with one of their affiliations,
    or uploaded by them. Superusers see all.
    """
    if getattr(user, "is_superuser", False):
        return UserPrecursor.objects.all()
    affiliations = _user_affiliations(user)
    return UserPrecursor.objects(
        __raw__={
            "$or": [
                {"user_id": user.id},
                {"visibility_affiliations": {"$in": affiliations}},
            ]
        }
    )


def _get_visible_precursor(precursor_id: str, user):
    try:
        return _visible_precursors_qs(user).filter(id=precursor_id).first()
    except Exception:
        return None


def _user_can_edit_precursor(user, precursor: UserPrecursor) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return precursor.user_id == user.id


def _find_precursor_collision(user, cas_norm: str, exclude_id: Optional[str] = None):
    """Return an existing precursor visible to ``user`` with the same CAS, if any."""
    if not cas_norm:
        return None
    qs = _visible_precursors_qs(user).filter(cas_number=cas_norm)
    if exclude_id:
        qs = qs.filter(id__ne=exclude_id)
    return qs.first()


def _collision_response(existing: UserPrecursor, viewer_user_id: int):
    existing_dict = existing.to_public_dict(viewer_user_id=viewer_user_id)
    label = existing.name or "(unnamed)"
    if existing.uploaded_by_username:
        label += f" (by {existing.uploaded_by_username})"
    return JsonResponse(
        {
            "error": "duplicate_cas",
            "message": (
                f"A precursor with CAS {existing.cas_number} already exists in "
                f"your library or your affiliation's shared library: {label}. "
                "Save anyway?"
            ),
            "existing": existing_dict,
        },
        status=409,
    )


@login_required
@require_POST
def precursors_create(request):
    """Create a new saved precursor, shared with the uploader's affiliations."""
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "Precursor name is required"}, status=400)

    cas_raw = (data.get("cas_number") or "").strip()
    cas_norm = _normalize_cas(cas_raw) if cas_raw else ""
    if cas_raw and not cas_norm:
        return JsonResponse({"error": "CAS number must look like 7440-50-8"}, status=400)

    force = bool(data.get("force"))
    if cas_norm and not force:
        existing = _find_precursor_collision(request.user, cas_norm)
        if existing is not None:
            return _collision_response(existing, request.user.id)

    affiliations = _get_visibility_affiliations_for_create(request.user)
    precursor = UserPrecursor(
        user_id=request.user.id,
        uploaded_by_username=str(request.user.username or ""),
        visibility_affiliations=affiliations,
        name=name,
        formula=(data.get("formula") or "").strip() or None,
        cas_number=cas_norm or None,
        purity=(data.get("purity") or "").strip() or None,
        supplier=(data.get("supplier") or "").strip() or None,
        notes=(data.get("notes") or "").strip() or None,
    )
    precursor.save()
    return JsonResponse({"precursor": precursor.to_public_dict(viewer_user_id=request.user.id)})


@login_required
def precursors_list(request):
    """Return all precursors visible to the current user."""
    if request.method != "GET":
        return JsonResponse({"error": "GET only"}, status=405)
    qs = _visible_precursors_qs(request.user).order_by("name")
    return JsonResponse(
        {"precursors": [p.to_public_dict(viewer_user_id=request.user.id) for p in qs]}
    )


@login_required
@require_POST
def precursors_update(request, precursor_id):
    precursor = _get_visible_precursor(precursor_id, request.user)
    if precursor is None:
        return JsonResponse({"error": "Precursor not found"}, status=404)
    if not _user_can_edit_precursor(request.user, precursor):
        return JsonResponse({"error": "You can only edit precursors you uploaded."}, status=403)
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    force = bool(data.get("force"))

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return JsonResponse({"error": "Precursor name is required"}, status=400)
        precursor.name = name
    if "formula" in data:
        precursor.formula = (data.get("formula") or "").strip() or None
    if "cas_number" in data:
        cas_raw = (data.get("cas_number") or "").strip()
        cas_norm = _normalize_cas(cas_raw) if cas_raw else ""
        if cas_raw and not cas_norm:
            return JsonResponse({"error": "CAS number must look like 7440-50-8"}, status=400)
        if cas_norm and cas_norm != (precursor.cas_number or "") and not force:
            existing = _find_precursor_collision(
                request.user, cas_norm, exclude_id=str(precursor.id)
            )
            if existing is not None:
                return _collision_response(existing, request.user.id)
        precursor.cas_number = cas_norm or None
    if "purity" in data:
        precursor.purity = (data.get("purity") or "").strip() or None
    if "supplier" in data:
        precursor.supplier = (data.get("supplier") or "").strip() or None
    if "notes" in data:
        precursor.notes = (data.get("notes") or "").strip() or None

    precursor.save()
    return JsonResponse({"precursor": precursor.to_public_dict(viewer_user_id=request.user.id)})


@login_required
@require_POST
def precursors_delete(request, precursor_id):
    precursor = _get_visible_precursor(precursor_id, request.user)
    if precursor is None:
        return JsonResponse({"error": "Precursor not found"}, status=404)
    if not _user_can_edit_precursor(request.user, precursor):
        return JsonResponse({"error": "You can only delete precursors you uploaded."}, status=403)
    precursor.delete()
    return JsonResponse({"success": True, "id": precursor_id})


_PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
_PUBCHEM_UA = {"User-Agent": "LOOP/1.0 (mailto:loop@s4e.ai)"}


def _pubchem_get_json(url: str, timeout: int = 8):
    """Return parsed JSON from a PubChem PUG REST URL, or ``None`` on 404."""
    import urllib.request
    import urllib.error

    req = urllib.request.Request(url, headers=_PUBCHEM_UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _pick_pubchem_property(props):
    """Pick the most "compound-like" property row out of a multi-hit result.

    PubChem's xref/RN endpoint often returns several rows for a single CAS
    (e.g. the anion and the neutral acid for HCl). We'd rather land on the
    neutral molecule whose formula actually contains hydrogen (covers acids,
    hydrates, most salts). Fall back to the first row if no row qualifies.
    """
    if not props:
        return None
    # Prefer rows with a formula that isn't a bare ion (contains no '+' or '-')
    # and that has at least one hydrogen — that filters "Cl-" / "Na+" out.
    def _score(row):
        formula = (row.get("MolecularFormula") or "").strip()
        if not formula:
            return (0, 0)
        is_ion = "+" in formula or "-" in formula
        has_h = "H" in formula
        return (0 if is_ion else 2, 1 if has_h else 0)

    best = max(props, key=_score)
    # If even the "best" row is a pure ion, keep it (better than nothing).
    return best


def precursors_cas_lookup(request):
    """Proxy a CAS number to PubChem and return ``{name, formula, cas_number}``.

    Uses PubChem's public PUG REST API. Unauthenticated, ~5 req/s per IP.
    Tries the ``xref/RN`` endpoint first, then falls back to ``name`` (which
    resolves CAS as a synonym) so CAS numbers PubChem only indexes under
    ``name`` still hydrate. Returns ``{"found": False}`` when PubChem has
    no record.
    """
    cas = _normalize_cas(request.GET.get("cas") or "")
    if not cas:
        return JsonResponse({"error": "Missing or malformed ?cas= (expect e.g. 7440-50-8)"}, status=400)

    prop_path = "property/MolecularFormula,Title/JSON"
    endpoints = [
        f"{_PUBCHEM_BASE}/compound/xref/RN/{cas}/{prop_path}",
        f"{_PUBCHEM_BASE}/compound/name/{cas}/{prop_path}",
    ]

    best = None
    for url in endpoints:
        try:
            payload = _pubchem_get_json(url)
        except Exception as exc:
            return JsonResponse({"error": f"PubChem lookup failed: {exc}"}, status=502)
        if payload is None:
            continue
        props = (
            payload.get("PropertyTable", {}).get("Properties", [])
            if isinstance(payload, dict)
            else []
        )
        best = _pick_pubchem_property(props)
        if best:
            break

    if not best:
        return JsonResponse({"found": False, "cas_number": cas})

    name = (best.get("Title") or best.get("IUPACName") or "").strip()
    formula = (best.get("MolecularFormula") or "").strip()
    return JsonResponse({
        "found": True,
        "cas_number": cas,
        "name": name,
        "formula": formula,
        "cid": best.get("CID"),
    })


@login_required
def precursors_manage_page(request):
    """Render the CRUD page for saved precursors visible to the user."""
    qs = _visible_precursors_qs(request.user).order_by("name")
    user_affiliations = _user_affiliations(request.user)
    return render(
        request,
        "catalog/precursors.html",
        {
            "precursors": [p.to_public_dict(viewer_user_id=request.user.id) for p in qs],
            "user_affiliations": user_affiliations,
        },
    )


# Per-user protocol library
# =============================================================================


def _visible_protocols_qs(user):
    """Protocols visible to ``user``: uploaded by them or shared via affiliation.
    Superusers see all.
    """
    if getattr(user, "is_superuser", False):
        return UserProtocol.objects.all()
    affiliations = _user_affiliations(user)
    return UserProtocol.objects(
        __raw__={
            "$or": [
                {"user_id": user.id},
                {"visibility_affiliations": {"$in": affiliations}},
            ]
        }
    )


def _get_visible_protocol(protocol_id: str, user):
    try:
        return _visible_protocols_qs(user).filter(id=protocol_id).first()
    except Exception:
        return None


def _user_can_edit_protocol(user, protocol: UserProtocol) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return protocol.user_id == user.id


@login_required
def protocols_list(request):
    """Return all protocols visible to the current user."""
    if request.method != "GET":
        return JsonResponse({"error": "GET only"}, status=405)
    qs = _visible_protocols_qs(request.user).order_by("name")
    return JsonResponse(
        {"protocols": [p.to_public_dict(viewer_user_id=request.user.id) for p in qs]}
    )


@login_required
@require_POST
def protocols_create(request):
    """Create a new saved protocol, shared with the uploader's affiliations."""
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    name = (data.get("name") or "").strip()
    if not name:
        return JsonResponse({"error": "Protocol name is required"}, status=400)

    steps = data.get("steps")
    if not isinstance(steps, list):
        return JsonResponse({"error": "'steps' must be a list"}, status=400)

    affiliations = _get_visibility_affiliations_for_create(request.user)
    protocol = UserProtocol(
        user_id=request.user.id,
        uploaded_by_username=str(request.user.username or ""),
        visibility_affiliations=affiliations,
        name=name,
        description=(data.get("description") or "").strip() or None,
        steps=steps,
    )
    protocol.save()
    return JsonResponse({"protocol": protocol.to_public_dict(viewer_user_id=request.user.id)})


@login_required
@require_POST
def protocols_update(request, protocol_id):
    protocol = _get_visible_protocol(protocol_id, request.user)
    if protocol is None:
        return JsonResponse({"error": "Protocol not found"}, status=404)
    if not _user_can_edit_protocol(request.user, protocol):
        return JsonResponse({"error": "You can only edit protocols you uploaded."}, status=403)
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        return JsonResponse({"error": f"Invalid JSON: {exc}"}, status=400)

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return JsonResponse({"error": "Protocol name is required"}, status=400)
        protocol.name = name
    if "description" in data:
        protocol.description = (data.get("description") or "").strip() or None
    if "steps" in data:
        steps = data.get("steps")
        if not isinstance(steps, list):
            return JsonResponse({"error": "'steps' must be a list"}, status=400)
        protocol.steps = steps

    protocol.save()
    return JsonResponse({"protocol": protocol.to_public_dict(viewer_user_id=request.user.id)})


@login_required
@require_POST
def protocols_delete(request, protocol_id):
    protocol = _get_visible_protocol(protocol_id, request.user)
    if protocol is None:
        return JsonResponse({"error": "Protocol not found"}, status=404)
    if not _user_can_edit_protocol(request.user, protocol):
        return JsonResponse({"error": "You can only delete protocols you uploaded."}, status=403)
    protocol.delete()
    return JsonResponse({"success": True, "id": protocol_id})


@login_required
def protocols_manage_page(request):
    """Render the CRUD page for saved protocols visible to the user."""
    qs = _visible_protocols_qs(request.user).order_by("name")
    user_affiliations = _user_affiliations(request.user)
    protocols = [p.to_public_dict(viewer_user_id=request.user.id) for p in qs]
    return render(
        request,
        "catalog/protocols.html",
        {
            "protocols": protocols,
            "protocols_json": json.dumps(protocols),
            "user_affiliations": user_affiliations,
        },
    )


def custom_bad_request(request, exception=None):
    return render(request, "400.html", {"message": "The submitted request is invalid."}, status=400)


def custom_page_not_found(request, exception=None, template_name="404.html"):
    """Answer a 404 in the content type the caller is speaking.

    Only the resolver's own 404 reaches here; a view under ``/api/v1/`` that
    reports a missing record already renders problem+json through DRF. Without
    this split an unrouted API path falls through to the HTML page and a JSON
    client sees a parse error rather than "no such endpoint".
    """
    # Imported here because catalog.api.views imports this module at load time.
    from catalog.api.exceptions import api_not_found, is_api_v1_path

    if is_api_v1_path(request.path_info):
        return api_not_found(request)
    return django_page_not_found(request, exception, template_name=template_name)


def custom_server_error(request, template_name="500.html"):
    """Answer a 500 in the content type the caller is speaking.

    The same split as ``custom_page_not_found``, for the status DRF cannot
    handle: its exception hook only converts ``APIException`` subclasses, so an
    ordinary bug in a view escapes to Django and rendered the HTML error page
    under ``/api/v1/``. Without this an API client's failure path has to parse
    HTML precisely when something is already wrong.
    """
    # Imported here because catalog.api.views imports this module at load time.
    from catalog.api.exceptions import api_server_error, is_api_v1_path

    if is_api_v1_path(request.path_info):
        return api_server_error(request)
    return django_server_error(request, template_name=template_name)


@login_required
def _render_batch_exp(request, form, preview=None, *, zip_only=False,
                      ready=False, can_download_template=False):
    return render(request, "catalog/batch_upload_exp_data.html", {
        "form": form,
        "preview": preview,
        "zip_only": zip_only,
        "ready_to_confirm": ready,
        "can_download_template": can_download_template,
    })


def batch_upload_exp_data(request):
    """One pipeline for all three upload modes: validate inputs, build the
    preview, persist files, stash the confirm payload in the session."""
    if request.method != "POST":
        return _render_batch_exp(request, BatchExperimentalUploadForm())

    form = BatchExperimentalUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        return _render_batch_exp(request, form)

    mode = form.cleaned_data["upload_mode"]
    manifest = request.FILES.get("manifest")
    archive = request.FILES.get("archive")
    zip_only = mode == BatchExperimentalUploadForm.MODE_ZIP_ONLY
    existing_zip = mode == BatchExperimentalUploadForm.MODE_WITH_EXISTING_ZIP
    payload = request.session.get("batch_experiment_upload") or {}

    def fail(message):
        messages.error(request, message)
        return redirect("batch_upload_exp_data")

    if existing_zip and not payload.get("archive_path"):
        return fail("No XRD ZIP is available. Upload the XRD ZIP first.")
    if existing_zip and manifest is None:
        return fail("Upload the completed manifest CSV.")
    if mode == BatchExperimentalUploadForm.MODE_WITH_MANIFEST and manifest is None:
        return fail("Upload a manifest CSV.")
    if not existing_zip and archive is None:
        return fail("Upload an XRD folder ZIP.")

    try:
        if existing_zip:
            manifest.seek(0)
            with default_storage.open(payload["archive_path"], "rb") as archive_file:
                preview = create_preview(
                    manifest_file=manifest, archive_file=archive_file, zip_only=False,
                )
        else:
            preview = create_preview(
                manifest_file=None if zip_only else manifest,
                archive_file=archive,
                zip_only=zip_only,
            )
    except ValueError as exc:
        return fail(f"Could not read manifest: {exc}")

    if manifest is not None:
        manifest.seek(0)
    if existing_zip:
        # Keep the stored ZIP; from here on the upload behaves like mode 1.
        payload["upload_mode"] = BatchExperimentalUploadForm.MODE_WITH_MANIFEST
        payload["manifest_path"] = persist_uploaded_file(manifest, "batch_upload_manifests")
        payload["structure_family"] = form.cleaned_data["structure_family"]
        request.session["batch_experiment_upload"] = payload
        return _render_batch_exp(request, form, preview, ready=preview.error_count == 0)

    archive.seek(0)
    request.session["batch_experiment_upload"] = {
        "upload_mode": mode,
        "manifest_path": (
            persist_uploaded_file(manifest, "batch_upload_manifests") if manifest else None
        ),
        "archive_path": persist_uploaded_file(archive, "batch_upload_archives"),
        "structure_family": form.cleaned_data["structure_family"],
    }
    return _render_batch_exp(
        request, form, preview,
        zip_only=zip_only,
        ready=not zip_only and preview.error_count == 0,
        can_download_template=zip_only,
    )


@login_required
def confirm_batch_upload_exp_data(request):
    if request.method != "POST":
        return redirect("batch_upload_exp_data")

    payload = request.session.get("batch_experiment_upload")
    if not payload:
        messages.error(request, "No batch upload is ready to confirm.")
        return redirect("batch_upload_exp_data")

    if payload.get("upload_mode") == BatchExperimentalUploadForm.MODE_ZIP_ONLY:
        messages.error(
            request,
            "ZIP-only mode cannot be imported directly. Download the manifest template, fill it in, then upload CSV + ZIP.",
        )
        return redirect("batch_upload_exp_data")

    if not payload.get("manifest_path"):
        messages.error(request, "Missing manifest file for batch import.")
        return redirect("batch_upload_exp_data")

    result = commit_batch(
        manifest_path=payload["manifest_path"],
        archive_path=payload["archive_path"],
        user=request.user,
        request=request,
        structure_family=payload["structure_family"],
    )

    request.session.pop("batch_experiment_upload", None)

    messages.success(
        request,
        (
            "Batch import complete. "
            f"Materials: {result['created_materials']}; "
            f"Recipes: {result['created_recipes']}; "
            f"Trials: {result['created_trials']}; "
            f"Files: {result['recorded_files']}; "
            f"Skipped: {result['skipped']}."
        ),
    )
    for detail in result.get("skipped_details", []):
        messages.warning(request, f"Skipped {detail}")

    return redirect("browse_data")


@login_required
def download_batch_manifest_template(request):
    payload = request.session.get("batch_experiment_upload")

    if not payload or not payload.get("archive_path"):
        messages.error(request, "Upload an XRD ZIP first to generate a manifest template.")
        return redirect("batch_upload_exp_data")

    with default_storage.open(payload["archive_path"], "rb") as archive_file:
        preview = create_preview(
            manifest_file=None,
            archive_file=archive_file,
            zip_only=True,
        )

    csv_text = generate_manifest_template_csv(preview)

    response = HttpResponse(csv_text, content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="batch_experiment_manifest_template.csv"'
    return response


def batch_landing(request):
    """Data-type selector reached from the Add Data -> Batch modal choice."""
    return render(request, "catalog/batch_landing.html")


@login_required
def batch_upload_lit_data(request):
    if request.method == "POST":
        form = BatchLiteratureUploadForm(request.POST, request.FILES)

        if form.is_valid():
            manifest = request.FILES["manifest"]

            try:
                preview = batch_literature_upload.create_preview(
                    manifest_file=manifest, filename=manifest.name
                )
            except ValueError as exc:
                messages.error(request, f"Could not read manifest: {exc}")
                return render(
                    request,
                    "catalog/batch_upload_lit_data.html",
                    {"form": form, "preview": None, "ready_to_confirm": False},
                )

            manifest.seek(0)
            manifest_path = persist_uploaded_file(manifest, "batch_upload_lit_manifests")

            request.session["batch_literature_upload"] = {
                "manifest_path": manifest_path,
                "structure_family": form.cleaned_data["structure_family"],
            }

            return render(
                request,
                "catalog/batch_upload_lit_data.html",
                {
                    "form": form,
                    "preview": preview,
                    "ready_to_confirm": preview.error_count == 0 and preview.total_rows > 0,
                },
            )
    else:
        form = BatchLiteratureUploadForm()

    return render(
        request,
        "catalog/batch_upload_lit_data.html",
        {"form": form, "preview": None, "ready_to_confirm": False},
    )


@login_required
def confirm_batch_upload_lit_data(request):
    if request.method != "POST":
        return redirect("batch_upload_lit_data")

    payload = request.session.get("batch_literature_upload")
    if not payload or not payload.get("manifest_path"):
        messages.error(request, "No literature batch is ready to confirm.")
        return redirect("batch_upload_lit_data")

    result = batch_literature_upload.commit_batch_literature(
        manifest_path=payload["manifest_path"],
        user=request.user,
        structure_family=payload["structure_family"],
    )

    request.session.pop("batch_literature_upload", None)

    messages.success(
        request,
        (
            "Literature batch import complete. "
            f"Records: {result['created_records']}; "
            f"Skipped: {result['skipped']}."
        ),
    )
    for detail in result.get("skipped_details", []):
        messages.warning(request, f"Skipped {detail}")

    return redirect("browse_data")


@login_required
def download_batch_lit_manifest_template(request):
    csv_text = batch_literature_upload.generate_manifest_template_csv()
    response = HttpResponse(csv_text, content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="batch_literature_manifest_template.csv"'
    return response
