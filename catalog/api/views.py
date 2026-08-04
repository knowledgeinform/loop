"""Foundation endpoints for LOOP API v1."""

import json
from pathlib import Path

from django.conf import settings
from django.http import FileResponse
from django.urls import reverse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.decorators import authentication_classes
from rest_framework.authentication import BasicAuthentication, SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError

from catalog import batch_upload as batch_upload_mod
from catalog import views as catalog_views
from catalog.utils import render_xrd_plot, xrd_parse
from catalog.documents import (
    DOIMapping,
    Material,
    Recipe,
    UserPrecursor,
    UserProtocol,
    XRDAnalysisJob,
    compute_material_auid,
)
from catalog.xrd_analysis.persistence import (
    load_persisted_xrd_analysis,
    validate_persisted_xrd_analysis,
)
from catalog.xrd_analysis.worker import (
    XRDAnalysisJobError,
    allowed_analysis_artifact_names,
    assemble_repository_xrd_input,
    normalize_artifact_name,
    submit_xrd_analysis_job,
)
from catalog.views import (
    DuplicateFileError,
    DuplicateRecordError,
    _is_uploader_or_superuser,
    _is_visible_to_user,
    _find_precursor_collision,
    _get_visibility_affiliations_for_create,
    _get_visible_precursor,
    _get_visible_protocol,
    _normalize_cas,
    _user_affiliations,
    _user_can_edit_precursor,
    _user_can_edit_protocol,
    _visible_precursors_qs,
    _visible_protocols_qs,
)

from . import key_service, record_service
from .exceptions import PROBLEM_CONTENT_TYPE, force_json_rendering, problem_body
from .permissions import (
    HasCatalogScope,
    HasDataReadScope,
    HasDataWriteScope,
    HasFileWriteScope,
    HasImportWriteScope,
    IsApprovedUser,
)
from .serializers import (
    APIKeyCreateSerializer,
    ComputationalCreateSerializer,
    CompositionNormalizeSerializer,
    ExperimentCreateSerializer,
    ExperimentMultipartSerializer,
    LiteratureCreateSerializer,
    ImportSerializer,
    MaterialSerializer,
    PrecursorWriteSerializer,
    ProtocolWriteSerializer,
    RecordValidationSerializer,
)


@extend_schema(
    tags=["Platform"],
    summary="API health check",
    description="Returns API availability and its stable major version.",
    responses={
        200: inline_serializer(
            name="HealthResponse",
            fields={
                "data": inline_serializer(
                    name="HealthData",
                    fields={
                        "status": serializers.CharField(),
                        "api_version": serializers.CharField(),
                    },
                )
            },
        )
    },
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser])
def health(request):
    return Response({"data": {"status": "ok", "api_version": "v1"}})


@extend_schema(
    tags=["Platform"],
    summary="API version",
    description="Returns the deployed public interface version.",
    responses={
        200: inline_serializer(
            name="VersionResponse",
            fields={
                "data": inline_serializer(
                    name="VersionData",
                    fields={"version": serializers.CharField()},
                )
            },
        )
    },
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser])
def version(request):
    return Response({"data": {"version": "1.0.0"}})


@extend_schema(
    tags=["Authentication"], summary="Current API identity", responses={200: OpenApiTypes.OBJECT}
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser])
def me(request):
    return Response(
        {
            "data": {
                "id": request.user.id,
                "username": request.user.get_username(),
                "email": request.user.email,
            }
        }
    )


@extend_schema(
    tags=["Authentication"],
    summary="List or create API keys",
    request=APIKeyCreateSerializer,
    responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT},
)
@api_view(["GET", "POST"])
@authentication_classes([SessionAuthentication, BasicAuthentication])
@permission_classes([IsAuthenticated, IsApprovedUser])
def api_keys(request):
    if request.method == "GET":
        problem = _unsupported_query_params_problem(
            request, UNFILTERED_LIST_QUERY_PARAMS
        )
        if problem is not None:
            return problem
        return Response({"data": key_service.list_for_user(request.user)})

    serializer = APIKeyCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    key, raw_key = key_service.issue(user=request.user, **serializer.validated_data)
    return Response(
        {
            "data": {
                "id": key["id"],
                "name": key["name"],
                "prefix": key["prefix"],
                "scopes": key["scopes"],
                "key": raw_key,
                "expires_at": key["expires_at"],
            }
        },
        status=201,
    )


@extend_schema(
    tags=["Authentication"],
    summary="Revoke an API key",
    description="Revokes one of the current user's keys. Revocation is immediate.",
    responses={204: None},
)
@api_view(["DELETE"])
@authentication_classes([SessionAuthentication, BasicAuthentication])
@permission_classes([IsAuthenticated, IsApprovedUser])
def api_key_detail(request, key_id):
    if not key_service.revoke(user=request.user, key_id=key_id):
        return _problem(
            request, status_code=404, title="Not Found", detail="API key was not found."
        )
    return Response(status=204)


@extend_schema(
    tags=["Validation"],
    summary="Validate and normalize a catalog record",
    description=(
        "Runs the same normalization used by LOOP's Add Data forms without "
        "writing to MongoDB."
    ),
    request=RecordValidationSerializer,
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataWriteScope])
def validate_record(request):
    serializer = RecordValidationSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    record_type = serializer.validated_data["record_type"]
    record = serializer.validated_data["record"]
    material_auid = serializer.validated_data.get("material_auid")

    locked_elements = None
    locked_structure = None
    if material_auid:
        material = Material.objects(id=material_auid).first()
        if material is None or not _is_visible_to_user(
            material.default_visibility_affiliations,
            _user_affiliations(request.user),
        ):
            return _problem(
                request,
                status_code=404,
                title="Not Found",
                detail=f"Material {material_auid} was not found.",
            )
        locked_elements = material.elements
        locked_structure = material.structure_family

    if record_type == "computational":
        computational_serializer = ComputationalCreateSerializer(data=record)
        if computational_serializer.is_valid():
            return Response(
                {
                    "data": {
                        "accepted": True,
                        "errors": [],
                        "normalized": computational_serializer.validated_data,
                    }
                }
            )
        return Response(
            {
                "data": {
                    "accepted": False,
                    "errors": computational_serializer.errors,
                    "normalized": {},
                }
            }
        )

    normalized, errors = batch_upload_mod.normalize_record(
        record_type,
        record,
        locked_elements=locked_elements,
        locked_structure=locked_structure,
    )
    return Response(
        {
            "data": {
                "accepted": not errors,
                "errors": errors,
                "normalized": normalized,
            }
        }
    )


@extend_schema(
    tags=["Imports"],
    summary="Validate or import multiple records",
    description=(
        "Processes up to 100 records using the same validation and persistence "
        "paths as the single-record endpoints. Set dry_run=true to validate "
        "without writing. Records are independent; a mixed result returns 207."
    ),
    request=ImportSerializer,
    responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT, 207: OpenApiTypes.OBJECT},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasImportWriteScope])
def imports(request):
    serializer = ImportSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    record_type = serializer.validated_data["record_type"]
    records = serializer.validated_data["records"]
    dry_run = serializer.validated_data["dry_run"]
    serializers_by_type = {
        "experiment": ExperimentCreateSerializer,
        "literature": LiteratureCreateSerializer,
        "computational": ComputationalCreateSerializer,
    }

    rows = []
    created = 0
    failed = 0
    for index, record in enumerate(records):
        record_serializer = serializers_by_type[record_type](data=record)
        if not record_serializer.is_valid():
            failed += 1
            rows.append(
                {"index": index, "status": "rejected", "errors": record_serializer.errors}
            )
            continue
        payload = record_serializer.validated_data
        if dry_run:
            if record_type == "computational":
                errors = []
            else:
                _, errors = batch_upload_mod.normalize_record(record_type, payload)
            if errors:
                failed += 1
                rows.append({"index": index, "status": "rejected", "errors": errors})
            else:
                rows.append({"index": index, "status": "validated", "errors": []})
            continue

        try:
            if record_type == "experiment":
                result, errors = record_service.create_experiment(
                    actor=request.user, request=request, payload=payload
                )
            elif record_type == "literature":
                result, errors = record_service.create_literature(
                    actor=request.user, payload=payload
                )
            else:
                result = record_service.create_computational(
                    actor=request.user, payload=payload
                )
                errors = []
        except Exception as exc:
            result, errors = None, [str(exc)]

        if errors:
            failed += 1
            rows.append({"index": index, "status": "rejected", "errors": errors})
        else:
            created += 1
            rows.append({"index": index, "status": "created", "record": result})

    meta = {
        "record_type": record_type,
        "dry_run": dry_run,
        "total": len(records),
        "created": created,
        "failed": failed,
        "validated": len(records) - failed if dry_run else 0,
    }
    if dry_run:
        status_code = 200
    elif failed and created:
        status_code = 207
    elif failed:
        status_code = 422
    else:
        status_code = 201
    return Response({"data": rows, "meta": meta}, status=status_code)


