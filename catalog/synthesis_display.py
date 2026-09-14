"""Plain-language summaries of stored synthesis routes for catalog lists."""

from __future__ import annotations

import math


def _number(step, keys, unit):
    for key in keys:
        value = step.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return f"{number:g} {unit}"
    return ""


def format_steps_preview(steps, max_len: int = 96) -> str:
    """Summarize steps in their stored order without exposing nested data.

    Include recorded temperature, duration, and atmosphere where available.
    Imported free-text routes use an ``other`` step with a ``description``.
    Unknown or malformed steps stay visibly unspecified; never infer a route.
    """
    if not steps:
        return "—"
    if isinstance(steps, dict):
        steps = [steps]
    if not isinstance(steps, (list, tuple)):
        return "Steps not reported"

    labels = []
    for step in steps:
        if not isinstance(step, dict):
            labels.append("Unspecified step")
            continue
        raw_type = step.get("step_type")
        label = raw_type.replace("_", " ").strip() if isinstance(raw_type, str) else ""
        if label.lower() in {"", "unknown", "unspecified", "na", "n/a"}:
            label = "Unspecified step"
        else:
            label = label[0].upper() + label[1:]
        description = step.get("description")
        if label in {"Other", "Unspecified step"} and isinstance(description, str):
            # The importers keep the actual route here. ``notes`` can instead
            # contain batch provenance, so it is not a synthesis fallback.
            label = " ".join(description.split()) or label
        details = [
            _number(step, ("max_temp_c", "temperature_c"), "°C"),
            _number(step, ("hold_time_hours", "duration_hours", "milling_time_hours"), "h"),
        ]
        atmosphere = step.get("atmosphere")
        if isinstance(atmosphere, str) and atmosphere.strip():
            details.append(atmosphere.strip())
        details = [value for value in details if value]
        labels.append(f"{label} ({', '.join(details)})" if details else label)

    text = " → ".join(labels)
    if len(text) > max_len:
        return text[:max(0, max_len - 1)].rstrip() + "…"
    return text
