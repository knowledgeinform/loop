"""OpenAPI extensions for LOOP-specific authentication."""

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from drf_spectacular.views import SpectacularAPIView
from rest_framework.renderers import JSONRenderer


class APIKeyAuthenticationScheme(OpenApiAuthenticationExtension):
    target_class = "catalog.api.authentication.APIKeyAuthentication"
    name = "LoopApiKey"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": (
                "A scoped LOOP key created at /api/v1/api-keys/. "
                "Authorization: Bearer <key> is also accepted."
            ),
        }


class BrowserOpenApiJsonView(SpectacularAPIView):
    """Render the API contract as ordinary JSON instead of a download MIME type."""

    renderer_classes = [JSONRenderer]
