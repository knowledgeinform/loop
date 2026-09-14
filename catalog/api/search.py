"""Authenticated semantic retrieval with exact chemistry filters and source visibility."""
from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from catalog.documents import Material, Recipe, STRUCTURE_FAMILY_VALUES
from catalog.embeddings import EMBEDDING_MODEL_NAME
from catalog.permissions import is_visible_to_user
from catalog.vector_search import VectorSearchUnavailable, semantic_material_auids
from catalog.views import _user_affiliations
from .permissions import HasDataReadScope, IsApprovedUser
from .views import _material_data, _problem, _unsupported_query_params_problem


@extend_schema(
    tags=["Materials"], summary="Search material and synthesis descriptions by meaning",
    description="Approximate vector retrieval with exact chemistry filters and record visibility. "
                "Similarity measures relevance, not a probability of synthesis. "
                "Returns 503 when embedding/index infrastructure is unavailable.",
    parameters=[OpenApiParameter("q", str, required=True), OpenApiParameter("elements", str),
                OpenApiParameter("structure_family", str), OpenApiParameter("limit", int)],
    responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 503: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def semantic_search(request):
    problem = _unsupported_query_params_problem(request, {"q", "elements", "structure_family", "limit"})
    if problem is not None:
        return problem
    query = (request.query_params.get("q") or "").strip()
    family = (request.query_params.get("structure_family") or "").strip().lower()
    elements = {x.strip() for x in request.query_params.get("elements", "").split(",") if x.strip()}
    try:
        limit = int(request.query_params.get("limit", "20"))
    except ValueError:
        limit = 0
    if not query or len(query) > 2000 or not 1 <= limit <= 100 or family and family not in STRUCTURE_FAMILY_VALUES:
        return _problem(request, status_code=400, title="Bad Request",
                        detail="Provide q (1–2000 characters), limit (1–100), and a valid structure_family.")

    affiliations = _user_affiliations(request.user)
    materials, recipes, evidence = {}, {}, {}
    threshold = float(getattr(settings, "SEMANTIC_SCORE_THRESHOLD", 0.55))

    def visible_hit(hit):
        auid = hit.get("material_auid")
        if not auid:
            return False
        if auid not in materials:
            materials[auid] = Material.objects(id=auid).first()
        material = materials[auid]
        if material is None or not is_visible_to_user(material.default_visibility_affiliations, affiliations):
            return False
        if family and material.structure_family != family:
            return False
        if not elements.issubset(material.element_symbols or []):
            return False
        scope = hit.get("scope")
        if scope == "recipe":
            rid = hit.get("recipe_auid")
            if not rid:
                return False
            if rid not in recipes:
                recipes[rid] = Recipe.objects(id=rid).first()
            recipe = recipes[rid]
            if recipe is None or recipe.material_auid != auid or not is_visible_to_user(recipe.visibility_affiliations, affiliations):
                return False
        elif scope == "comp":
            cid = hit.get("comp_auid")
            dft = next((d for d in material.dft_calculations or [] if d.comp_auid == cid), None)
            if dft is None or not is_visible_to_user(dft.visibility_affiliations, affiliations):
                return False
        elif scope != "material":
            return False
        if float(hit.get("_score") or 0) >= threshold:
            source = {key: hit[key] for key in ("scope", "recipe_auid", "comp_auid") if hit.get(key)}
            if source not in evidence.setdefault(auid, []):
                evidence[auid].append(source)
        return True

    candidate_limit = max(100, limit * 3)
    try:
        ranked = semantic_material_auids(query, limit=candidate_limit,
                                        user_affiliations=affiliations, hit_filter=visible_hit)
    except VectorSearchUnavailable:
        return _problem(request, status_code=503, title="Semantic Search Unavailable",
                        detail="Semantic search is unavailable. Use /materials/ for exact chemistry search; "
                               "an administrator must check embeddings and vector indexes.")
    rows = [{**_material_data(materials[auid]), "similarity_score": score,
             "matched_records": evidence.get(auid, [])} for auid, score in ranked[:limit]]
    return Response({"data": rows, "meta": {
        "query": query, "returned": len(rows), "limit": limit, "search_mode": "semantic",
        "embedding_model": EMBEDDING_MODEL_NAME, "score_threshold": threshold,
        "candidate_limit_per_index": candidate_limit,
        "complete": False, "coverage": "Approximate candidates; exact filters and visibility applied before ranking. "
                                        "This is not an exhaustive catalog enumeration.",
        "filters_applied": {"elements": sorted(elements), "structure_family": family or None},
        "score_meaning": "Retrieval relevance, not synthesis probability or chemical equivalence.",
    }})