def _problem(request, *, status_code, title, detail, errors=None):
    body = problem_body(
        status_code=status_code,
        title=title,
        detail=detail,
        instance=request.path,
        errors=errors,
    )
    force_json_rendering(request)
    return Response(body, status=status_code, content_type=PROBLEM_CONTENT_TYPE)


def _analysis_problem(request, *, status_code, title, detail, code):
    return _problem(
        request,
        status_code=status_code,
        title=title,
        detail=detail,
        errors=[{"code": code}],
    )


# Query parameters every list endpoint tolerates because they are transport
# concerns rather than filters: "format" drives DRF content negotiation.
UNIVERSAL_LIST_QUERY_PARAMS = frozenset({"format"})

# Every list endpoint pages the same way, so the two paging knobs are declared
# once and unioned into each endpoint's own parameter set.
PAGINATION_QUERY_PARAMS = frozenset({"limit", "offset"})

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100

# The schema counterpart of PAGINATION_QUERY_PARAMS. Declared alongside it so a
# generated client is offered exactly the knobs the endpoint accepts -- and, via
# LIST_ERROR_RESPONSES, is given a type for the 400 that anything else now earns.
PAGINATION_PARAMETERS = [
    OpenApiParameter(
        "limit", int, description=f"Page size, 1-{MAX_PAGE_LIMIT}. Defaults to {DEFAULT_PAGE_LIMIT}."
    ),
    OpenApiParameter("offset", int, description="Rows to skip before the page."),
]

LIST_ERROR_RESPONSES = {
    400: OpenApiResponse(
        response=OpenApiTypes.OBJECT,
        description=(
            "application/problem+json. An unsupported query parameter, a "
            "non-integer limit or offset, a limit below 1, or a negative offset."
        ),
    )
}

# Declared per endpoint so an unrecognised filter can be rejected instead of
# silently dropped -- a caller who mistypes a filter would otherwise analyse an
# unfiltered population with nothing in the response to show it.
MATERIALS_QUERY_PARAMS = (
    frozenset({"structure_family", "elements"}) | PAGINATION_QUERY_PARAMS
)
EXPERIMENTS_QUERY_PARAMS = (
    frozenset({"material_auid", "recipe_auid"}) | PAGINATION_QUERY_PARAMS
)
LITERATURE_QUERY_PARAMS = (
    frozenset({"material_auid", "recipe_auid", "doi"}) | PAGINATION_QUERY_PARAMS
)
COMPUTATIONAL_QUERY_PARAMS = frozenset({"material_auid"}) | PAGINATION_QUERY_PARAMS
PRECURSORS_QUERY_PARAMS = PAGINATION_QUERY_PARAMS
PROTOCOLS_QUERY_PARAMS = PAGINATION_QUERY_PARAMS
# The sub-resource lists and the key list return their whole visible set, so
# they take no filter and no paging knob. Declaring that empties the allowlist
# rather than skipping the check: an endpoint that ignores "offset" is the same
# hazard as one that ignores a filter, and the rejection tells a caller that
# paging is not available here instead of handing back a page-one-shaped answer.
UNFILTERED_LIST_QUERY_PARAMS = frozenset()


def _unsupported_query_params_problem(request, supported):
    """Return a 400 problem naming every query parameter the endpoint cannot honor."""
    allowed = frozenset(supported) | UNIVERSAL_LIST_QUERY_PARAMS
    unsupported = sorted(set(request.query_params) - allowed)
    if not unsupported:
        return None
    return _problem(
        request,
        status_code=400,
        title="Bad Request",
        detail=(
            "Unsupported query parameter(s): "
            f"{', '.join(unsupported)}. This endpoint would have ignored them."
        ),
        errors={
            "code": "unsupported_query_parameters",
            "unsupported": unsupported,
            "supported": sorted(allowed),
        },
    )


def _invalid_pagination_problem(request, *, name, value, expected):
    return _problem(
        request,
        status_code=400,
        title="Bad Request",
        detail=f"Query parameter {name!r} must be {expected}; received {value!r}.",
        errors={"code": f"invalid_{name}", "parameter": name, "received": value},
    )


def _pagination_or_problem(request):
    """Resolve ``limit``/``offset`` into a page window, or a 400 problem.

    Returns a ``(window, problem)`` pair so callers bail out before touching the
    database. Asking for fewer than one row is a caller mistake with no sensible
    reading, so it is reported rather than quietly turned into one row; asking
    for more than MAX_PAGE_LIMIT is a request the server deliberately caps, and
    ``meta.limit`` reports the size actually served.
    """
    raw_limit = request.query_params.get("limit", DEFAULT_PAGE_LIMIT)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = None
    if limit is None or limit < 1:
        return None, _invalid_pagination_problem(
            request, name="limit", value=raw_limit, expected="a positive integer"
        )
    limit = min(limit, MAX_PAGE_LIMIT)

    raw_offset = request.query_params.get("offset", 0)
    try:
        offset = int(raw_offset)
    except (TypeError, ValueError):
        offset = None
    if offset is None or offset < 0:
        return None, _invalid_pagination_problem(
            request, name="offset", value=raw_offset, expected="a non-negative integer"
        )
    return (limit, offset), None


def _paginate(rows, *, limit, offset):
    """Take one page from a lazy stream of already-filtered rows.

    The stream is consumed in Python rather than skipped in the database because
    these list views decide visibility -- and, for literature, DOI matching --
    per row after loading it; a database-level skip would count rows the caller
    is never allowed to see. One row past the page is pulled so ``has_more`` can
    be reported without walking the whole population.
    """
    page = []
    has_more = False
    for index, row in enumerate(rows):
        if index < offset:
            continue
        if len(page) >= limit:
            has_more = True
            break
        page.append(row)
    return page, has_more


def _count_scan(queryset, fields):
    """Stream ``fields`` of every document matching ``queryset``, as plain dicts.

    A total has to touch the whole matching population, so this is the cheapest
    shape available while visibility is still decided per row in Python: raw
    dicts with one projected field rather than hydrated documents. The page's
    sort is dropped because a count does not depend on order, which also keeps a
    large unindexed sort out of the request.

    It is still the dominant cost of any request that uses it, and it is paid
    again on every page. Measured against 35,576 materials / 46,778 embedded
    calculations by stubbing the count out and re-timing the same request:
    /materials/?limit=5 goes from ~7 ms to ~75 ms, /computational/?limit=5 from
    ~7 ms to ~125 ms -- roughly ten and seventeen times the rest of the request.
    /experiments/ and /literature/ scan only recipes and pay under 2 ms;
    /precursors/ and /protocols/ count server-side and never come through here.

    Making this affordable means counting in the database, which means
    expressing ``catalog.permissions.is_visible_to_user`` as a query -- it
    normalises stored tags, defaults an empty list to VISIBILITY_DEFAULT and
    strips "S4E" from the item side before matching, so a hand-written ``$in``
    would not agree with the rows. Until that predicate is query-shaped, a total
    on /materials/ and /computational/ costs a full scan; ``_paginate`` derives
    ``has_more`` independently, so those two could drop or gate ``total``
    without losing it.
    """
    return queryset.order_by().only(*fields).as_pymongo()


def _visible_total(queryset, field, affiliations):
    """Count the documents behind a page that this viewer is allowed to see.

    Visibility is decided in Python against the same predicate the page uses,
    rather than translated into a query, so a total can never disagree with the
    rows about what "visible" means -- and never advertise records the viewer
    would be refused.
    """
    return sum(
        1
        for doc in _count_scan(queryset, [field])
        if _is_visible_to_user(doc.get(field), affiliations)
    )


def _visible_embedded_total(queryset, list_field, affiliations, *, fields=(), keep=None):
    """Count embedded rows across parent documents, without loading the parents.

    ``keep`` applies whatever filter the list view resolves per embedded row, so
    the total counts exactly the population the page draws from; ``fields`` names
    the sub-fields that filter reads.
    """
    projection = [f"{list_field}.visibility_affiliations"]
    projection += [f"{list_field}.{name}" for name in fields]
    total = 0
    for doc in _count_scan(queryset, projection):
        for item in doc.get(list_field) or []:
            if keep is not None and not keep(item):
                continue
            if _is_visible_to_user(item.get("visibility_affiliations"), affiliations):
                total += 1
    return total


def _page_meta(limit, offset, rows, has_more, total, filters_applied=None):
    meta = {
        "limit": limit,
        "offset": offset,
        "returned": len(rows),
        "total": total,
        "has_more": has_more,
    }
    if filters_applied is not None:
        meta["filters_applied"] = filters_applied
    return meta


