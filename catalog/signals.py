"""
MongoEngine signal wiring.

On every ``Material`` / ``Recipe`` save we refresh the corresponding
``MLEmbedding`` row so the browse-page semantic search stays in sync with the
live data. Embedded ``DFT`` calculations can't register their own post_save
(they're embedded, not top-level documents), so the ``add_computational_data``
view calls :func:`refresh_comp_embedding` directly after it has re-saved the
parent material.

Failures in the embedding pipeline (missing model, network hiccup downloading
the sentence-transformers checkpoint, Mongo blip) are logged and swallowed —
vector search is an enhancement, not a correctness property, and we don't
want an uploader's form submission to fail because the embedder went sideways.

The whole hook set is gated on ``settings.EMBEDDINGS_ON_WRITE`` (default
``True``) so tests and low-resource environments can turn it off without
patching modules.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings
from mongoengine import signals as me_signals

logger = logging.getLogger(__name__)

_signals_connected = False


def _embeddings_enabled() -> bool:
    """Toggle the on-write hooks via a Django setting."""
    return bool(getattr(settings, "EMBEDDINGS_ON_WRITE", True))


def _refresh_material_embedding(material) -> None:
    """Recompute ``composition_embedding`` + ``structure_embedding`` for a Material."""
    from . import embeddings as embeddings_mod
    from .documents import MLEmbedding

    text = embeddings_mod.material_text(material)
    vector = embeddings_mod.embed_text(text)
    MLEmbedding.objects(scope="material", material_auid=material.id).update_one(
        set__composition_embedding=vector,
        set__structure_embedding=vector,
        set__model_version=embeddings_mod.EMBEDDING_MODEL_VERSION,
        upsert=True,
    )


def _refresh_recipe_embedding(recipe) -> None:
    """Recompute ``synthesis_embedding`` for a Recipe."""
    from . import embeddings as embeddings_mod
    from .documents import MLEmbedding

    text = embeddings_mod.recipe_text(recipe)
    vector = embeddings_mod.embed_text(text)
    MLEmbedding.objects(scope="recipe", recipe_auid=recipe.id).update_one(
        set__material_auid=recipe.material_auid,
        set__synthesis_embedding=vector,
        set__model_version=embeddings_mod.EMBEDDING_MODEL_VERSION,
        upsert=True,
    )


def refresh_comp_embedding(material, dft) -> None:
    """Public helper for views that just wrote an ``EmbeddedDFT``.

    Embedded documents don't fire their own MongoEngine signals — saving the
    parent material triggers our Material post_save, but that hook intentionally
    doesn't re-embed every attached DFT on every annotation edit. Views that
    know they just added or updated a specific DFT call this instead.
    """
    if not _embeddings_enabled():
        return
    comp_auid = getattr(dft, "comp_auid", None)
    if not comp_auid:
        return
    try:
        from . import embeddings as embeddings_mod
        from .documents import MLEmbedding

        text = embeddings_mod.comp_text(material, dft)
        vector = embeddings_mod.embed_text(text)
        MLEmbedding.objects(scope="comp", comp_auid=comp_auid).update_one(
            set__material_auid=material.id,
            set__structure_embedding=vector,
            set__model_version=embeddings_mod.EMBEDDING_MODEL_VERSION,
            upsert=True,
        )
    except Exception as exc:
        logger.warning("refresh_comp_embedding failed for %s: %s", comp_auid, exc)


def delete_comp_embedding(comp_auid: Optional[str]) -> None:
    """Best-effort cleanup after a DFT calc is removed from its parent."""
    if not comp_auid:
        return
    try:
        from .documents import MLEmbedding
        MLEmbedding.objects(scope="comp", comp_auid=comp_auid).delete()
    except Exception as exc:
        logger.warning("delete_comp_embedding failed for %s: %s", comp_auid, exc)


# --- MongoEngine post_save handlers ------------------------------------------

def _material_post_save(sender, document, **kwargs):
    if not _embeddings_enabled():
        return
    try:
        _refresh_material_embedding(document)
    except Exception as exc:
        logger.warning(
            "Material embedding refresh failed for %s: %s", document.id, exc
        )


def _recipe_post_save(sender, document, **kwargs):
    if not _embeddings_enabled():
        return
    try:
        _refresh_recipe_embedding(document)
    except Exception as exc:
        logger.warning(
            "Recipe embedding refresh failed for %s: %s", document.id, exc
        )


def connect_document_signals() -> None:
    """Register post_save hooks; safe to call repeatedly."""
    global _signals_connected
    if _signals_connected:
        return
    from .documents import Material, Recipe

    me_signals.post_save.connect(_material_post_save, sender=Material)
    me_signals.post_save.connect(_recipe_post_save, sender=Recipe)
    _signals_connected = True


__all__ = [
    "connect_document_signals",
    "refresh_comp_embedding",
    "delete_comp_embedding",
]
