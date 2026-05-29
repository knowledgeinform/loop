"""
Text-to-vector helpers for the browse-page semantic search.

Embedding model is a local ``sentence-transformers`` checkpoint
(``all-MiniLM-L6-v2``, 384 dimensions, ~90 MB) loaded lazily on first use.
The model is cached as a module-level singleton so subsequent requests stay
cheap.

Two responsibilities live here:

1. Render a ``Material`` / ``Recipe`` / ``EmbeddedDFT`` into a single piece of
   natural-language text that captures the semantic content of that record
   (elements, structure family, synthesis recipe, DFT metadata, notes, ...).
2. Turn that text — and any free-form user query — into a ``List[float]``
   vector suitable for upsert into ``MLEmbedding`` and for ``$vectorSearch``.

Keeping the text builders and the embedder together means ``backfill_embeddings``
and :func:`catalog.vector_search.semantic_material_auids` agree on what the
vector actually represents.
"""
from __future__ import annotations

import json
from threading import Lock
from typing import Any, Dict, List, Optional

# Sentence-transformers model identifier. Kept small on purpose: loads quickly,
# runs on CPU, and the 384-dim vectors are cheap to store/index.
EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_MODEL_VERSION = "all-MiniLM-L6-v2"
EMBEDDING_DIMENSIONS = 384

_model = None
_model_lock = Lock()


class EmbeddingUnavailable(RuntimeError):
    """Raised when the embedding model cannot be loaded at runtime.

    Lets call sites (views, management commands) distinguish a transient
    model-load failure from genuine "no results" so they can degrade
    gracefully (e.g. fall back to regex search).
    """


def get_embedder():
    """Return the cached ``SentenceTransformer`` instance, loading on demand."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingUnavailable(
                "sentence-transformers is not installed. "
                "Add it to requirements.txt and `pip install` to enable semantic search."
            ) from exc
        try:
            _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
        except Exception as exc:
            raise EmbeddingUnavailable(
                f"Could not load embedding model {EMBEDDING_MODEL_NAME}: {exc}"
            ) from exc
    return _model


def embed_text(text: str) -> List[float]:
    """Encode ``text`` into a unit-normalized dense vector."""
    model = get_embedder()
    cleaned = (text or "").strip()
    if not cleaned:
        cleaned = " "
    vector = model.encode(cleaned, normalize_embeddings=True)
    return [float(x) for x in vector.tolist()]


# =============================================================================
# Text builders: turn a record into a descriptive natural-language string.
# =============================================================================

def _format_elements(elements: Optional[Dict[str, Any]]) -> str:
    if not elements:
        return ""
    parts: List[str] = []
    for el, ratio in elements.items():
        if ratio in (None, "", 0):
            parts.append(str(el))
        else:
            parts.append(f"{el}:{ratio}")
    return " ".join(parts)


def material_text(material) -> str:
    """Concatenate every semantically useful field on a ``Material``.

    Used to populate the ``composition_embedding`` and ``structure_embedding``
    fields for scope="material".
    """
    elements = getattr(material, "elements", None) or {}
    structure_family = getattr(material, "structure_family", None) or ""
    display_name = getattr(material, "display_name", None) or ""
    notes = getattr(material, "notes", None) or ""
    curator = getattr(material, "curator", None) or ""
    bits: List[str] = []
    if display_name:
        bits.append(display_name)
    element_str = _format_elements(elements)
    if element_str:
        bits.append(f"elements {element_str}")
    if structure_family:
        bits.append(f"structure {structure_family}")
    if curator:
        bits.append(f"curator {curator}")
    if notes:
        bits.append(notes)
    return " | ".join(bits)


def _format_synthesis_step(step: Dict[str, Any]) -> str:
    if not isinstance(step, dict):
        return ""
    step_type = str(step.get("step_type") or "").replace("_", " ").strip()
    pieces: List[str] = []
    if step_type:
        pieces.append(step_type)
    for key, value in step.items():
        if key in {"step_number", "step_type"} or value in (None, "", [], {}):
            continue
        if key == "precursors_list" and isinstance(value, list):
            precursors = ", ".join(
                str(p.get("name") or p.get("formula") or p.get("cas_number") or "")
                for p in value
                if isinstance(p, dict)
            )
            if precursors:
                pieces.append(f"precursors {precursors}")
            continue
        pretty = str(key).replace("_", " ")
        pieces.append(f"{pretty} {value}")
    return " ".join(pieces)


def recipe_text(recipe) -> str:
    """Concatenate synthesis-step semantics plus the recipe's material context.

    Used to populate the ``synthesis_embedding`` for scope="recipe".
    """
    elements = getattr(recipe, "elements", None) or {}
    structure_family = getattr(recipe, "structure_family", None) or ""
    synthesis_steps = getattr(recipe, "synthesis_steps", None) or []
    bits: List[str] = []
    element_str = _format_elements(elements)
    if element_str:
        bits.append(f"elements {element_str}")
    if structure_family:
        bits.append(f"structure {structure_family}")
    step_texts = [_format_synthesis_step(step) for step in synthesis_steps]
    step_texts = [s for s in step_texts if s]
    if step_texts:
        bits.append("steps: " + " ; ".join(step_texts))
    return " | ".join(bits)


def comp_text(material, dft) -> str:
    """Concatenate DFT metadata plus the parent material context.

    Used to populate the ``structure_embedding`` for scope="comp".
    """
    bits: List[str] = []
    if material is not None:
        element_str = _format_elements(getattr(material, "elements", None))
        if element_str:
            bits.append(f"elements {element_str}")
        sf = getattr(material, "structure_family", None)
        if sf:
            bits.append(f"structure {sf}")
    dft_source = getattr(dft, "dft_source", None)
    if dft_source:
        bits.append(f"dft_source {dft_source}")
    metadata = getattr(dft, "dft_metadata", None) or {}
    for key, value in metadata.items():
        if value in (None, ""):
            continue
        bits.append(f"{key} {value}")
    for key in ("dft_formation_energy_ev", "dft_hull_distance_ev", "dft_bandgap_ev"):
        value = getattr(dft, key, None)
        if value is not None:
            bits.append(f"{key} {value}")
    ml_predictions = getattr(dft, "ml_predictions", None) or {}
    if ml_predictions:
        try:
            bits.append("ml_predictions " + json.dumps(ml_predictions, sort_keys=True, default=str))
        except Exception:
            pass
    return " | ".join(bits)


__all__ = [
    "EMBEDDING_MODEL_NAME",
    "EMBEDDING_MODEL_VERSION",
    "EMBEDDING_DIMENSIONS",
    "EmbeddingUnavailable",
    "get_embedder",
    "embed_text",
    "material_text",
    "recipe_text",
    "comp_text",
]