def _unpaged_meta(rows):
    """Meta for a list that always returns its whole visible set.

    ``limit`` and ``offset`` are absent because this endpoint has no page
    window to report, but ``filters_applied`` is present and empty so that
    reading it is safe on every list response the API serves.
    """
    return {
        "returned": len(rows),
        "total": len(rows),
        "has_more": False,
        "filters_applied": {},
    }


def _record_payload(request):
    """Accept a direct JSON body or multipart ``record`` JSON plus attachments."""
    if "record" not in request.data:
        return request.data
    raw = request.data.get("record")
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValidationError({"record": [f"Invalid JSON: {exc}"]}) from exc
    if not isinstance(parsed, dict):
        raise ValidationError({"record": ["Must contain one JSON object."]})
    return parsed


def _external_lookup_response(request, legacy_response, service_name):
    try:
        payload = json.loads(legacy_response.content)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if legacy_response.status_code >= 400:
        return _problem(
            request,
            status_code=legacy_response.status_code,
            title=f"{service_name} lookup failed",
            detail=payload.get("error") or f"{service_name} did not return usable data.",
        )
    return Response({"data": payload}, status=legacy_response.status_code)


@extend_schema(
    tags=["Add Data helpers"],
    summary="Normalize a material composition",
    request=CompositionNormalizeSerializer,
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def normalize_composition(request):
    serializer = CompositionNormalizeSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    elements = serializer.validated_data["elements"]
    structure_family = serializer.validated_data["structure_family"]
    material_auid = compute_material_auid(elements, structure_family)
    material = Material.objects(id=material_auid).only(
        "id", "default_visibility_affiliations"
    ).first()
    exists = bool(
        material is not None
        and _is_visible_to_user(
            material.default_visibility_affiliations,
            _user_affiliations(request.user),
        )
    )
    return Response(
        {
            "data": {
                "material_auid": material_auid,
                "elements": elements,
                "element_symbols": sorted(elements),
                "structure_family": structure_family,
                "exists": exists,
            }
        }
    )


@extend_schema(
    tags=["Add Data helpers"],
    summary="Find an existing DOI mapping in LOOP",
    parameters=[OpenApiParameter("doi", str, required=True)],
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def doi_lookup(request):
    doi = (request.query_params.get("doi") or "").strip()
    if not doi:
        raise ValidationError({"doi": ["This query parameter is required."]})
    mapping = DOIMapping.objects(doi=doi).first()
    return Response(
        {
            "data": {
                "found": mapping is not None,
                "doi": doi,
                "material_auids": list(mapping.material_auids or []) if mapping else [],
                "title": mapping.title if mapping else "",
            }
        }
    )


@extend_schema(
    tags=["Add Data helpers"],
    summary="Fetch DOI metadata from Crossref",
    parameters=[OpenApiParameter("doi", str, required=True)],
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def doi_metadata(request):
    request._request.user = request.user
    return _external_lookup_response(
        request,
        catalog_views.fetch_doi_metadata(request._request),
        "Crossref",
    )


@extend_schema(
    tags=["Precursors"],
    summary="Resolve a CAS number with PubChem",
    parameters=[OpenApiParameter("cas", str, required=True)],
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def precursor_cas_lookup(request):
    request._request.user = request.user
    return _external_lookup_response(
        request,
        catalog_views.precursors_cas_lookup(request._request),
        "PubChem",
    )


def _precursor_collision_problem(request, collision):
    return _problem(
        request,
        status_code=409,
        title="Conflict",
        detail=f"A visible precursor already uses CAS {collision.cas_number}.",
        errors={
            "code": "duplicate_cas",
            "existing": collision.to_public_dict(viewer_user_id=request.user.id),
        },
    )


@extend_schema_view(
    get=extend_schema(
        tags=["Precursors"],
        operation_id="precursors_list",
        summary="List visible precursors",
        description="Unrecognised query parameters are rejected with 400.",
        parameters=PAGINATION_PARAMETERS,
        responses={200: OpenApiTypes.OBJECT, **LIST_ERROR_RESPONSES},
    ),
    post=extend_schema(
        tags=["Precursors"],
        operation_id="precursors_create",
        summary="Create a precursor",
        request=PrecursorWriteSerializer,
        responses={201: OpenApiTypes.OBJECT},
    ),
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasCatalogScope])
def precursors(request):
    if request.method == "GET":
        problem = _unsupported_query_params_problem(request, PRECURSORS_QUERY_PARAMS)
        if problem is not None:
            return problem
        window, problem = _pagination_or_problem(request)
        if problem is not None:
            return problem
        limit, offset = window
        # Precursor visibility is entirely in the query, so the total is a
        # server-side count rather than a scan.
        visible = _visible_precursors_qs(request.user)
        total = visible.count()
        rows, has_more = _paginate(
            (
                item.to_public_dict(viewer_user_id=request.user.id)
                for item in visible.order_by("name")
            ),
            limit=limit,
            offset=offset,
        )
        return Response(
            {"data": rows, "meta": _page_meta(limit, offset, rows, has_more, total, {})}
        )

    serializer = PrecursorWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payload = serializer.validated_data
    cas_raw = (payload.get("cas_number") or "").strip()
    cas_number = _normalize_cas(cas_raw) if cas_raw else ""
    if cas_raw and not cas_number:
        raise ValidationError({"cas_number": ["Expected a value such as 7440-50-8."]})
    if cas_number and not payload.get("force"):
        collision = _find_precursor_collision(request.user, cas_number)
        if collision is not None:
            return _precursor_collision_problem(request, collision)
    item = UserPrecursor(
        user_id=request.user.id,
        uploaded_by_username=request.user.username,
        visibility_affiliations=_get_visibility_affiliations_for_create(request.user),
        name=payload["name"].strip(),
        formula=(payload.get("formula") or "").strip() or None,
        cas_number=cas_number or None,
        purity=(payload.get("purity") or "").strip() or None,
        supplier=(payload.get("supplier") or "").strip() or None,
        notes=(payload.get("notes") or "").strip() or None,
    ).save()
    return Response(
        {"data": item.to_public_dict(viewer_user_id=request.user.id)}, status=201
    )


@extend_schema_view(
    get=extend_schema(tags=["Precursors"], operation_id="precursors_retrieve", summary="Retrieve a precursor", responses={200: OpenApiTypes.OBJECT}),
    patch=extend_schema(tags=["Precursors"], operation_id="precursors_update", summary="Update a precursor", request=PrecursorWriteSerializer, responses={200: OpenApiTypes.OBJECT}),
    delete=extend_schema(tags=["Precursors"], operation_id="precursors_delete", summary="Delete a precursor", responses={204: None}),
)
@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasCatalogScope])
def precursor_detail(request, precursor_id):
    item = _get_visible_precursor(precursor_id, request.user)
    if item is None:
        return _problem(request, status_code=404, title="Not Found", detail="Precursor was not found.")
    if request.method == "GET":
        return Response({"data": item.to_public_dict(viewer_user_id=request.user.id)})
    if not _user_can_edit_precursor(request.user, item):
        return _problem(request, status_code=403, title="Forbidden", detail="Only the uploader may modify this precursor.")
    if request.method == "DELETE":
        item.delete()
        return Response(status=204)

    payload = item.to_public_dict(viewer_user_id=request.user.id)
    payload.update(request.data)
    serializer = PrecursorWriteSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    cas_raw = (data.get("cas_number") or "").strip()
    cas_number = _normalize_cas(cas_raw) if cas_raw else ""
    if cas_raw and not cas_number:
        raise ValidationError({"cas_number": ["Expected a value such as 7440-50-8."]})
    if cas_number and cas_number != (item.cas_number or "") and not data.get("force"):
        collision = _find_precursor_collision(
            request.user, cas_number, exclude_id=str(item.id)
        )
        if collision is not None:
            return _precursor_collision_problem(request, collision)
    item.name = data["name"].strip()
    item.formula = (data.get("formula") or "").strip() or None
    item.cas_number = cas_number or None
    item.purity = (data.get("purity") or "").strip() or None
    item.supplier = (data.get("supplier") or "").strip() or None
    item.notes = (data.get("notes") or "").strip() or None
    item.save()
    return Response({"data": item.to_public_dict(viewer_user_id=request.user.id)})


@extend_schema_view(
    get=extend_schema(
        tags=["Protocols"],
        operation_id="protocols_list",
        summary="List visible protocols",
        description="Unrecognised query parameters are rejected with 400.",
        parameters=PAGINATION_PARAMETERS,
        responses={200: OpenApiTypes.OBJECT, **LIST_ERROR_RESPONSES},
    ),
    post=extend_schema(tags=["Protocols"], operation_id="protocols_create", summary="Create a protocol", request=ProtocolWriteSerializer, responses={201: OpenApiTypes.OBJECT}),
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasCatalogScope])
def protocols(request):
    if request.method == "GET":
        problem = _unsupported_query_params_problem(request, PROTOCOLS_QUERY_PARAMS)
        if problem is not None:
            return problem
        window, problem = _pagination_or_problem(request)
        if problem is not None:
            return problem
        limit, offset = window
        visible = _visible_protocols_qs(request.user)
        total = visible.count()
        rows, has_more = _paginate(
            (
                item.to_public_dict(viewer_user_id=request.user.id)
                for item in visible.order_by("name")
            ),
            limit=limit,
            offset=offset,
        )
        return Response(
            {"data": rows, "meta": _page_meta(limit, offset, rows, has_more, total, {})}
        )
    serializer = ProtocolWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    payload = serializer.validated_data
    item = UserProtocol(
        user_id=request.user.id,
        uploaded_by_username=request.user.username,
        visibility_affiliations=_get_visibility_affiliations_for_create(request.user),
        name=payload["name"].strip(),
        description=(payload.get("description") or "").strip() or None,
        steps=payload["steps"],
    ).save()
    return Response({"data": item.to_public_dict(viewer_user_id=request.user.id)}, status=201)


