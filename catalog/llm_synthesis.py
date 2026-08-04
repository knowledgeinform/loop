"""OpenRouter-backed parser that splits a free-form synthesis route into discrete
``synthesis_steps[]``. Gated by ``SYNTHESIS_LLM_ENABLED``; failures raise
``SynthesisParseUnavailable`` so callers keep the single ``other`` step."""
from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from typing import Any, Dict, List

from django.conf import settings

from .batch_upload import STEP_FIELD_SCHEMA, STEP_TYPE_VALUES

logger = logging.getLogger(__name__)

# Bump whenever the prompt or output contract changes so cached parses from an
# older prompt are not reused. Part of the cache key.
PROMPT_VERSION = "1"

# Human-readable hint per field ``kind`` in STEP_FIELD_SCHEMA, used to build the
# prompt so the model knows the expected value type for each field.
_KIND_HINT = {
    "float": "number",
    "int": "integer",
    "ratio": '"X:Y" ratio',
    "list": "list",
    "str": "text",
}

# Closed option sets for the ``str`` fields that the manual upload form (see
# ``catalog/templates/catalog/upload_exp_data.html``) renders as a ``<select>``
# rather than free text. Mirrored here so the model is constrained to the same
# fixed vocabulary and cannot invent values the app has no dropdown entry for.
_FIELD_OPTIONS: Dict[str, Dict[str, tuple]] = {
    "ball_milling": {
        "atmosphere": ("argon", "nitrogen", "air", "vacuum", "unknown", "na"),
        "jar_material": (
            "stainless_steel",
            "tungsten_carbide",
            "zirconia",
            "agate",
            "plastic",
            "unknown",
            "na",
        ),
        "ball_material": (
            "stainless_steel",
            "tungsten_carbide",
            "zirconia",
            "ysz",
            "unknown",
            "na",
        ),
    },
    "mixing": {
        "mixing_method": ("mortar_pestle", "tumbler", "vortex", "unknown", "na"),
    },
    "heat_treatment": {
        "atmosphere": (
            "air",
            "argon",
            "nitrogen",
            "hydrogen",
            "oxygen",
            "vacuum",
            "unknown",
            "na",
        ),
        "furnace_type": ("box", "tube", "muffle", "sps", "unknown", "na"),
    },
    "annealing": {
        "atmosphere": ("air", "argon", "vacuum", "unknown", "na"),
    },
    "arc_melting": {
        "hearth_material": ("copper", "graphite", "unknown", "na"),
        "atmosphere": ("argon", "argon_hydrogen", "unknown", "na"),
    },
    "quenching": {
        "quenching_medium": ("water", "oil", "liquid_nitrogen", "air", "unknown", "na"),
    },
    "cooling": {
        "cooling_method": ("furnace_cool", "air_cool", "controlled", "unknown", "na"),
    },
    "grinding": {
        "grinding_method": ("mortar", "ball_mill", "polishing", "unknown", "na"),
    },
    "xrd_measurement": {
        "radiation": ("cu_ka", "co_ka", "mo_ka", "unknown", "na"),
    },
}

# Object shape for ``list``-kind fields, keyed by (step_type, field). Order and
# keys must match ``_PRECURSOR_KEYS`` in ``catalog/batch_upload.py`` (the only
# consumer of these lists), which in turn mirrors the manual upload form's
# per-precursor row (name, formula, cas_number, purity, supplier, notes).
_LIST_ITEM_SCHEMA: Dict[tuple, tuple] = {
    ("weighing", "precursors_list"): (
        "cas_number",
        "name",
        "formula",
        "purity",
        "supplier",
        "notes",
    ),
}


class SynthesisParseUnavailable(Exception):
    """Raised when the LLM is disabled, misconfigured, or the API call fails."""


def is_enabled() -> bool:
    """True iff the feature is switched on and an API key is configured."""
    return bool(
        getattr(settings, "SYNTHESIS_LLM_ENABLED", False)
        and getattr(settings, "OPENROUTER_API_KEY", "")
    )


def _field_hint(step_type: str, name: str, kind: str) -> str:
    """Render one field's value contract: its kind, or its closed option list."""
    options = _FIELD_OPTIONS.get(step_type, {}).get(name)
    if options is not None:
        return f"{name} (one of: {', '.join(options)})"
    if kind == "list":
        item_fields = _LIST_ITEM_SCHEMA.get((step_type, name))
        if item_fields is not None:
            return (
                f"{name} (list of objects, one per precursor, each with "
                f"optional fields: {', '.join(item_fields)})"
            )
    return f"{name} ({_KIND_HINT.get(kind, kind)})"


def _build_step_catalog() -> str:
    """Render the allowed step types and their fields for the system prompt."""
    lines: List[str] = []
    for step_type in STEP_TYPE_VALUES:
        if step_type in ("unknown", "na"):
            continue
        fields = STEP_FIELD_SCHEMA[step_type]
        if fields:
            rendered = ", ".join(
                _field_hint(step_type, name, kind) for name, kind in fields.items()
            )
        else:
            rendered = "(no structured fields)"
        lines.append(f"- {step_type}: {rendered}")
    return "\n".join(lines)


