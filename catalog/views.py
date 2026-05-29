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

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMessage, EmailMultiAlternatives
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, TemplateView

from . import aggregation as aggregation_mod
from . import auid as auid_mod
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
from .gsas_tools import peak_finder, peak_finder_fast
from .raw_db import record_raw_file
from .upload_archive import archive_upload
from .utils import render_xrd_plot, xrd_parse


# =============================================================================
# Generic helpers
# =============================================================================

AFFILIATION_CANONICAL = {
    "s4e": "S4E",
    "apl": "APL",
    "oak ridge": "Oak Ridge",
    "oakridge": "Oak Ridge",
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

def _canonical_affiliation(value):
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in AFFILIATION_CANONICAL:
        return AFFILIATION_CANONICAL[lowered]
    normalized = " ".join(lowered.replace("-", " ").replace("_", " ").split())
    if normalized in AFFILIATION_CANONICAL:
        return AFFILIATION_CANONICAL[normalized]
    for allowed in AFFILIATION_VALUES:
        if lowered == allowed.lower():
            return allowed
    return None


def _normalize_visibility_tags(tags):
    if isinstance(tags, str):
        raw = tags.strip()
        if not raw:
            tags = []
        else:
            parsed = None
            if raw.startswith("[") and raw.endswith("]"):
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = None
            if isinstance(parsed, list):
                tags = parsed
            elif "," in raw:
                tags = [part.strip() for part in raw.split(",")]
            else:
                tags = [raw]
    normalized = []
    for tag in (tags or list(VISIBILITY_DEFAULT)):
        canonical = _canonical_affiliation(tag)
        if canonical and canonical not in normalized:
            normalized.append(canonical)
    return normalized


def _user_affiliations(user):
    return _normalize_visibility_tags(get_user_affiliations(user) or list(VISIBILITY_DEFAULT))


def _get_visibility_affiliations_for_create(user):
    return _normalize_visibility_tags(_user_affiliations(user) or list(VISIBILITY_DEFAULT))


def _is_visible_to_user(item_visibility, user_affiliations):
    visibility = _normalize_visibility_tags(item_visibility or list(VISIBILITY_DEFAULT))
    user_affiliations = _normalize_visibility_tags(user_affiliations or list(VISIBILITY_DEFAULT))
    if "S4E" in user_affiliations:
        return True
    if "S4E" in visibility:
        visibility = [aff for aff in visibility if aff != "S4E"]
    if not visibility:
        return False
    return any(aff in visibility for aff in user_affiliations)


def _is_uploader_or_superuser(user, uploader):
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return bool(uploader) and str(user.username).strip().lower() == str(uploader).strip().lower()


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


def _next_trial_id(material_auid, date_prefix):
    """Generate a unique trial_id within a material's recipes for ``date_prefix_N``."""
    taken = set()
    for recipe in Recipe.objects(material_auid=material_auid).only("trials"):
        for trial in recipe.trials or []:
            tid = trial.trial_id or ""
            if tid.startswith(f"{date_prefix}_"):
                taken.add(tid)
    suffix = 1
    trial_id = f"{date_prefix}_{suffix}"
    while trial_id in taken:
        suffix += 1
        trial_id = f"{date_prefix}_{suffix}"
    return trial_id


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

def _recipe_visible_for_browse(recipe, user_affiliations):
    if _is_visible_to_user(getattr(recipe, "visibility_affiliations", None), user_affiliations):
        return True
    for trial in recipe.trials or []:
        if _is_visible_to_user(getattr(trial, "visibility_affiliations", None), user_affiliations):
            return True
    for lit in recipe.literature or []:
        if _is_visible_to_user(getattr(lit, "visibility_affiliations", None), user_affiliations):
            return True
    return False


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
        if rows:
            m_auids = [r["material_auid"] for r in rows]
            for recipe in Recipe.objects(material_auid__in=m_auids).only("material_auid", "trials"):
                recipes_by_m.setdefault(recipe.material_auid, []).append(recipe)

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
                "has_comp": bool(row.get("has_computational")),
                "organizations": trial_orgs,
                "temperature_display": _format_temperature_display(trial_temps),
                "display_elements": _display_elements(row.get("elements") or {}),
                "composition_compact": _format_composition_compact(
                    _display_elements(row.get("elements") or {})
                ),
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
        "structure_families": ["rocksalt", "pyrochlore", "spinel", "perovskite", "fluorite", "other"],
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
        })

    display_elements = _display_elements(view.get("elements") or {})
    num_elements = view.get("num_elements") or 0
    recipe_groups_enriched = _enrich_recipe_groups_for_composition(view.get("recipe_groups"))

    can_modify_annotation = bool(getattr(request.user, "is_authenticated", False))
    can_delete_material = bool(getattr(request.user, "is_superuser", False))

    context = {
        "view": view,
        "material_auid": material_auid,
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
        row = dict(group)
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

    trials = []
    for trial in recipe.trials or []:
        if not _is_visible_to_user(trial.visibility_affiliations, user_affiliations):
            continue
        trials.append(trial)

    literature = []
    for lit in recipe.literature or []:
        if not _is_visible_to_user(lit.visibility_affiliations, user_affiliations):
            continue
        literature.append(lit)

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
        "can_modify_computational": _is_uploader_or_superuser(request.user, getattr(record, "uploaded_by", "")),
        "can_delete_computational": _is_uploader_or_superuser(request.user, getattr(record, "uploaded_by", "")),
    }
    return render(request, "catalog/computational_detail.html", context)