@extend_schema_view(
    get=extend_schema(tags=["Protocols"], operation_id="protocols_retrieve", summary="Retrieve a protocol", responses={200: OpenApiTypes.OBJECT}),
    patch=extend_schema(tags=["Protocols"], operation_id="protocols_update", summary="Update a protocol", request=ProtocolWriteSerializer, responses={200: OpenApiTypes.OBJECT}),
    delete=extend_schema(tags=["Protocols"], operation_id="protocols_delete", summary="Delete a protocol", responses={204: None}),
)
@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasCatalogScope])
def protocol_detail(request, protocol_id):
    item = _get_visible_protocol(protocol_id, request.user)
    if item is None:
        return _problem(request, status_code=404, title="Not Found", detail="Protocol was not found.")
    if request.method == "GET":
        return Response({"data": item.to_public_dict(viewer_user_id=request.user.id)})
    if not _user_can_edit_protocol(request.user, item):
        return _problem(request, status_code=403, title="Forbidden", detail="Only the uploader may modify this protocol.")
    if request.method == "DELETE":
        item.delete()
        return Response(status=204)
    payload = item.to_public_dict(viewer_user_id=request.user.id)
    payload.update(request.data)
    serializer = ProtocolWriteSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    item.name = data["name"].strip()
    item.description = (data.get("description") or "").strip() or None
    item.steps = data["steps"]
    item.save()
    return Response({"data": item.to_public_dict(viewer_user_id=request.user.id)})


@extend_schema_view(
    get=extend_schema(
        tags=["Experiments"],
        summary="List experimental trials",
        description="Unrecognised query parameters are rejected with 400.",
        parameters=[
            OpenApiParameter("material_auid", str),
            OpenApiParameter("recipe_auid", str),
            *PAGINATION_PARAMETERS,
        ],
        responses={200: OpenApiTypes.OBJECT, **LIST_ERROR_RESPONSES},
    ),
    post=extend_schema(
        tags=["Experiments"],
        summary="Create an experimental trial",
        description=(
            "Creates or reuses the material and recipe, then stores a trial with "
            "the same normalization used by Add Data. Multipart requests use a "
            "record JSON field plus an optional csv_file."
        ),
        request={
            "application/json": ExperimentCreateSerializer,
            "multipart/form-data": ExperimentMultipartSerializer,
        },
        responses={201: OpenApiTypes.OBJECT},
    ),
)
@api_view(["GET", "POST"])
@permission_classes(
    [IsAuthenticated, IsApprovedUser, HasCatalogScope, HasFileWriteScope]
)
def experiments(request):
    if request.method == "GET":
        problem = _unsupported_query_params_problem(request, EXPERIMENTS_QUERY_PARAMS)
        if problem is not None:
            return problem
        qs = Recipe.objects.order_by("-created_at")
        material_auid = (request.query_params.get("material_auid") or "").strip()
        recipe_auid = (request.query_params.get("recipe_auid") or "").strip()
        filters_applied = {}
        if material_auid:
            qs = qs.filter(material_auid=material_auid)
            filters_applied["material_auid"] = material_auid
        if recipe_auid:
            qs = qs.filter(id=recipe_auid)
            filters_applied["recipe_auid"] = recipe_auid
        window, problem = _pagination_or_problem(request)
        if problem is not None:
            return problem
        limit, offset = window
        affiliations = _user_affiliations(request.user)

        def visible_trials():
            for recipe in qs:
                for trial in recipe.trials or []:
                    if not _is_visible_to_user(
                        trial.visibility_affiliations, affiliations
                    ):
                        continue
                    data = trial.to_mongo().to_dict()
                    data.pop("_id", None)
                    data.pop("exp_condition", None)
                    data["material_auid"] = recipe.material_auid
                    data["recipe_auid"] = recipe.id
                    data["synthesis_steps"] = list(recipe.synthesis_steps or [])
                    yield data

        total = _visible_embedded_total(qs, "trials", affiliations)
        rows, has_more = _paginate(visible_trials(), limit=limit, offset=offset)
        return Response(
            {
                "data": rows,
                "meta": _page_meta(
                    limit, offset, rows, has_more, total, filters_applied
                ),
            }
        )

    serializer = ExperimentCreateSerializer(data=_record_payload(request))
    serializer.is_valid(raise_exception=True)
    try:
        result, errors = record_service.create_experiment(
            actor=request.user,
            request=request,
            payload=serializer.validated_data,
            csv_file=request.FILES.get("csv_file"),
        )
    except (DuplicateFileError, DuplicateRecordError) as exc:
        return _problem(
            request,
            status_code=409,
            title="Conflict",
            detail=str(exc),
        )
    if errors:
        return _problem(
            request,
            status_code=422,
            title="Validation failed",
            detail="The experimental record could not be accepted.",
            errors=errors,
        )
    return Response(
        {
            "data": {
                "material_auid": result["material_auid"],
                "recipe_auid": result["recipe_auid"],
                "trial_id": result["trial_id"],
                "warnings": result.get("warnings", []),
            }
        },
        status=201,
    )


@extend_schema_view(
    get=extend_schema(
        tags=["Experiments"],
        summary="Retrieve an experimental trial",
        responses={200: OpenApiTypes.OBJECT},
    ),
    patch=extend_schema(
        tags=["Experiments"],
        summary="Update an experimental trial",
        request={
            "application/json": ExperimentCreateSerializer,
            "multipart/form-data": ExperimentMultipartSerializer,
        },
        responses={200: OpenApiTypes.OBJECT},
    ),
    delete=extend_schema(
        tags=["Experiments"],
        summary="Delete an experimental trial",
        responses={204: None},
    ),
)
@api_view(["GET", "PATCH", "DELETE"])
@permission_classes(
    [IsAuthenticated, IsApprovedUser, HasCatalogScope, HasFileWriteScope]
)
def experiment_detail(request, recipe_auid, trial_id):
    recipe = Recipe.objects(id=recipe_auid).first()
    if recipe is None:
        return _problem(
            request, status_code=404, title="Not Found", detail="Recipe was not found."
        )
    affiliations = _user_affiliations(request.user)
    for trial in recipe.trials or []:
        if trial.trial_id != trial_id:
            continue
        if not _is_visible_to_user(trial.visibility_affiliations, affiliations):
            break
        if request.method in ("PATCH", "DELETE") and not _is_uploader_or_superuser(
            request.user, trial.experimenter
        ):
            return _problem(
                request,
                status_code=403,
                title="Forbidden",
                detail="Only the uploader or a superuser may modify this trial.",
            )
        if request.method == "DELETE":
            record_service.delete_experiment(recipe=recipe, trial_id=trial_id)
            return Response(status=204)
        if request.method == "PATCH":
            condition = getattr(trial, "exp_condition", None)
            additional = getattr(condition, "additional_params", {}) or {}
            payload = {
                "elements": dict(recipe.elements or {}),
                "structure_family": recipe.structure_family,
                "phase_status": trial.phase_status,
                "synthesis_steps": additional.get(
                    "synthesis_steps", list(recipe.synthesis_steps or [])
                ),
                "spacegroup": trial.spacegroup or "unknown",
                "element_sites": dict(trial.element_sites or {}),
                "raw_data_type": trial.raw_data_type or "xrd",
                "comments": trial.notes or "na",
            }
            payload.update(_record_payload(request))
            serializer = ExperimentCreateSerializer(data=payload)
            serializer.is_valid(raise_exception=True)
            try:
                result, errors = record_service.update_experiment(
                    actor=request.user,
                    request=request,
                    recipe=recipe,
                    trial=trial,
                    payload=serializer.validated_data,
                    csv_file=request.FILES.get("csv_file"),
                )
            except (DuplicateFileError, DuplicateRecordError) as exc:
                return _problem(
                    request,
                    status_code=409,
                    title="Conflict",
                    detail=str(exc),
                )
            if errors:
                return _problem(
                    request,
                    status_code=422,
                    title="Validation failed",
                    detail="The experimental record could not be updated.",
                    errors=errors,
                )
            return Response(
                {
                    "data": {
                        "material_auid": result["material_auid"],
                        "recipe_auid": result["recipe_auid"],
                        "trial_id": result["trial_id"],
                        "warnings": result.get("warnings", []),
                    }
                }
            )
        data = trial.to_mongo().to_dict()
        data.pop("_id", None)
        condition = data.pop("exp_condition", {}) or {}
        additional = condition.get("additional_params", {}) or {}
        data["synthesis_steps"] = additional.get("synthesis_steps", [])
        data["material_auid"] = recipe.material_auid
        data["recipe_auid"] = recipe.id
        return Response({"data": data})
    return _problem(
        request, status_code=404, title="Not Found", detail="Trial was not found."
    )


