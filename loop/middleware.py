from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.shortcuts import redirect
from django.contrib import messages
from django.http import JsonResponse

import re


def _extract_api_key(request):
    """Return the API token from ``Authorization: Bearer/Token`` or ``X-API-Key``, or None."""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    if header:
        parts = header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("bearer", "token"):
            return parts[1].strip()
    x_api_key = request.META.get("HTTP_X_API_KEY", "")
    return x_api_key.strip() or None


class ArchiveContextMiddleware:
    """Attribute archive writes made during this request to the signed-in user.

    The JSON archive journals an actor and a source for every change, but the
    write happens inside a MongoEngine signal handler with no access to the
    request. This middleware puts the attribution into a context variable that
    the writer reads. See :mod:`catalog.archive.context`.

    Must sit *after* the authentication middlewares so ``request.user`` is
    resolved, and after ``ApiTokenAuthMiddleware`` so an API key's owning user
    is credited rather than "anonymous".
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from catalog.api.exceptions import is_api_v1_path
        from catalog.archive.context import actor_from_user, archive_context

        source = "api" if is_api_v1_path(request.path_info) else "web"
        attribution = actor_from_user(getattr(request, "user", None), source=source)
        with archive_context(attribution.actor, attribution.source):
            return self.get_response(request)


class ApiTokenAuthMiddleware:
    """Authenticate ``/api/`` requests via an API key header. A resolved key sets
    ``request.user`` and skips CSRF; the user still passes the approval gate.
    Unauthenticated JSON-API requests get a 401 instead of the HTML login redirect."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Imported here so constructing the middleware does not pull in DRF.
        from catalog.api.exceptions import is_api_v1_path

        path = request.path_info.lstrip("/")
        # /api/v1/ authenticates through DRF (catalog.api.authentication);
        # this middleware only guards the legacy session-JSON endpoints. The
        # test is shared with the API's own 404 handler so the two cannot
        # disagree about where the versioned API starts -- while they did, an
        # anonymous GET /api/v1 counted as API to one and as a legacy JSON
        # endpoint to the other, and came back in a third error shape.
        if not path.startswith("api/") or is_api_v1_path(request.path_info):
            return self.get_response(request)

        raw_key = _extract_api_key(request)
        if raw_key:
            user = self._user_for_key(raw_key)
            if user is None:
                return JsonResponse({"error": "invalid API key"}, status=401)
            request.user = user
            request._dont_enforce_csrf_checks = True
            return self.get_response(request)

        # Everything under api/ except the HTML docs page answers 401 as JSON.
        is_json_api = not path.startswith("api/docs")
        if is_json_api and not request.user.is_authenticated:
            return JsonResponse(
                {"error": "authentication required — log in or send an API key"},
                status=401,
            )
        return self.get_response(request)

    def _user_for_key(self, raw_key):
        """Resolve a ``loop_<prefix>_<secret>`` key against catalog.models.APIKey."""
        from django.utils import timezone

        from catalog.models import APIKey

        parts = raw_key.split("_", 2)
        if len(parts) != 3 or parts[0] != "loop":
            return None
        credential = (
            APIKey.objects.select_related("user").filter(prefix=parts[1]).first()
        )
        if credential is None or not credential.matches(raw_key):
            return None
        now = timezone.now()
        if credential.expires_at is not None and credential.expires_at <= now:
            return None
        if credential.revoked_at is not None:
            return None
        if not credential.user.is_active:
            return None
        APIKey.objects.filter(pk=credential.pk).update(last_used_at=now)
        return credential.user

DEFAULT_EXEMPT = (
    r"^$",                         # /
    r"^index/?$",
    r"^catalog/?$",
    r"^accounts/",                 # auth pages
    r"^signup/pending/?$",
    r"^signup/complete/?$",
    r"^awaiting-approval/?$",
    r"^admin/",                    # ⬅ allow the whole admin tree
    r"^api/v1/",                   # versioned API handles its own auth/permissions
    r"^llms\.txt$",                # public LLM documentation discovery
    r"^docs\.url$",                # canonical human documentation URI
    r"^developers/api\.md$",       # public Markdown API guide
    r"^developers/agent\.md$",     # public API-only agent client guide
    r"^developers/samples/[\w-]+\.py$",  # downloadable copies of the documented samples
    r"^developers/docs/?$",         # public human API guide
    r"^developers/guide/?$",        # public long-form integration guide
    r"^developers/keys/?$",         # public API-key page shows sign-in/approval state
    r"^developers/?$",             # public human API documentation
    r"^static/",
    r"^media/",
)

class ApprovedGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        patterns = getattr(settings, "ACCESS_EXEMPT_URLS", DEFAULT_EXEMPT)
        login_path = settings.LOGIN_URL.lstrip("/")
        self.exempt = [re.compile(p) for p in patterns] + [re.compile(f"^{re.escape(login_path)}")]

        self.approved_group = getattr(settings, "APPROVED_GROUP_NAME", "approved")
        self.superuser_bypass = getattr(settings, "APPROVED_BYPASS_SUPERUSERS", True)

    def __call__(self, request):
        # Imported here so constructing the middleware does not pull in DRF.
        from catalog.api.exceptions import is_api_v1_path

        path = request.path_info.lstrip("/")

        # The versioned API authenticates itself and answers in problem+json.
        # Tested with the same predicate the API's 404 handler and the token
        # middleware use, rather than relying on the ``^api/v1/`` entry in the
        # exempt list: that pattern needs the trailing slash, so a bare
        # ``/api/v1`` fell through to the HTML login redirect while every other
        # API path returned JSON.
        if is_api_v1_path(request.path_info):
            return self.get_response(request)

        # Public or auth routes pass through
        if any(p.match(path) for p in self.exempt):
            return self.get_response(request)

        user = request.user

        # Not logged in → send to login with ?next=
        if not user.is_authenticated:
            return redirect_to_login(next=request.get_full_path())

        # let staff/superusers bypass the gate
        if self.superuser_bypass and (user.is_superuser or user.is_staff):
            return self.get_response(request)

        # Must be in the "approved" group
        if not user.groups.filter(name=self.approved_group).exists():
            return redirect(getattr(settings, "AWAITING_APPROVAL_URL_NAME", "awaiting_approval"))

        # All good
        return self.get_response(request)