"""Scope and approval permissions for LOOP API v1."""

from rest_framework.permissions import BasePermission
from rest_framework.permissions import SAFE_METHODS

from catalog.models import APIKey


class IsApprovedUser(BasePermission):
    """Mirror the website approval gate for protected API operations."""

    message = "Your LOOP account has not been approved."

    def has_permission(self, request, view):
        from access.policy import has_loop_access

        user = request.user
        if not user or not user.is_authenticated:
            return False
        # The Approved group and an affiliation set by an admin; staff and
        # superusers pass when APPROVED_BYPASS_SUPERUSERS is on.
        return has_loop_access(user)


class HasDataReadScope(BasePermission):
    message = "This API key does not have the data:read scope."

    def has_permission(self, request, view):
        return not isinstance(request.auth, APIKey) or "data:read" in request.auth.scopes


class HasDataWriteScope(BasePermission):
    message = "This API key does not have the data:write scope."

    def has_permission(self, request, view):
        return not isinstance(request.auth, APIKey) or "data:write" in request.auth.scopes


class HasImportWriteScope(BasePermission):
    message = "This API key does not have the imports:write scope."

    def has_permission(self, request, view):
        return not isinstance(request.auth, APIKey) or "imports:write" in request.auth.scopes


class HasFileWriteScope(BasePermission):
    message = "This API key does not have the files:write scope."

    def has_permission(self, request, view):
        if not request.FILES or not isinstance(request.auth, APIKey):
            return True
        return "files:write" in request.auth.scopes


class HasCatalogScope(BasePermission):
    """Require read scope for safe methods and write scope for mutations."""

    def has_permission(self, request, view):
        if not isinstance(request.auth, APIKey):
            return True
        required = "data:read" if request.method in SAFE_METHODS else "data:write"
        self.message = f"This API key does not have the {required} scope."
        return required in request.auth.scopes