@extend_schema(
    tags=["Literature"],
    summary="Create a literature synthesis record",
    description=(
        "Creates or reuses the material and recipe, then stores a DOI-backed "
        "literature record using Add Data normalization. GET rejects "
        "unrecognised query parameters with 400."
    ),
    parameters=[
        OpenApiParameter("material_auid", str),
        OpenApiParameter("recipe_auid", str),
        OpenApiParameter("doi", str),
        *PAGINATION_PARAMETERS,
    ],
    request=LiteratureCreateSerializer,
    responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT, **LIST_ERROR_RESPONSES},
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasCatalogScope])
def literature(request):
    if request.method == "GET":
        problem = _unsupported_query_params_problem(request, LITERATURE_QUERY_PARAMS)
        if problem is not None:
            return problem
        qs = Recipe.objects.order_by("-created_at")
        material_auid = (request.query_params.get("material_auid") or "").strip()
        recipe_auid = (request.query_params.get("recipe_auid") or "").strip()
        doi = (request.query_params.get("doi") or "").strip()
        filters_applied = {}
        if material_auid:
            qs = qs.filter(material_auid=material_auid)
            filters_applied["material_auid"] = material_auid
        if recipe_auid:
            qs = qs.filter(id=recipe_auid)
            filters_applied["recipe_auid"] = recipe_auid
        if doi:
            filters_applied["doi"] = doi
        window, problem = _pagination_or_problem(request)
        if problem is not None:
            return problem
        limit, offset = window
        affiliations = _user_affiliations(request.user)

        # The page walks documents and the total walks raw dicts, so the DOI
        # rule is written once and applied through both accessors.
        def doi_matches(value):
            return not doi or (value or "").lower() == doi.lower()

        def visible_literature():
            for recipe in qs:
                for item in recipe.literature or []:
                    if not doi_matches(item.doi):
                        continue
                    if not _is_visible_to_user(
                        item.visibility_affiliations, affiliations
                    ):
                        continue
                    data = item.to_mongo().to_dict()
                    data.pop("_id", None)
                    data.pop("exp_condition", None)
                    data["material_auid"] = recipe.material_auid
                    data["recipe_auid"] = recipe.id
                    data["synthesis_steps"] = list(recipe.synthesis_steps or [])
                    yield data

        total = _visible_embedded_total(
            qs,
            "literature",
            affiliations,
            fields=("doi",),
            keep=lambda item: doi_matches(item.get("doi")),
        )
        rows, has_more = _paginate(visible_literature(), limit=limit, offset=offset)
        return Response(
            {
                "data": rows,
                "meta": _page_meta(
                    limit, offset, rows, has_more, total, filters_applied
                ),
            }
        )

    serializer = LiteratureCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        result, errors = record_service.create_literature(
            actor=request.user, payload=serializer.validated_data
        )
    except (DuplicateFileError, DuplicateRecordError) as exc:
        return _problem(
            request,
            status_code=409,
            title="Conflict",
            detail=str(exc),
        )
    if errors:
        return _problem(
            request,
            status_code=422,
            title="Validation failed",
            detail="The literature record could not be accepted.",
            errors=errors,
        )
    return Response({"data": result}, status=201)


@extend_schema_view(
    get=extend_schema(
        tags=["Literature"],
        summary="Retrieve a literature synthesis record",
        responses={200: OpenApiTypes.OBJECT},
    ),
    patch=extend_schema(
        tags=["Literature"],
        summary="Update a literature synthesis record",
        request=LiteratureCreateSerializer,
        responses={200: OpenApiTypes.OBJECT},
    ),
    delete=extend_schema(
        tags=["Literature"],
        summary="Delete a literature synthesis record",
        responses={204: None},
    ),
)
@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasCatalogScope])
def literature_detail(request, recipe_auid, lit_id):
    recipe = Recipe.objects(id=recipe_auid).first()
    if recipe is None:
        return _problem(
            request, status_code=404, title="Not Found", detail="Recipe was not found."
        )
    affiliations = _user_affiliations(request.user)
    for item in recipe.literature or []:
        if item.lit_id != lit_id:
            continue
        if not _is_visible_to_user(item.visibility_affiliations, affiliations):
            break
        if request.method in ("PATCH", "DELETE") and not _is_uploader_or_superuser(
            request.user, item.extracted_by
        ):
            return _problem(
                request,
                status_code=403,
                title="Forbidden",
                detail="Only the uploader or a superuser may modify this literature record.",
            )
        if request.method == "DELETE":
            record_service.delete_literature(recipe=recipe, lit_id=lit_id)
            return Response(status=204)
        if request.method == "PATCH":
            condition = getattr(item, "exp_condition", None)
            additional = getattr(condition, "additional_params", {}) or {}
            payload = {
                "doi": item.doi,
                "synthesis_successful": item.synthesis_successful,
                "elements": dict(recipe.elements or {}),
                "structure_family": recipe.structure_family,
                "title": item.title or "na",
                "authors": list(item.authors or []),
                "journal": item.journal or "na",
                "year": item.year,
                "findings": item.notes or "na",
                "synthesis_steps": additional.get(
                    "synthesis_steps", list(recipe.synthesis_steps or [])
                ),
                "spacegroup": item.spacegroup or "unknown",
                "element_sites": dict(item.element_sites or {}),
            }
            payload.update(_record_payload(request))
            serializer = LiteratureCreateSerializer(data=payload)
            serializer.is_valid(raise_exception=True)
            result, errors = record_service.update_literature(
                actor=request.user,
                recipe=recipe,
                literature=item,
                payload=serializer.validated_data,
            )
            if errors:
                return _problem(
                    request,
                    status_code=422,
                    title="Validation failed",
                    detail="The literature record could not be updated.",
                    errors=errors,
                )
            return Response({"data": result})
        data = item.to_mongo().to_dict()
        data.pop("_id", None)
        condition = data.pop("exp_condition", {}) or {}
        additional = condition.get("additional_params", {}) or {}
        data["synthesis_steps"] = additional.get("synthesis_steps", [])
        data["material_auid"] = recipe.material_auid
        data["recipe_auid"] = recipe.id
        return Response({"data": data})
    return _problem(
        request, status_code=404, title="Not Found", detail="Literature record was not found."
    )


@extend_schema(
    tags=["Computational"],
    summary="Create a computational record",
    description=(
        "Creates or reuses a material and stores an embedded DFT calculation. "
        "GET rejects unrecognised query parameters with 400."
    ),
    parameters=[
        OpenApiParameter("material_auid", str),
        *PAGINATION_PARAMETERS,
    ],
    request=ComputationalCreateSerializer,
    responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT, **LIST_ERROR_RESPONSES},
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasCatalogScope])
def computational(request):
    if request.method == "GET":
        problem = _unsupported_query_params_problem(request, COMPUTATIONAL_QUERY_PARAMS)
        if problem is not None:
            return problem
        qs = Material.objects.order_by("-created_at")
        material_auid = (request.query_params.get("material_auid") or "").strip()
        filters_applied = {}
        if material_auid:
            qs = qs.filter(id=material_auid)
            filters_applied["material_auid"] = material_auid
        window, problem = _pagination_or_problem(request)
        if problem is not None:
            return problem
        limit, offset = window
        affiliations = _user_affiliations(request.user)

        def visible_calculations():
            for material in qs:
                for item in material.dft_calculations or []:
                    if not _is_visible_to_user(
                        item.visibility_affiliations, affiliations
                    ):
                        continue
                    data = item.to_mongo().to_dict()
                    data.pop("_id", None)
                    data["material_auid"] = material.id
                    yield data

        total = _visible_embedded_total(qs, "dft_calculations", affiliations)
        rows, has_more = _paginate(visible_calculations(), limit=limit, offset=offset)
        return Response(
            {
                "data": rows,
                "meta": _page_meta(
                    limit, offset, rows, has_more, total, filters_applied
                ),
            }
        )

    serializer = ComputationalCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        result = record_service.create_computational(
            actor=request.user, payload=serializer.validated_data
        )
    except DuplicateRecordError as exc:
        return _problem(
            request,
            status_code=409,
            title="Conflict",
            detail=str(exc),
        )
    except ValueError as exc:
        return _problem(
            request,
            status_code=422,
            title="Validation failed",
            detail="The computational record could not be accepted.",
            errors=[str(exc)],
        )
    return Response({"data": result}, status=201)


