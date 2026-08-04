"""Shared-cache throttles for IPs, session users, and individual API keys."""

from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle

from catalog.models import APIKey


class _ConfiguredThrottle(SimpleRateThrottle):
    setting_name = ""
    default_rate = "60/min"

    def get_rate(self):
        return getattr(settings, self.setting_name, self.default_rate)


class AnonymousIPRateThrottle(_ConfiguredThrottle):
    scope = "loop_anon_ip"
    setting_name = "LOOP_API_ANON_IP_RATE"
    default_rate = "60/min"

    def get_cache_key(self, request, view):
        if request.user and request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class AuthenticatedIPRateThrottle(_ConfiguredThrottle):
    scope = "loop_auth_ip"
    setting_name = "LOOP_API_AUTH_IP_RATE"
    default_rate = "1200/min"

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class APIKeyRateThrottle(_ConfiguredThrottle):
    scope = "loop_api_key"
    setting_name = "LOOP_API_KEY_RATE"
    default_rate = "600/min"

    def get_cache_key(self, request, view):
        if not isinstance(request.auth, APIKey):
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.auth.prefix}


class SessionUserRateThrottle(_ConfiguredThrottle):
    scope = "loop_session_user"
    setting_name = "LOOP_API_SESSION_USER_RATE"
    default_rate = "600/min"

    def get_cache_key(self, request, view):
        if (
            not request.user
            or not request.user.is_authenticated
            or isinstance(request.auth, APIKey)
        ):
            return None
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}