def trial_detail(request, recipe_id, trial_id):
    if not auid_mod.is_recipe_id(recipe_id):
        raise Http404(f"Recipe {recipe_id} not found.")

    recipe = get_recipe(recipe_id)
    if recipe is None:
        return render(request, "404.html", {"message": f"Recipe {recipe_id} not found."}, status=404)

    record = find_embedded_trial(recipe, trial_id)
    if record is None:
        return render(request, "404.html", {"message": f"Trial {trial_id} not found."}, status=404)

    user_affiliations = _user_affiliations(request.user)
    if not _is_visible_to_user(record.visibility_affiliations, user_affiliations):
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
    plot_b64 = None
    detected_peaks = []
    plot_notice = None
    raw_link = getattr(record, "raw_data_link", "") or ""
    local_csv_path = None
    candidate_paths = []
    if raw_link:
        parsed_path = urlparse(raw_link).path
        media_marker = "/media/"
        if media_marker in parsed_path:
            rel_part = parsed_path.split(media_marker, 1)[1].replace("/", os.sep)
            candidate_paths.append(os.path.join(settings.MEDIA_ROOT, rel_part))
    candidate_paths.append(
        os.path.join(settings.MEDIA_ROOT, "xrd_data", recipe.material_auid, f"{record.trial_id}.csv")
    )
    for candidate in candidate_paths:
        if candidate and os.path.exists(candidate):
            local_csv_path = candidate
            break

    has_xrd_csv = bool(local_csv_path)
    refine_gsas_requested = request.GET.get("refine_gsas", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    use_full_gsas = refine_gsas_requested or os.environ.get(
        "LOOP_GSAS_FULL_SYNC", ""
    ).strip().lower() in ("1", "true", "yes")
    full_gsas_applied = False

    if local_csv_path:
        try:
            metadata, df = xrd_parse(local_csv_path)
            try:
                if use_full_gsas:
                    detected_peaks, _, plot_data_uri = peak_finder(df)
                    full_gsas_applied = True
                else:
                    detected_peaks, _, plot_data_uri = peak_finder_fast(df)
                if detected_peaks:
                    plot_notice = None
                else:
                    plot_notice = "No peaks were detected."
            except Exception:
                detected_peaks = []
                plot_data_uri = render_xrd_plot(df, encode_base64=True)
                plot_notice = "Peak detection overlay is unavailable for this trial."
            plot_b64 = plot_data_uri.replace("data:image/png;base64,", "")
            for key, value in metadata:
                key_str = str(key).strip()
                value_str = str(value).strip()
                if not key_str and not value_str:
                    continue
                pretty_key = key_str.replace("_", " ").replace("-", " ").strip().title() or "Field"
                metadata_rows.append((pretty_key, value_str))
        except Exception:
            metadata_rows = []
            plot_b64 = None
            detected_peaks = []
            plot_notice = None

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

    context = {
        "material_auid": recipe.material_auid,
        "recipe_id": recipe.id,
        "trial_id": trial_id,
        "record": record,
        "elements": _display_elements(recipe.elements or {}),
        "structure_family": recipe.structure_family,
        "metadata_rows": metadata_rows,
        "plot_b64": plot_b64,
        "detected_peaks": detected_peaks,
        "plot_notice": plot_notice,
        "synthesis_steps_display": synthesis_steps_display,
        "additional_notes": additional_notes,
        "can_modify_trial": _is_uploader_or_superuser(request.user, getattr(record, "experimenter", "")),
        "can_delete_trial": _is_uploader_or_superuser(request.user, getattr(record, "experimenter", "")),
        "has_xrd_csv": has_xrd_csv,
        "full_gsas_applied": full_gsas_applied,
    }
    return render(request, "catalog/trial_detail.html", context)


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
        }
        post_synthesis_steps = _parse_synthesis_steps_from_request(request)
        handled_validation_error = False
        try:
            csv_file = request.FILES.get("csv_file")
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
                    messages.error(
                        request,
                        (
                            "Duplicate raw data file detected by hash. "
                            f"This file already exists in {dup_material} "
                            f"(trial {dup_trial_id})."
                        ),
                    )
                    handled_validation_error = True
                    raise ValueError("duplicate_csv_file")

            if edit_recipe is not None:
                raw_elements = edit_recipe.elements or {}
                structure_family = edit_recipe.structure_family
            else:
                elements_raw = _parse_elements_payload(request.POST.get("elements", "{}"))
                raw_elements = normalize_elements_payload(elements_raw)
                structure_family = _normalize_structure_family(
                    request.POST.get("structure_family", "rocksalt")
                )

            material_auid = compute_material_auid(raw_elements, structure_family)

            synthesis_steps = post_synthesis_steps
            recipe_auid = compute_recipe_auid(material_auid, synthesis_steps)

            if edit_trial_record is not None:
                trial_id = edit_trial_record.trial_id
                trial_date = edit_trial_record.trial_date or timezone.now()
            else:
                trial_date = timezone.now()
                local_trial_date = timezone.localtime(trial_date)
                date_prefix = local_trial_date.strftime("%m_%d_%Y")
                trial_id = _next_trial_id(material_auid, date_prefix)

            additional_params = {"synthesis_steps": synthesis_steps}
            raw_data_link = None

            if has_csv:
                additional_params["file_hash"] = csv_sha256
                xrd_data_dir = os.path.join(settings.MEDIA_ROOT, "xrd_data", material_auid)
                os.makedirs(xrd_data_dir, exist_ok=True)
                csv_filename = f"{trial_id}.csv"
                csv_path = os.path.join(xrd_data_dir, csv_filename)
                with open(csv_path, "wb+") as destination:
                    for chunk in csv_file.chunks():
                        destination.write(chunk)
                media_rel_path = f"{settings.MEDIA_URL}xrd_data/{material_auid}/{csv_filename}"
                raw_data_link = request.build_absolute_uri(media_rel_path)
                csv_file.seek(0)
                try:
                    metadata, df = xrd_parse(csv_file)
                    plot_image = render_xrd_plot(df, encode_base64=True)
                    additional_params["xrd_metadata"] = metadata
                except Exception as exc:
                    messages.warning(request, f"Could not parse CSV for plotting: {exc}")
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

            visibility = _get_visibility_affiliations_for_create(request.user)

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

            phase_status_raw = request.POST.get("phase_status", "not_confirmed")
            if phase_status_raw not in ("single_phase", "multi_phase", "not_confirmed"):
                phase_status_raw = "not_confirmed"
            success_value = {
                "single_phase": True,
                "multi_phase": False,
                "not_confirmed": None,
            }[phase_status_raw]

            new_trial = EmbeddedTrial(
                trial_id=trial_id,
                trial_date=trial_date,
                phase_status=phase_status_raw,
                success=success_value,
                exp_condition=exp_condition,
                raw_data_type=request.POST.get("raw_data_type", "xrd"),
                file_hash=additional_params.get("file_hash"),
                experimenter=request.user.username if request.user.is_authenticated else "",
                notes=request.POST.get("comments", "") or request.POST.get("notes", ""),
                visibility_affiliations=visibility,
            )
            if raw_data_link:
                new_trial.raw_data_link = raw_data_link

            # Upsert the trial by trial_id inside the target recipe.
            target_recipe.trials = [
                t for t in (target_recipe.trials or []) if t.trial_id != trial_id
            ] + [new_trial]
            target_recipe.save()

            # Idempotent Raw DB entry for the uploaded file.
            if additional_params.get("file_hash"):
                stored_path = None
                if has_csv:
                    stored_path = f"xrd_data/{material_auid}/{trial_id}.csv"
                record_raw_file(
                    file_hash=additional_params["file_hash"],
                    material_auid=material_auid,
                    recipe_auid=recipe_auid,
                    trial_id=trial_id,
                    original_filename=getattr(csv_file, "name", None) if has_csv else None,
                    stored_path=stored_path,
                    content_type=getattr(csv_file, "content_type", None) if has_csv else None,
                    size_bytes=getattr(csv_file, "size", None) if has_csv else None,
                    uploaded_by=request.user.username if request.user.is_authenticated else None,
                    elements=raw_elements,
                    structure_family=structure_family,
                )

            archive_upload(
                upload_type="trial",
                username=request.user.username if request.user.is_authenticated else "anonymous",
                timestamp=timezone.localtime(trial_date),
                metadata={
                    "type": "trial",
                    "trial_id": trial_id,
                    "material_auid": material_auid,
                    "recipe_auid": recipe_auid,
                    "experimenter": request.user.username if request.user.is_authenticated else "",
                    "trial_date": trial_date.isoformat(),
                    "phase_status": phase_status_raw,
                    "raw_data_type": request.POST.get("raw_data_type", "xrd"),
                    "elements": raw_elements,
                    "structure_family": structure_family,
                    "synthesis_steps": synthesis_steps,
                    "notes": request.POST.get("comments", "") or request.POST.get("notes", ""),
                    "file_hash": additional_params.get("file_hash"),
                    "has_csv": has_csv,
                },
                media_src_path=csv_path if has_csv else None,
                media_dest_filename=f"{trial_id}.csv" if has_csv else None,
            )

            messages.success(
                request,
                f"{'Experimental data updated' if edit_trial_record is not None else 'Experimental data added'} to {material_auid}",
            )
            return redirect("composition_detail", material_auid=material_auid)

        except Exception as exc:
            if not handled_validation_error:
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
        "structure_families": ["rocksalt", "pyrochlore", "spinel", "perovskite", "fluorite", "other"],
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

            material_auid = compute_material_auid(raw_elements, structure_family)

            synthesis_steps = _parse_synthesis_steps_from_request(request)
            additional_params = {"synthesis_steps": synthesis_steps} if synthesis_steps else {}

            legacy_milling_time = _safe_float(request.POST.get("milling_time"))
            legacy_milling_rpm = _safe_float(request.POST.get("milling_rpm"))
            legacy_atmosphere = (request.POST.get("atmosphere") or "").strip()
            raw_cooling = (request.POST.get("cooling_method") or "").strip()
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

            doi = (request.POST.get("doi") or "").strip()
            if not doi:
                messages.error(request, "DOI is required for a literature entry.")
                raise ValueError("doi_required")

            recipe_auid = compute_recipe_auid(material_auid, synthesis_steps or [])

            authors_raw = request.POST.get("authors", "")
            authors = [a.strip() for a in authors_raw.split(",") if a.strip()]

            visibility = _get_visibility_affiliations_for_create(request.user)

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
                title=request.POST.get("title", ""),
                authors=authors,
                journal=request.POST.get("journal", ""),
                year=_safe_int(request.POST.get("year")),
                synthesis_successful=request.POST.get("synthesis_successful") == "true",
                exp_condition=exp_condition,
                notes=request.POST.get("findings", ""),
                extracted_by=request.user.username if request.user.is_authenticated else "anonymous",
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
                    set_on_insert__title=request.POST.get("title", ""),
                    add_to_set__material_auids=material_auid,
                    upsert=True,
                )

            archive_upload(
                upload_type="literature",
                username=request.user.username if request.user.is_authenticated else "anonymous",
                timestamp=timezone.localtime(timezone.now()),
                metadata={
                    "type": "literature",
                    "doi": doi,
                    "material_auid": material_auid,
                    "recipe_auid": recipe_auid,
                    "extracted_by": request.user.username if request.user.is_authenticated else "anonymous",
                    "title": request.POST.get("title", ""),
                    "authors": authors,
                    "journal": request.POST.get("journal", ""),
                    "year": _safe_int(request.POST.get("year")),
                    "synthesis_successful": request.POST.get("synthesis_successful") == "true",
                    "notes": request.POST.get("findings", ""),
                    "elements": raw_elements,
                    "structure_family": structure_family,
                    "synthesis_steps": synthesis_steps,
                },
            )

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
        "structure_families": ["rocksalt", "pyrochlore", "spinel", "perovskite", "fluorite", "other"],
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

            new_dft = EmbeddedDFT(
                comp_auid=new_comp_auid,
                dft_source=request.POST.get("dft_source", ""),
                dft_formation_energy_ev=_safe_float(request.POST.get("formation_energy")),
                dft_hull_distance_ev=_safe_float(request.POST.get("hull_distance")),
                dft_bandgap_ev=_safe_float(request.POST.get("bandgap")),
                dft_metadata={k: v for k, v in dft_inputs.items() if k != "dft_source"},
                ml_predictions=ml_predictions,
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
        "structure_families": ["rocksalt", "pyrochlore", "spinel", "perovskite", "fluorite", "other"],
        "dft_sources": ["mintaka", "AFLOW", "Materials Project", "OQMD", "manual", "other"],
        "calc_methods": ["DFT", "DFT+U", "hybrid", "GW", "other"],
        "functionals": ["PBE", "PBEsol", "LDA", "HSE06", "SCAN", "other"],
        "existing_material_auid": existing_material_auid,
        "existing_comp_record": existing_comp_record,
        "edit_comp_auid": edit_comp_auid,
        "locked_elements_json": json.dumps(_elements_to_selection_list(locked_elements)),
        "locked_elements_display": _elements_to_selection_list(locked_elements),
        "locked_structure_family": locked_structure,
    }
    return render(request, "catalog/add_computational_data.html", context)


# =============================================================================
# API: DOI + composition normalization
# =============================================================================

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