@extend_schema_view(
    get=extend_schema(
        tags=["Computational"],
        summary="Retrieve a computational record",
        responses={200: OpenApiTypes.OBJECT},
    ),
    patch=extend_schema(
        tags=["Computational"],
        summary="Update a computational record",
        request=ComputationalCreateSerializer,
        responses={200: OpenApiTypes.OBJECT},
    ),
    delete=extend_schema(
        tags=["Computational"],
        summary="Delete a computational record",
        responses={204: None},
    ),
)
@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasCatalogScope])
def computational_detail(request, material_auid, comp_auid):
    material = Material.objects(id=material_auid).first()
    if material is None:
        return _problem(
            request, status_code=404, title="Not Found", detail="Material was not found."
        )
    affiliations = _user_affiliations(request.user)
    for item in material.dft_calculations or []:
        if item.comp_auid != comp_auid:
            continue
        if not _is_visible_to_user(item.visibility_affiliations, affiliations):
            break
        if request.method in ("PATCH", "DELETE") and not _is_uploader_or_superuser(
            request.user, item.uploaded_by
        ):
            return _problem(
                request,
                status_code=403,
                title="Forbidden",
                detail="Only the uploader or a superuser may modify this computational record.",
            )
        if request.method == "DELETE":
            record_service.delete_computational(
                material=material, comp_auid=comp_auid
            )
            return Response(status=204)
        if request.method == "PATCH":
            metadata = dict(item.dft_metadata or {})
            payload = {
                "elements": dict(material.elements or {}),
                "structure_family": material.structure_family,
                "dft_source": item.dft_source or "",
                "calculation_method": metadata.get("calculation_method", ""),
                "functional": metadata.get("functional", ""),
                "pseudopotential": metadata.get("pseudopotential", ""),
                "k_points": metadata.get("k_points", ""),
                "cutoff_energy": metadata.get("cutoff_energy"),
                "formation_energy_ev": item.dft_formation_energy_ev,
                "hull_distance_ev": item.dft_hull_distance_ev,
                "bandgap_ev": item.dft_bandgap_ev,
                "bandgap_type": item.bandgap_type,
                "bandgap_fit_ev": item.bandgap_fit_ev,
                "bulk_modulus_vrh": item.bulk_modulus_vrh,
                "shear_modulus_vrh": item.shear_modulus_vrh,
                "youngs_modulus_vrh": item.youngs_modulus_vrh,
                "poisson_ratio": item.poisson_ratio,
                "elastic_anisotropy": item.elastic_anisotropy,
                "debye_temperature": item.debye_temperature,
                "thermal_conductivity_300k": item.thermal_conductivity_300k,
                "gruneisen_parameter": item.gruneisen_parameter,
                "thermal_expansion_300k": item.thermal_expansion_300k,
                "pearson_symbol": item.pearson_symbol,
                "crystal_system": item.crystal_system,
                "crystal_family": item.crystal_family,
                "spin_atom": item.spin_atom,
                "ml_predictions": dict(item.ml_predictions or {}),
                "extended_data": dict(item.extended_data or {}),
                "spacegroup": item.spacegroup or "unknown",
                "element_sites": dict(item.element_sites or {}),
            }
            payload.update(_record_payload(request))
            serializer = ComputationalCreateSerializer(data=payload)
            serializer.is_valid(raise_exception=True)
            result = record_service.update_computational(
                actor=request.user,
                material=material,
                computation=item,
                payload=serializer.validated_data,
            )
            return Response({"data": result})
        data = item.to_mongo().to_dict()
        data.pop("_id", None)
        data["material_auid"] = material.id
        return Response({"data": data})
    return _problem(
        request, status_code=404, title="Not Found", detail="Computational record was not found."
    )


def _material_data(material):
    return {
        "material_auid": material.id,
        "elements": dict(material.elements or {}),
        "element_symbols": list(material.element_symbols or []),
        "num_elements": material.num_elements or len(material.elements or {}),
        "structure_family": material.structure_family,
        "display_name": material.display_name,
        "notes": material.notes,
        "curator": material.curator,
        "visibility_affiliations": list(material.default_visibility_affiliations or []),
        "created_at": material.created_at,
        "updated_at": material.updated_at,
    }


def _trial_data(recipe, trial):
    """Return the public representation shared by trial reads and exports."""
    data = trial.to_mongo().to_dict()
    data.pop("_id", None)
    condition = data.pop("exp_condition", {}) or {}
    additional = condition.get("additional_params", {}) or {}
    data["material_auid"] = recipe.material_auid
    data["recipe_auid"] = recipe.id
    data["synthesis_steps"] = additional.get(
        "synthesis_steps", list(recipe.synthesis_steps or [])
    )
    return data


def _visible_recipe_or_problem(request, recipe_auid):
    recipe = Recipe.objects(id=recipe_auid).first()
    if recipe is None or not _is_visible_to_user(
        recipe.visibility_affiliations, _user_affiliations(request.user)
    ):
        return None, _problem(
            request, status_code=404, title="Not Found", detail="Recipe was not found."
        )
    return recipe, None


def _recipe_data(recipe, affiliations):
    return {
        "recipe_auid": recipe.id,
        "material_auid": recipe.material_auid,
        "elements": dict(recipe.elements or {}),
        "structure_family": recipe.structure_family,
        "synthesis_steps": list(recipe.synthesis_steps or []),
        "trials": [
            _trial_data(recipe, trial)
            for trial in recipe.trials or []
            if _is_visible_to_user(trial.visibility_affiliations, affiliations)
        ],
        "literature_count": sum(
            1
            for item in recipe.literature or []
            if _is_visible_to_user(item.visibility_affiliations, affiliations)
        ),
        "created_at": recipe.created_at,
        "updated_at": recipe.updated_at,
    }


def _json_attachment(data, filename):
    response = Response({"data": data})
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _visible_trial_or_problem(request, recipe_auid, trial_id):
    recipe, problem = _visible_recipe_or_problem(request, recipe_auid)
    if problem:
        return None, None, problem
    affiliations = _user_affiliations(request.user)
    for trial in recipe.trials or []:
        if trial.trial_id == trial_id and _is_visible_to_user(
            trial.visibility_affiliations, affiliations
        ):
            return recipe, trial, None
    return None, None, _problem(
        request, status_code=404, title="Not Found", detail="Trial was not found."
    )


def _trial_xrd_path(recipe, trial):
    """Resolve the stored raw XRD file for a trial via the canonical store.

    Falls back to the pre-xrd_store MEDIA_ROOT layout so records uploaded
    before the storage refactor stay downloadable.
    """
    from catalog import xrd_store

    resolved = xrd_store.resolve_raw_path(recipe.id, trial.trial_id)
    if resolved:
        path = Path(resolved)
        if path.is_file():
            return path
    candidate = (
        Path(settings.MEDIA_ROOT)
        / "xrd_data"
        / recipe.material_auid
        / f"{trial.trial_id}.csv"
    )
    return candidate if candidate.is_file() else None


def _iso_or_none(value):
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _job_status_url(job):
    return reverse("api-v1-xrd-analysis-job-detail", kwargs={"job_id": str(job.id)})


def _analysis_result_url(job):
    return reverse("api-v1-xrd-analysis-result", kwargs={"analysis_id": str(job.analysis_id)})


def _analysis_artifact_url(job, artifact_name):
    return reverse(
        "api-v1-xrd-analysis-artifact",
        kwargs={"analysis_id": str(job.analysis_id), "artifact_name": artifact_name},
    )


def _job_by_id(job_id):
    try:
        return XRDAnalysisJob.objects(id=job_id).first()
    except Exception:
        return None


def _job_by_analysis_id(analysis_id):
    return XRDAnalysisJob.objects(analysis_id=analysis_id).first()


def _visible_analysis_job_or_problem(request, *, job_id=None, analysis_id=None):
    job = _job_by_id(job_id) if job_id is not None else _job_by_analysis_id(analysis_id)
    if job is None:
        return None, None, None, _analysis_problem(
            request,
            status_code=404,
            title="Not Found",
            detail="XRD analysis job was not found.",
            code="analysis_job_not_found" if job_id is not None else "analysis_result_not_found",
        )
    recipe, trial, problem = _visible_trial_or_problem(request, job.recipe_auid, job.trial_id)
    if problem:
        return None, None, None, _analysis_problem(
            request,
            status_code=404,
            title="Not Found",
            detail="The requested XRD analysis is not visible.",
            code="analysis_access_denied",
        )
    return job, recipe, trial, None


