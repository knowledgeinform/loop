from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from rest_framework.permissions import IsAuthenticated

from . import views
from .permissions import IsApprovedUser
from .schema import BrowserOpenApiJsonView


ACCOUNT_PERMISSIONS = [IsAuthenticated, IsApprovedUser]


urlpatterns = [
    path("health/", views.health, name="api-v1-health"),
    path("version/", views.version, name="api-v1-version"),
    path("me/", views.me, name="api-v1-me"),
    path("api-keys/", views.api_keys, name="api-v1-api-keys"),
    path("api-keys/<int:key_id>/", views.api_key_detail, name="api-v1-api-key-detail"),
    path("records/validate/", views.validate_record, name="api-v1-record-validate"),
    path("imports/", views.imports, name="api-v1-imports"),
    path(
        "compositions/normalize/",
        views.normalize_composition,
        name="api-v1-composition-normalize",
    ),
    path("doi/", views.doi_lookup, name="api-v1-doi-lookup"),
    path("doi/metadata/", views.doi_metadata, name="api-v1-doi-metadata"),
    path("precursors/", views.precursors, name="api-v1-precursors"),
    path(
        "precursors/cas-lookup/",
        views.precursor_cas_lookup,
        name="api-v1-precursor-cas-lookup",
    ),
    path(
        "precursors/<str:precursor_id>/",
        views.precursor_detail,
        name="api-v1-precursor-detail",
    ),
    path("protocols/", views.protocols, name="api-v1-protocols"),
    path(
        "protocols/<str:protocol_id>/",
        views.protocol_detail,
        name="api-v1-protocol-detail",
    ),
    path("experiments/", views.experiments, name="api-v1-experiments"),
    path("literature/", views.literature, name="api-v1-literature"),
    path("computational/", views.computational, name="api-v1-computational"),
    path("materials/", views.materials, name="api-v1-materials"),
    path("materials/<str:material_auid>/", views.material_detail, name="api-v1-material-detail"),
    path(
        "materials/<str:material_auid>/recipes/",
        views.material_recipes,
        name="api-v1-material-recipes",
    ),
    path(
        "materials/<str:material_auid>/download/",
        views.material_download,
        name="api-v1-material-download",
    ),
    path("recipes/<str:recipe_auid>/", views.recipe_detail, name="api-v1-recipe-detail"),
    path(
        "recipes/<str:recipe_auid>/trials/",
        views.recipe_trials,
        name="api-v1-recipe-trials",
    ),
    path(
        "recipes/<str:recipe_auid>/download/",
        views.recipe_download,
        name="api-v1-recipe-download",
    ),
    path(
        "recipes/<str:recipe_auid>/trials/<str:trial_id>/",
        views.experiment_detail,
        name="api-v1-experiment-detail",
    ),
    path(
        "recipes/<str:recipe_auid>/trials/<str:trial_id>/download/",
        views.experiment_download,
        name="api-v1-experiment-download",
    ),
    path(
        "recipes/<str:recipe_auid>/trials/<str:trial_id>/xrd/",
        views.experiment_xrd,
        name="api-v1-experiment-xrd",
    ),
    path(
        "recipes/<str:recipe_auid>/trials/<str:trial_id>/xrd/metadata/",
        views.experiment_xrd_metadata,
        name="api-v1-experiment-xrd-metadata",
    ),
    path(
        "recipes/<str:recipe_auid>/trials/<str:trial_id>/xrd/preview/",
        views.experiment_xrd_preview,
        name="api-v1-experiment-xrd-preview",
    ),
    path(
        "recipes/<str:recipe_auid>/trials/<str:trial_id>/xrd-analyses/",
        views.experiment_xrd_analysis_submit,
        name="api-v1-experiment-xrd-analysis-submit",
    ),
    path(
        "xrd-analysis-jobs/<str:job_id>/",
        views.xrd_analysis_job_detail,
        name="api-v1-xrd-analysis-job-detail",
    ),
    path(
        "xrd-analyses/<str:analysis_id>/",
        views.xrd_analysis_result,
        name="api-v1-xrd-analysis-result",
    ),
    path(
        "xrd-analyses/<str:analysis_id>/artifacts/<str:artifact_name>/",
        views.xrd_analysis_artifact,
        name="api-v1-xrd-analysis-artifact",
    ),
    path(
        "materials/<str:material_auid>/computations/<str:comp_auid>/",
        views.computational_detail,
        name="api-v1-computational-detail",
    ),
    path(
        "recipes/<str:recipe_auid>/literature/<str:lit_id>/",
        views.literature_detail,
        name="api-v1-literature-detail",
    ),
    path(
        "openapi/",
        SpectacularAPIView.as_view(permission_classes=ACCOUNT_PERMISSIONS),
        name="api-v1-openapi",
    ),
    path(
        "openapi.json",
        BrowserOpenApiJsonView.as_view(permission_classes=ACCOUNT_PERMISSIONS),
        name="api-v1-openapi-json",
    ),
    path(
        "docs/",
        SpectacularSwaggerView.as_view(
            url_name="api-v1-openapi", permission_classes=ACCOUNT_PERMISSIONS
        ),
        name="api-v1-docs",
    ),
    path(
        "redoc/",
        SpectacularRedocView.as_view(
            url_name="api-v1-openapi", permission_classes=ACCOUNT_PERMISSIONS
        ),
        name="api-v1-redoc",
    ),
]