def _system_prompt() -> str:
    return (
        "You convert a free-text materials-synthesis description into an ordered "
        "list of discrete, structured steps for a materials database.\n\n"
        "Return ONLY a JSON object of the form "
        '{"steps": [{"step_type": "...", "<field>": <value>, "notes": "..."}, ...]} '
        "with the steps in chronological order.\n\n"
        "Allowed step_type values and their optional fields:\n"
        f"{_build_step_catalog()}\n\n"
        "Rules:\n"
        "- Use the most specific step_type that fits; use \"other\" only when a "
        "step matches no type, putting the text in its \"description\" field.\n"
        "- Include a field only when the text states or clearly implies its "
        "value; omit any field you cannot determine. Never invent values.\n"
        "- For a field marked \"one of: ...\", you must use one of the listed "
        "options verbatim; if the text describes something not on the list, "
        "use \"unknown\" (or \"na\" if not mentioned at all) instead of "
        "inventing a new option.\n"
        "- For \"weighing\" steps, put every distinct precursor/reagent "
        "mentioned as its own object in \"precursors_list\" (one entry per "
        "reagent, not one string listing them all); use \"precursors\" only "
        "for leftover text that does not fit those per-reagent fields.\n"
        "- Numbers must be plain numbers in the field's unit (hours, rpm, "
        "degrees C, MPa, etc.). Ratios use the form \"10:1\".\n"
        "- Put anything useful that no field captures into that step's \"notes\".\n"
        "- Split distinct physical operations into separate steps; do not merge "
        "milling, heating, and cooling into one step."
    )


def _cache_key(text: str) -> str:
    model = getattr(settings, "OPENROUTER_MODEL", "")
    blob = f"{PROMPT_VERSION}\x00{model}\x00{text}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_get(key: str):
    """Return cached steps for ``key`` or ``None``. Cache misses never raise."""
    try:
        from .documents import SynthesisParseCache

        hit = SynthesisParseCache.objects(id=key).first()
        return list(hit.steps) if hit is not None else None
    except Exception as exc:  # cache is best-effort
        logger.debug("synthesis parse cache read failed: %s", exc)
        return None


def _cache_put(key: str, steps: List[Dict[str, Any]]) -> None:
    try:
        from .documents import SynthesisParseCache

        SynthesisParseCache.objects(id=key).update_one(
            set__steps=steps,
            set__model=getattr(settings, "OPENROUTER_MODEL", ""),
            set__prompt_version=PROMPT_VERSION,
            upsert=True,
        )
    except Exception as exc:  # cache is best-effort
        logger.debug("synthesis parse cache write failed: %s", exc)


def _call_openrouter(text: str) -> Dict[str, Any]:
    """POST the route text to OpenRouter and return the parsed JSON content."""
    base = getattr(settings, "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    url = f"{base.rstrip('/')}/chat/completions"
    payload = {
        "model": getattr(settings, "OPENROUTER_MODEL", ""),
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": text},
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {getattr(settings, 'OPENROUTER_API_KEY', '')}",
            "Content-Type": "application/json",
            # OpenRouter attribution headers (optional but recommended).
            "HTTP-Referer": "https://s4e.ai",
            "X-Title": "LOOP",
        },
        method="POST",
    )
    timeout = float(getattr(settings, "SYNTHESIS_LLM_TIMEOUT", 60))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode()[:500]
        except Exception:
            pass
        raise SynthesisParseUnavailable(
            f"OpenRouter HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SynthesisParseUnavailable(f"OpenRouter network error: {exc.reason}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise SynthesisParseUnavailable(f"OpenRouter returned non-JSON: {exc}") from exc

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SynthesisParseUnavailable(f"Unexpected OpenRouter response shape: {exc}") from exc

    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (ValueError, json.JSONDecodeError) as exc:
            raise SynthesisParseUnavailable(
                f"Model content was not valid JSON: {exc}"
            ) from exc
    if not isinstance(content, dict):
        raise SynthesisParseUnavailable("Model content was not a JSON object")
    return content


def _extract_steps(content: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Keep only well-formed steps with a known ``step_type`` (lowercased).

    Field-level coercion/validation is left to
    :func:`catalog.batch_upload.normalize_synthesis_steps`; this only filters out
    junk the model may emit so that normalization does not reject the whole row.
    """
    raw_steps = content.get("steps")
    if not isinstance(raw_steps, list):
        raise SynthesisParseUnavailable("Model JSON missing a 'steps' list")

    steps: List[Dict[str, Any]] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        step_type = str(raw.get("step_type") or "").strip().lower()
        if step_type not in STEP_TYPE_VALUES:
            continue
        step = {k: v for k, v in raw.items() if k != "step_type"}
        step["step_type"] = step_type
        steps.append(step)
    return steps


def parse_synthesis_route(text: str) -> List[Dict[str, Any]]:
    """Split a free-text synthesis route into raw typed step dicts.

    Returns a list shaped for :func:`normalize_synthesis_steps` (each dict has a
    ``step_type`` plus any determinable fields). Returns ``[]`` for empty input.
    Raises :class:`SynthesisParseUnavailable` when disabled/misconfigured or on
    any API/parse failure, so callers can fall back to the single ``other`` step.
    """
    text = (text or "").strip()
    if not text:
        return []
    if not is_enabled():
        raise SynthesisParseUnavailable("synthesis LLM disabled or no API key")

    key = _cache_key(text)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    steps = _extract_steps(_call_openrouter(text))
    _cache_put(key, steps)
    return steps
