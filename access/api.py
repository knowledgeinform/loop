"""``GET /api/v1/access/``: which curtains the caller may pass.

Other s4e.ai services (the CHAOS app) forward the visitor's session cookie or
API key here and act on the answer. The response only describes the caller,
so it is safe to answer anyone: anonymous callers learn nothing, and an
invalid key gets the API's usual 401.
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.urls import reverse

from access import policy
from catalog.api.throttling import APIKeyRateThrottle, SessionUserRateThrottle
from catalog.models import APIKey


@extend_schema(
    tags=["Authentication"],
    summary="Which parts of s4e.ai the caller may use",
    responses={200: OpenApiTypes.OBJECT},
)
@api_view(["GET"])
@permission_classes([AllowAny])
# Per key and per account only. The CHAOS app calls from one address for all
# of its visitors, so the address-keyed throttles would pool them together.
@throttle_classes([APIKeyRateThrottle, SessionUserRateThrottle])
def access_status(request):
    user = request.user
    key = request.auth if isinstance(request.auth, APIKey) else None
    if not getattr(user, "is_authenticated", False):
        data = {"authenticated": False, "loop": False, "chaos": False}
    else:
        scopes = list(key.scopes or []) if key is not None else None
        chaos = policy.has_chaos_access(user) and (
            key is None or policy.CHAOS_KEY_SCOPE in scopes
        )
        # Until the account has filled in its profile (and accepted the usage
        # notice), neither curtain opens; profile_url says where to do it.
        profile_ok = policy.profile_ok(user)
        data = {
            "authenticated": True,
            "user": {"id": user.id, "username": user.get_username()},
            "via": "api_key" if key is not None else "session",
            "loop": policy.has_loop_access(user) and profile_ok,
            "chaos": chaos and profile_ok,
            "chaos_terms_current": policy.accepted_current_chaos_terms(user),
            # True when the profile requirement is met (or switched off with
            # PROFILE_REQUIRED=0), so the gate and the kiosk follow the switch.
            "profile_complete": profile_ok,
            "profile_required": policy.profile_required(),
            "profile_url": request.build_absolute_uri(reverse("profile")),
        }
        if key is not None:
            data["key_prefix"] = key.prefix
            data["scopes"] = scopes
    response = Response({"data": data})
    response["Cache-Control"] = "no-store"
    return response
