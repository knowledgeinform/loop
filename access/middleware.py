"""Two middlewares for the usage record (decided by Corey Oses, 2026-09-29).

UsageRecordMiddleware writes one line per request to the usage record
(access/usage.py). It sits right after WhiteNoise, so static files are left
out and everything else is seen with its final status, including the
redirects and refusals of the middlewares after it.

ProfileRequiredMiddleware sends a signed-in account that has not filled in
its profile, or has not accepted the current usage notice, to the profile page
before anything else. API requests (/api/, by session or API key) get 403 with
the address of that page instead; /api/v1/access/ is left alone, since it
reports the profile state to the CHAOS gate and the CHAOS-Agent kiosk. The API
key is resolved here because most /api/v1/ views name their own permission
classes, so a DRF default permission would not reach them. Every account, new
and existing, staff included, goes through it once. PROFILE_REQUIRED=0 turns
it off.
"""

import re
import time
from urllib.parse import quote

from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from access import policy
from access.usage import record_request, usage_dir

# Reachable without a profile: signing in and out, password pages and the
# profile page itself (accounts/), sign-up steps, the public documents.
PROFILE_EXEMPT = [
    re.compile(p)
    for p in (
        r"^accounts/",
        r"^static/",
        r"^signup/",
        r"^awaiting-approval/?$",
        r"^access/chaos/terms/?$",
        r"^llms\.txt$",
        r"^docs\.url$",
        r"^developers/(api\.md|agent\.md|docs/?|guide/?|samples/[\w-]+\.py)$",
        # Reports the profile state itself (access/api.py).
        r"^api/v1/access/?$",
        # The catalog's totals, counts only (catalog.views.catalog_counts).
        r"^api/v1/stats/?$",
    )
]


def api_key_of(request):
    """A LOOP API key the way either API reads it: X-API-Key, or Authorization
    with Bearer, Token (the older endpoints) or ApiKey (the versioned API)."""
    direct = request.META.get("HTTP_X_API_KEY", "").strip()
    if direct:
        return direct
    keyword, _, candidate = request.META.get("HTTP_AUTHORIZATION", "").strip().partition(" ")
    if keyword.lower() in ("bearer", "token", "apikey") and candidate.strip():
        return candidate.strip()
    return None


class UsageRecordMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.monotonic()
        response = self.get_response(request)
        if usage_dir():
            try:
                record_request(request, response, started)
            except Exception:  # the record must never break a page
                pass
        return response


class ProfileRequiredMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not policy.profile_required():
            return self.get_response(request)
        path = request.path_info.lstrip("/")
        if any(p.match(path) for p in PROFILE_EXEMPT):
            return self.get_response(request)
        user = getattr(request, "user", None)
        if path.startswith("api/"):
            # A key decides whose request it is, whatever session comes with
            # it: DRF authenticates the key first (catalog.api.authentication),
            # after this middleware, so the key's owner is checked here. An
            # invalid key is left to the views (401).
            from loop.middleware import ApiTokenAuthMiddleware

            raw_key = api_key_of(request)
            owner = ApiTokenAuthMiddleware._user_for_key(None, raw_key) if raw_key else None
            if owner is not None:
                user = owner
        if user is None or not user.is_authenticated or policy.profile_complete(user):
            return self.get_response(request)
        profile_url = reverse("profile")
        if path.startswith("api/"):
            return JsonResponse(
                {
                    "error": "complete your profile first",
                    "profile_url": request.build_absolute_uri(profile_url),
                },
                status=403,
            )
        return redirect(f"{profile_url}?next={quote(request.get_full_path())}")