def _artifact_metadata_rows(job, persisted):
    rows = []
    for artifact in persisted.persisted_artifacts:
        if isinstance(artifact, dict):
            relative_path = str(artifact.get("relative_path") or "")
            artifact_type = artifact.get("artifact_type")
            content_type = artifact.get("content_type")
            size_bytes = artifact.get("size_bytes")
            sha256 = artifact.get("sha256")
        else:
            relative_path = str(getattr(artifact, "relative_path", ""))
            artifact_type = getattr(artifact, "artifact_type", None)
            content_type = getattr(artifact, "content_type", None)
            size_bytes = getattr(artifact, "size_bytes", None)
            sha256 = getattr(artifact, "sha256", None)
        name = Path(relative_path).name
        if name not in allowed_analysis_artifact_names():
            continue
        rows.append(
            {
                "name": name,
                "artifact_type": artifact_type,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "download_url": _analysis_artifact_url(job, name),
            }
        )
    rows.sort(key=lambda item: item["name"])
    return rows


def _artifact_relative_path_for_name(persisted, artifact_name):
    for artifact in persisted.persisted_artifacts:
        relative_path = (
            artifact.get("relative_path")
            if isinstance(artifact, dict)
            else getattr(artifact, "relative_path", None)
        )
        if relative_path and Path(str(relative_path)).name == artifact_name:
            return str(relative_path)
    return None


def _job_payload(job):
    return {
        "job_id": str(job.id),
        "analysis_id": job.analysis_id,
        "status": job.status,
        "cache_hit": bool(job.cache_hit),
        "progress_stage": job.progress_stage,
        "progress_message": job.progress_message,
        "attempt_count": int(job.attempt_count or 0),
        "maximum_attempts": int(job.maximum_attempts or 0),
        "created_at": _iso_or_none(job.created_at),
        "queued_at": _iso_or_none(job.queued_at),
        "started_at": _iso_or_none(job.started_at),
        "last_heartbeat_at": _iso_or_none(job.last_heartbeat_at),
        "completed_at": _iso_or_none(job.completed_at),
        "warning_codes": [item.get("code") for item in (job.warnings or []) if item.get("code")],
        "warnings": list(job.warnings or []),
        "failure_codes": list(job.failure_codes or []),
        "error_summary": job.error_summary,
        "status_url": _job_status_url(job),
        "result_url": _analysis_result_url(job) if job.status == "succeeded" else None,
    }


