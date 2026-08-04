"""Small, cached client for AFLOW's public AFLUX API.

AFLOW does not publish EFA or DEED as direct AFLUX properties.  LOOP uses the
available formation-enthalpy and electronic-entropy records as model features
and provenance; the EFA/DEED values themselves come from the trained
ChemScreen-style regressors.
"""
from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings

from .documents import AFLOWCache, _utc_now


def _safe_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def species_key(elements: Mapping[str, Any] | Iterable[str]) -> str:
    symbols = (
        elements.keys()
        if isinstance(elements, Mapping)
        else elements
    )
    normalized = sorted({str(symbol).strip() for symbol in symbols if str(symbol).strip()})
    return hashlib.sha256(",".join(normalized).encode("utf-8")).hexdigest()[:24]


def build_aflux_query(
    elements: Mapping[str, Any] | Iterable[str],
    *,
    limit: int = 25,
) -> str:
    symbols = sorted(
        {
            str(symbol).strip()
            for symbol in (elements.keys() if isinstance(elements, Mapping) else elements)
            if str(symbol).strip()
        }
    )
    if not symbols:
        raise ValueError("AFLOW lookup needs at least one element.")
    joined = ",".join(symbols)
    return (
        f"species({joined}),nspecies({len(symbols)}),"
        "enthalpy_formation_atom(*),eentropy_atom,compound,"
        f"$paging(1,{max(1, min(int(limit), 100))})"
    )


def _summarize(records: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    records = list(records)
    formation = [
        value
        for record in records
        if (value := _safe_number(record.get("enthalpy_formation_atom"))) is not None
    ]
    entropy = [
        value
        for record in records
        if (value := _safe_number(record.get("eentropy_atom"))) is not None
    ]

    def stats(values: List[float], prefix: str) -> Dict[str, float]:
        if not values:
            return {}
        average = sum(values) / len(values)
        variance = sum((value - average) ** 2 for value in values) / len(values)
        return {
            f"{prefix}_mean": average,
            f"{prefix}_std": math.sqrt(variance),
            f"{prefix}_min": min(values),
            f"{prefix}_max": max(values),
        }

    return {
        "match_count": len(records),
        **stats(formation, "formation"),
        **stats(entropy, "electronic_entropy"),
    }


def fetch_aflow_records(
    elements: Mapping[str, Any] | Iterable[str],
    *,
    force: bool = False,
    limit: Optional[int] = None,
) -> AFLOWCache:
    """Return a cached exact-species AFLUX result, refreshing when stale."""
    key = species_key(elements)
    max_age_hours = float(getattr(settings, "AFLOW_CACHE_HOURS", 168))
    cached = AFLOWCache.objects(id=key).first()
    if (
        cached is not None
        and not force
        and cached.fetched_at
        and cached.fetched_at >= _utc_now() - timedelta(hours=max_age_hours)
    ):
        return cached

    symbols = sorted(
        {
            str(symbol).strip()
            for symbol in (elements.keys() if isinstance(elements, Mapping) else elements)
            if str(symbol).strip()
        }
    )
    query = build_aflux_query(
        symbols,
        limit=limit or int(getattr(settings, "AFLOW_RESULT_LIMIT", 25)),
    )
    base_url = str(
        getattr(settings, "AFLOW_API_BASE_URL", "https://aflow.org/API/aflux/")
    ).rstrip("/") + "/"
    url = f"{base_url}?{urlencode({'summons': query})}".replace("summons=", "")
    request = Request(url, headers={"User-Agent": "LOOP/1.0 (materials research)"})

    try:
        with urlopen(
            request,
            timeout=float(getattr(settings, "AFLOW_API_TIMEOUT", 12)),
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        records = payload if isinstance(payload, list) else []
        status = "ok" if records else "empty"
        cache = AFLOWCache(
            id=key,
            species=symbols,
            query=query,
            status=status,
            records=records,
            summary=_summarize(records),
            error="",
            fetched_at=_utc_now(),
        )
    except Exception as exc:
        cache = AFLOWCache(
            id=key,
            species=symbols,
            query=query,
            status="error",
            records=[],
            summary={},
            error=str(exc)[:1000],
            fetched_at=_utc_now(),
        )
    cache.save()
    return cache


def cached_aflow_summary(elements: Mapping[str, Any] | Iterable[str]) -> Dict[str, Any]:
    cached = AFLOWCache.objects(id=species_key(elements)).first()
    return dict(cached.summary or {}) if cached else {}


__all__ = [
    "build_aflux_query",
    "cached_aflow_summary",
    "fetch_aflow_records",
    "species_key",
]
