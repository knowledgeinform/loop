"""
Atlas ``$vectorSearch``-backed semantic lookup for the browse page.

The browse-page search bar calls :func:`semantic_material_auids` with a raw
user query; the helper embeds it with :mod:`catalog.embeddings` and fans out
three ``$vectorSearch`` stages (one per declared vector index on
``ml_embeddings``). We union the hits, keep the best score per material, and
return an order-preserving list of ``material_auid`` strings the browse view
then uses as a pre-filter.

Non-Atlas deployments don't have the ``$vectorSearch`` stage; we catch the
resulting PyMongo error and raise :class:`VectorSearchUnavailable` so the
caller can fall back to the existing regex-over-``material_auid`` behaviour.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from django.conf import settings
from mongoengine.connection import get_db

from . import embeddings as embeddings_mod
from .documents import MLEmbedding

logger = logging.getLogger(__name__)


# Declared vector-search indexes. Must match the names created by
# ``mongo_admin ensure-vector-indexes``. Each tuple is (field_path, index_name).
VECTOR_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("composition_embedding", "ml_embedding_composition_vidx"),
    ("structure_embedding", "ml_embedding_structure_vidx"),
    ("synthesis_embedding", "ml_embedding_synthesis_vidx"),
)


class VectorSearchUnavailable(RuntimeError):
    """Raised when Atlas Vector Search cannot service a query.

    Covers three distinct failure modes — missing ``$vectorSearch`` stage
    (non-Atlas deployment), missing search index, and embedding-model load
    failure — so the caller can uniformly degrade to a regex fallback.
    """


def _run_one_vector_search(
    coll,
    field: str,
    index_name: str,
    query_vector: List[float],
    limit: int,
    num_candidates: int,
) -> List[Dict]:
    """Run a single ``$vectorSearch`` stage. Returns docs with ``_score``."""
    pipeline = [
        {
            "$vectorSearch": {
                "index": index_name,
                "path": field,
                "queryVector": query_vector,
                "numCandidates": num_candidates,
                "limit": limit,
            }
        },
        {
            "$project": {
                "_id": 0,
                "material_auid": 1,
                "recipe_auid": 1,
                "comp_auid": 1,
                "scope": 1,
                "_score": {"$meta": "vectorSearchScore"},
            }
        },
    ]
    try:
        return list(coll.aggregate(pipeline))
    except Exception as exc:
        message = str(exc).lower()
        if (
            "$vectorsearch" in message
            or "unrecognized pipeline stage" in message
            or "index" in message and "not found" in message
            or "searchindex" in message
        ):
            raise VectorSearchUnavailable(
                f"$vectorSearch unavailable for {field} ({index_name}): {exc}"
            ) from exc
        # Unknown error — bubble up as unavailable so the caller degrades.
        raise VectorSearchUnavailable(str(exc)) from exc


def semantic_material_auids(
    query_text: str,
    *,
    limit: int = 100,
    num_candidates: Optional[int] = None,
    user_affiliations: Optional[Iterable[str]] = None,
    score_threshold: Optional[float] = None,
    hit_filter: Optional[Callable[[Dict], bool]] = None,
) -> List[Tuple[str, float]]:
    """Return ``[(material_auid, score), ...]`` ranked by cross-field best score.

    ``limit`` is per-field, so the merged result can contain up to
    ``3 * limit`` unique materials in the worst case. Callers are expected
    to paginate/trim downstream.

    ``score_threshold`` — minimum cosine score a match must reach to be
    returned. Defaults to ``settings.SEMANTIC_SCORE_THRESHOLD`` (0.55).
    Atlas Vector Search happily returns low-confidence matches well into
    the 0.3-0.5 "pure noise" range, which visibly pollutes the browse page
    for any query that doesn't actually correspond to anything in the
    catalog. Filtering below the threshold yields an honest empty state
    for nonsense queries and tighter, more relevant results otherwise.
    Pass ``0.0`` (or negative) to disable.

    Raises :class:`VectorSearchUnavailable` if any of the three indexed
    ``$vectorSearch`` calls fails to execute — a signal to the caller that
    the deployment doesn't support Atlas Vector Search and it should fall
    back to a textual search.
    """
    cleaned = (query_text or "").strip()
    if not cleaned:
        return []

    try:
        vector = embeddings_mod.embed_text(cleaned)
    except embeddings_mod.EmbeddingUnavailable as exc:
        raise VectorSearchUnavailable(f"Embedding model unavailable: {exc}") from exc

    db = get_db()
    coll = db[MLEmbedding._meta["collection"]]

    # Pre-flight: `$vectorSearch` on a collection with no matching search index
    # returns zero hits silently instead of raising. That produces an empty
    # browse page that looks like "semantic search found nothing" when really
    # the deployment was never indexed. Inspect the index catalog up-front so
    # we can fall back to the regex AUID search (and surface the banner) when
    # none of the declared indexes exist or are queryable yet.
    expected_indexes = {name for _field, name in VECTOR_FIELDS}
    try:
        existing = {
            idx.get("name"): idx
            for idx in coll.list_search_indexes()
        }
    except Exception as exc:
        # listSearchIndexes only exists on Atlas / Atlas Local. A non-Atlas
        # Mongo errors here; treat that the same as "no vector search".
        raise VectorSearchUnavailable(
            f"list_search_indexes failed (non-Atlas deployment?): {exc}"
        ) from exc
    queryable = [
        name for name in expected_indexes
        if existing.get(name, {}).get("queryable")
    ]
    if not queryable:
        raise VectorSearchUnavailable(
            "No queryable Atlas Vector Search indexes on ml_embeddings. "
            "Run `python manage.py mongo_admin ensure-vector-indexes` and wait "
            "for the indexes to finish building."
        )

    num_candidates = num_candidates or max(limit * 10, 200)

    best_score: Dict[str, float] = {}
    any_hits = False
    last_exc: Optional[Exception] = None
    for field, index_name in VECTOR_FIELDS:
        if index_name not in queryable:
            logger.debug(
                "Skipping %s: index %s is not queryable yet.",
                field, index_name,
            )
            continue
        try:
            hits = _run_one_vector_search(
                coll, field, index_name, vector, limit=limit, num_candidates=num_candidates
            )
        except VectorSearchUnavailable as exc:
            # One index missing is fine (e.g. no synthesis embeddings yet);
            # we only give up if every index fails.
            last_exc = exc
            logger.debug("Vector index %s unavailable: %s", index_name, exc)
            continue
        any_hits = True
        for hit in hits:
            # Callers exposing scored evidence must authorize the underlying
            # record before its score can influence a material's ranking.
            if hit_filter is not None and not hit_filter(hit):
                continue
            material_auid = hit.get("material_auid")
            if not material_auid:
                continue
            score = float(hit.get("_score") or 0.0)
            if score > best_score.get(material_auid, -1.0):
                best_score[material_auid] = score

    if not any_hits:
        raise VectorSearchUnavailable(
            f"All vector indexes failed; most recent error: {last_exc}"
        )

    if score_threshold is None:
        score_threshold = float(getattr(settings, "SEMANTIC_SCORE_THRESHOLD", 0.55))
    ranked = sorted(best_score.items(), key=lambda kv: kv[1], reverse=True)
    if score_threshold > 0:
        filtered = [(auid, s) for auid, s in ranked if s >= score_threshold]
        if ranked and not filtered:
            logger.debug(
                "All %d semantic hits filtered by threshold %.3f (top score %.3f).",
                len(ranked), score_threshold, ranked[0][1],
            )
        ranked = filtered
    return ranked


__all__ = [
    "VectorSearchUnavailable",
    "VECTOR_FIELDS",
    "semantic_material_auids",
]
