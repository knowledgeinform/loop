"""RFC 9457-style error rendering for the versioned API."""

from django.http import JsonResponse
from rest_framework.renderers import JSONRenderer
from rest_framework.views import exception_handler


PROBLEM_CONTENT_TYPE = "application/problem+json"

API_V1_PREFIX = "/api/v1/"


def is_api_v1_path(path):
    """Whether ``path`` addresses the versioned API.

    Takes the bare path so callers can pass ``request.path_info`` -- production
    mounts the site under ``DJANGO_SUBPATH``, which puts the prefix in
    ``request.path`` but not in ``path_info``. ``/api/v1`` counts even without
    its trailing slash: nothing routes it, so there is no redirect to fall back
    on and it would otherwise leave the API through the HTML 404 page.
    """
    return path == API_V1_PREFIX.rstrip("/") or path.startswith(API_V1_PREFIX)


def force_json_rendering(request):
    """Pin a DRF request to the JSON renderer so its error body is JSON.

    ``DEFAULT_RENDERER_CLASSES`` still includes the browsable API, so a caller
    sending a browser ``Accept`` negotiates HTML -- and ``finalize_response``
    copies the renderer off the request, overwriting anything set on the
    response. An error carrying the problem+json content type has to have a
    problem+json body, whatever the caller asked for, or a client that branches
    on the header before parsing breaks on the mismatch.
    """
    if request is None:
        return
    request.accepted_renderer = JSONRenderer()
    request.accepted_media_type = PROBLEM_CONTENT_TYPE


def problem_body(*, status_code, title, detail, instance, errors=None):
    """The one problem+json body every API error is rendered from.

    Defined here rather than in the views so that the DRF exception handler, the
    hand-built problems in ``catalog.api.views`` and the URL-resolver 404 all
    emit a shape a client can parse with a single code path.
    """
    body = {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": instance,
    }
    if errors is not None:
        body["errors"] = errors
    return body


def problem_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    detail = response.data
    if isinstance(detail, dict) and set(detail) == {"detail"}:
        message = str(detail["detail"])
        errors = None
    else:
        message = "One or more fields could not be accepted."
        errors = detail

    request = context.get("request")
    response.data = problem_body(
        status_code=response.status_code,
        title=response.status_text,
        detail=message,
        instance=request.path if request is not None else "",
        errors=errors,
    )
    response.content_type = PROBLEM_CONTENT_TYPE
    force_json_rendering(request)
    return response


# Collection names callers reach for that this API does not route under that
# name. The 404 body carries the correction, so a client that guessed wrong is
# redirected by the error itself instead of having to go and read the schema.
UNROUTED_COLLECTIONS = {
    "trials": (
        "Trial records are served by /api/v1/experiments/; a single trial is "
        "/api/v1/recipes/{recipe_auid}/trials/{trial_id}/.",
        "/api/v1/experiments/",
    ),
    "recipes": (
        "There is no recipe collection endpoint. List one material's recipes "
        "with /api/v1/materials/{material_auid}/recipes/, or retrieve a single "
        "recipe with /api/v1/recipes/{recipe_auid}/.",
        None,
    ),
}


def _unrouted_collection(path_info):
    remainder = path_info[len(API_V1_PREFIX):].strip("/")
    return UNROUTED_COLLECTIONS.get(remainder.split("/", 1)[0])


def api_not_found(request):
    """Render a 404 for an unrouted path under ``/api/v1/`` as problem+json.

    Reads ``path_info`` to recognise the collection, because under a deployment
    subpath ``request.path`` carries a prefix the API's own routes never see;
    ``path`` is still what the body reports back, since that is what the caller
    asked for.
    """
    guidance, endpoint = _unrouted_collection(request.path_info) or (
        "See /api/v1/openapi.json for the endpoints this API serves.",
        None,
    )
    errors = {"code": "unknown_endpoint"}
    if endpoint is not None:
        errors["endpoint"] = endpoint
    return JsonResponse(
        problem_body(
            status_code=404,
            title="Not Found",
            detail=f"No API endpoint at {request.path}. {guidance}",
            instance=request.path,
            errors=errors,
        ),
        status=404,
        content_type=PROBLEM_CONTENT_TYPE,
    )


def api_server_error(request):
    """Render an unhandled exception under ``/api/v1/`` as problem+json.

    DRF's ``exception_handler`` returns ``None`` for anything that is not an
    ``APIException``, so a bug inside a view left the API answering
    ``text/html`` -- the one status where a client is least able to absorb a
    parse error on top of the failure it is already dealing with.

    The detail is fixed text. This is the one error whose cause is an
    unhandled exception, so deriving the message from it risks putting an
    internal string in front of an unauthenticated caller; the traceback still
    reaches the logs through Django's usual ``got_request_exception`` path.
    """
    return JsonResponse(
        problem_body(
            status_code=500,
            title="Internal Server Error",
            detail=(
                "The API failed to complete this request. The failure has been "
                "logged. Retry; if it persists the request is hitting a bug."
            ),
            instance=request.path,
            errors={"code": "internal_error"},
        ),
        status=500,
        content_type=PROBLEM_CONTENT_TYPE,
    )