@extend_schema(
    tags=["Materials"],
    summary="List materials",
    description=(
        "Filters materials by structure family and a comma-separated set of "
        "required element symbols. Results honor affiliation visibility. "
        "Unrecognised query parameters are rejected with 400."
    ),
    parameters=[
        OpenApiParameter("structure_family", str),
        OpenApiParameter("elements", str, description="Comma-separated element symbols."),
        *PAGINATION_PARAMETERS,
    ],
    responses={200: MaterialSerializer(many=True), **LIST_ERROR_RESPONSES},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def materials(request):
    problem = _unsupported_query_params_problem(request, MATERIALS_QUERY_PARAMS)
    if problem is not None:
        return problem
    qs = Material.objects.order_by("-created_at")
    filters_applied = {}
    structure_family = (request.query_params.get("structure_family") or "").strip()
    if structure_family:
        qs = qs.filter(structure_family=structure_family)
        filters_applied["structure_family"] = structure_family
    elements = [
        item.strip()
        for item in (request.query_params.get("elements") or "").split(",")
        if item.strip()
    ]
    if elements:
        qs = qs.filter(element_symbols__all=elements)
        filters_applied["elements"] = elements
    window, problem = _pagination_or_problem(request)
    if problem is not None:
        return problem
    limit, offset = window
    affiliations = _user_affiliations(request.user)
    visible = (
        _material_data(material)
        for material in qs
        if _is_visible_to_user(material.default_visibility_affiliations, affiliations)
    )
    total = _visible_total(qs, "default_visibility_affiliations", affiliations)
    rows, has_more = _paginate(visible, limit=limit, offset=offset)
    return Response(
        {
            "data": rows,
            "meta": _page_meta(limit, offset, rows, has_more, total, filters_applied),
        }
    )


@extend_schema(
    tags=["Materials"], summary="Retrieve a material", responses={200: MaterialSerializer}
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def material_detail(request, material_auid):
    material = Material.objects(id=material_auid).first()
    if material is None or not _is_visible_to_user(
        material.default_visibility_affiliations, _user_affiliations(request.user)
    ):
        return _problem(
            request, status_code=404, title="Not Found", detail="Material was not found."
        )
    return Response({"data": _material_data(material)})


@extend_schema(
    tags=["Recipes"],
    summary="List visible recipes for a material",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def material_recipes(request, material_auid):
    problem = _unsupported_query_params_problem(request, UNFILTERED_LIST_QUERY_PARAMS)
    if problem is not None:
        return problem
    material = Material.objects(id=material_auid).first()
    affiliations = _user_affiliations(request.user)
    if material is None or not _is_visible_to_user(
        material.default_visibility_affiliations, affiliations
    ):
        return _problem(
            request, status_code=404, title="Not Found", detail="Material was not found."
        )
    recipes = [
        _recipe_data(recipe, affiliations)
        for recipe in Recipe.objects(material_auid=material_auid).order_by("-created_at")
        if _is_visible_to_user(recipe.visibility_affiliations, affiliations)
    ]
    # Not paged: the whole visible set is always returned, so total mirrors it.
    return Response({"data": recipes, "meta": _unpaged_meta(recipes)})


@extend_schema(
    tags=["Downloads"],
    summary="Download a material and its visible recipes as JSON",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def material_download(request, material_auid):
    material = Material.objects(id=material_auid).first()
    affiliations = _user_affiliations(request.user)
    if material is None or not _is_visible_to_user(
        material.default_visibility_affiliations, affiliations
    ):
        return _problem(
            request, status_code=404, title="Not Found", detail="Material was not found."
        )
    payload = {
        "material": _material_data(material),
        "recipes": [
            _recipe_data(recipe, affiliations)
            for recipe in Recipe.objects(material_auid=material_auid).order_by("-created_at")
            if _is_visible_to_user(recipe.visibility_affiliations, affiliations)
        ],
    }
    return _json_attachment(payload, f"{material_auid.replace(':', '_')}.json")


@extend_schema(
    tags=["Recipes"], summary="Retrieve a recipe", responses={200: OpenApiTypes.OBJECT}
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def recipe_detail(request, recipe_auid):
    recipe, problem = _visible_recipe_or_problem(request, recipe_auid)
    if problem:
        return problem
    return Response({"data": _recipe_data(recipe, _user_affiliations(request.user))})


@extend_schema(
    tags=["Recipes"],
    operation_id="recipe_trials_list",
    summary="List visible trials for a recipe",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def recipe_trials(request, recipe_auid):
    problem = _unsupported_query_params_problem(request, UNFILTERED_LIST_QUERY_PARAMS)
    if problem is not None:
        return problem
    recipe, problem = _visible_recipe_or_problem(request, recipe_auid)
    if problem:
        return problem
    affiliations = _user_affiliations(request.user)
    rows = [
        _trial_data(recipe, trial)
        for trial in recipe.trials or []
        if _is_visible_to_user(trial.visibility_affiliations, affiliations)
    ]
    # Not paged: the whole visible set is always returned, so total mirrors it.
    return Response({"data": rows, "meta": _unpaged_meta(rows)})


@extend_schema(
    tags=["Downloads"], summary="Download a visible recipe as JSON", responses={200: OpenApiTypes.OBJECT}
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def recipe_download(request, recipe_auid):
    recipe, problem = _visible_recipe_or_problem(request, recipe_auid)
    if problem:
        return problem
    return _json_attachment(
        _recipe_data(recipe, _user_affiliations(request.user)),
        f"{recipe_auid.replace(':', '_')}.json",
    )


@extend_schema(
    tags=["Downloads"], summary="Download a visible experimental trial as JSON", responses={200: OpenApiTypes.OBJECT}
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def experiment_download(request, recipe_auid, trial_id):
    recipe, trial, problem = _visible_trial_or_problem(request, recipe_auid, trial_id)
    if problem:
        return problem
    return _json_attachment(_trial_data(recipe, trial), f"{trial_id}.json")


@extend_schema(
    tags=["XRD"], summary="Download a trial's original XRD CSV", responses={200: OpenApiTypes.BINARY}
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def experiment_xrd(request, recipe_auid, trial_id):
    recipe, trial, problem = _visible_trial_or_problem(request, recipe_auid, trial_id)
    if problem:
        return problem
    path = _trial_xrd_path(recipe, trial)
    if path is None:
        return _problem(
            request, status_code=404, title="Not Found", detail="XRD CSV was not found."
        )
    return FileResponse(path.open("rb"), as_attachment=True, filename=f"{trial_id}.csv")


@extend_schema(
    tags=["XRD"], summary="Retrieve parsed metadata for a trial's XRD CSV", responses={200: OpenApiTypes.OBJECT}
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def experiment_xrd_metadata(request, recipe_auid, trial_id):
    recipe, trial, problem = _visible_trial_or_problem(request, recipe_auid, trial_id)
    if problem:
        return problem
    path = _trial_xrd_path(recipe, trial)
    if path is None:
        return _problem(
            request, status_code=404, title="Not Found", detail="XRD CSV was not found."
        )
    try:
        metadata, _ = xrd_parse(path)
    except Exception as exc:
        return _problem(
            request, status_code=422, title="Invalid XRD CSV", detail=str(exc)
        )
    return Response({"data": {"metadata": metadata, "trial_id": trial_id}})


@extend_schema(
    tags=["XRD"],
    summary="Generate a preview for a trial's XRD CSV",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def experiment_xrd_preview(request, recipe_auid, trial_id):
    recipe, trial, problem = _visible_trial_or_problem(request, recipe_auid, trial_id)
    if problem:
        return problem
    path = _trial_xrd_path(recipe, trial)
    if path is None:
        return _problem(
            request, status_code=404, title="Not Found", detail="XRD CSV was not found."
        )
    try:
        metadata, dataframe = xrd_parse(path)
        image_data_uri = render_xrd_plot(dataframe, encode_base64=True)
    except Exception as exc:
        return _problem(
            request, status_code=422, title="Invalid XRD CSV", detail=str(exc)
        )
    return Response(
        {
            "data": {
                "trial_id": trial_id,
                "metadata": metadata,
                "image_data_uri": image_data_uri,
            }
        }
    )


@extend_schema(
    tags=["XRD"],
    summary="Submit a background XRD phase-analysis job for a visible trial",
    request=None,
    responses={200: OpenApiTypes.OBJECT, 202: OpenApiTypes.OBJECT},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def experiment_xrd_analysis_submit(request, recipe_auid, trial_id):
    recipe, trial, problem = _visible_trial_or_problem(request, recipe_auid, trial_id)
    if problem:
        return _analysis_problem(
            request,
            status_code=404,
            title="Not Found",
            detail="Trial was not found.",
            code="xrd_trial_not_found",
        )
    if _trial_xrd_path(recipe, trial) is None:
        return _analysis_problem(
            request,
            status_code=404,
            title="Not Found",
            detail="The trial does not have an accessible raw XRD file.",
            code="xrd_raw_file_not_found",
        )
    try:
        submission = submit_xrd_analysis_job(
            recipe_auid,
            trial_id,
            recipe=recipe,
            trial=trial,
        )
    except XRDAnalysisJobError as exc:
        if exc.status_code == 404:
            return _analysis_problem(
                request,
                status_code=404,
                title="Not Found",
                detail=exc.message,
                code=exc.code,
            )
        if exc.status_code == 409:
            return _analysis_problem(
                request,
                status_code=409,
                title="Conflict",
                detail=exc.message,
                code=exc.code,
            )
        return _analysis_problem(
            request,
            status_code=500,
            title="XRD Analysis Submission Failed",
            detail=exc.message,
            code=exc.code,
        )

    payload = _job_payload(submission.job)
    payload.update(
        {
            "analysis_id": submission.analysis_id,
            "cache_hit": submission.cache_hit,
            "active_job_reused": submission.reused_active_job,
            "requeued_failed_job": submission.requeued_failed_job,
            "result_available": submission.cache_hit or submission.status == "succeeded",
        }
    )
    return Response({"data": payload}, status=200 if submission.cache_hit else 202)


@extend_schema(
    tags=["XRD"],
    summary="Retrieve the status of a background XRD phase-analysis job",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def xrd_analysis_job_detail(request, job_id):
    job, _, _, problem = _visible_analysis_job_or_problem(request, job_id=job_id)
    if problem:
        return problem
    return Response({"data": _job_payload(job)})


@extend_schema(
    tags=["XRD"],
    summary="Retrieve a validated persisted XRD analysis result",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def xrd_analysis_result(request, analysis_id):
    job, recipe, trial, problem = _visible_analysis_job_or_problem(request, analysis_id=analysis_id)
    if problem:
        return problem
    try:
        context = assemble_repository_xrd_input(
            recipe.id,
            trial.trial_id,
            recipe=recipe,
            trial=trial,
            require_accessible_raw_file=False,
        )
    except XRDAnalysisJobError as exc:
        return _analysis_problem(
            request,
            status_code=404 if exc.status_code == 404 else 500,
            title="XRD Analysis Unavailable",
            detail=exc.message,
            code=exc.code,
        )
    validation = validate_persisted_xrd_analysis(
        context.analysis_input,
        analysis_id=analysis_id,
    )
    if not validation.valid:
        if validation.failure_code == "analysis_cache_not_found":
            return _analysis_problem(
                request,
                status_code=404,
                title="Not Found",
                detail="No persisted XRD analysis result was found for this identity.",
                code="analysis_result_not_found",
            )
        return _analysis_problem(
            request,
            status_code=409,
            title="Persisted Analysis Integrity Failure",
            detail=validation.detail or "The persisted XRD analysis artifacts failed validation.",
            code="analysis_result_integrity_failed",
        )
    persisted = load_persisted_xrd_analysis(
        context.analysis_input,
        analysis_id=analysis_id,
        cache_validation=validation,
    )
    payload = {
        "analysis_id": persisted.analysis_id,
        "job": _job_payload(job),
        "summary": persisted.summary,
        "result": persisted.result,
        "reproducibility_manifest": persisted.reproducibility_manifest,
        "artifacts": _artifact_metadata_rows(job, persisted),
        "persistence_warning_codes": list(getattr(persisted, "persistence_warning_codes", ()) or ()),
    }
    return Response({"data": payload})


@extend_schema(
    tags=["XRD"],
    summary="Download an authorized XRD analysis artifact",
    responses={200: OpenApiTypes.BINARY},
)
@api_view(["GET"])
@permission_classes([IsAuthenticated, IsApprovedUser, HasDataReadScope])
def xrd_analysis_artifact(request, analysis_id, artifact_name):
    job, recipe, trial, problem = _visible_analysis_job_or_problem(request, analysis_id=analysis_id)
    if problem:
        return problem
    try:
        normalized_name = normalize_artifact_name(artifact_name)
    except XRDAnalysisJobError:
        return _analysis_problem(
            request,
            status_code=404,
            title="Not Found",
            detail="Artifact name is not allowed.",
            code="analysis_result_not_found",
        )
    if normalized_name not in allowed_analysis_artifact_names():
        return _analysis_problem(
            request,
            status_code=404,
            title="Not Found",
            detail="Artifact was not found.",
            code="analysis_result_not_found",
        )
    try:
        context = assemble_repository_xrd_input(
            recipe.id,
            trial.trial_id,
            recipe=recipe,
            trial=trial,
            require_accessible_raw_file=False,
        )
    except XRDAnalysisJobError as exc:
        return _analysis_problem(
            request,
            status_code=404 if exc.status_code == 404 else 500,
            title="XRD Analysis Unavailable",
            detail=exc.message,
            code=exc.code,
        )
    validation = validate_persisted_xrd_analysis(
        context.analysis_input,
        analysis_id=analysis_id,
    )
    if not validation.valid:
        return _analysis_problem(
            request,
            status_code=409 if validation.failure_code != "analysis_cache_not_found" else 404,
            title="Persisted Analysis Integrity Failure"
            if validation.failure_code != "analysis_cache_not_found"
            else "Not Found",
            detail=validation.detail or "The requested XRD analysis artifact is unavailable.",
            code="analysis_result_integrity_failed"
            if validation.failure_code != "analysis_cache_not_found"
            else "analysis_result_not_found",
        )
    persisted = load_persisted_xrd_analysis(
        context.analysis_input,
        analysis_id=analysis_id,
        cache_validation=validation,
    )
    allowed = {item["name"]: item for item in _artifact_metadata_rows(job, persisted)}
    artifact = allowed.get(normalized_name)
    if artifact is None:
        return _analysis_problem(
            request,
            status_code=404,
            title="Not Found",
            detail="Artifact was not found in the validated persisted manifest.",
            code="analysis_result_not_found",
        )
    relative_path = _artifact_relative_path_for_name(persisted, normalized_name)
    target = Path(settings.MEDIA_ROOT) / str(relative_path or "")
    if not target.is_file():
        return _analysis_problem(
            request,
            status_code=409,
            title="Persisted Analysis Integrity Failure",
            detail="The validated manifest references a missing artifact file.",
            code="analysis_result_integrity_failed",
        )
    return FileResponse(target.open("rb"), as_attachment=True, filename=normalized_name)
