"""Authentication adapters for LOOP API v1."""

from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from catalog.api import key_service
from catalog.models import APIKey


class APIKeyAuthentication(BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        raw_key = request.META.get("HTTP_X_API_KEY", "").strip()
        if not raw_key:
            authorization = request.META.get("HTTP_AUTHORIZATION", "")
            keyword, separator, candidate = authorization.partition(" ")
            if separator and keyword.lower() in {"bearer", "apikey"}:
                raw_key = candidate.strip()
        if not raw_key:
            return None

        parts = raw_key.split("_", 2)
        if len(parts) != 3 or parts[0] != "loop":
            raise AuthenticationFailed("Invalid API key.")

        credential = APIKey.objects.select_related("user").filter(prefix=parts[1]).first()
        if credential is None or not credential.matches(raw_key):
            raise AuthenticationFailed("Invalid or expired API key.")

        if not credential.user.is_active:
            raise AuthenticationFailed("Invalid or expired API key.")

        now = timezone.now()
        if credential.expires_at is not None and credential.expires_at <= now:
            key_service.purge_expired(user=credential.user, now=now)
            raise AuthenticationFailed("Invalid or expired API key.")
        if credential.revoked_at is not None:
            raise AuthenticationFailed("Invalid or expired API key.")

        APIKey.objects.filter(pk=credential.pk).update(last_used_at=now)
        return credential.user, credential

    def authenticate_header(self, request):
        return 'Bearer realm="LOOP API"'
