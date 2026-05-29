from django.urls import path

from . import data_management_views, views


urlpatterns = [
    path("", views.index, name="index"),
    path("browse/", views.browse_data, name="browse_data"),

    # Class-level (material_auid). Uses the ``path`` converter so that the
    # colon in ``M:...`` survives URL parsing.
    path("composition/<path:material_auid>/", views.composition_detail, name="composition_detail"),
    path("material/<path:material_auid>/annotation/", views.modify_material_annotation, name="modify_material_annotation"),
    path("material/<path:material_auid>/delete/", views.delete_material, name="delete_material"),

    # Recipe-level detail. composite recipe id "M:...:R:..." — needs the
    # ``path`` converter to preserve colons. Trials live at a sub-path keyed
    # by trial_id; literature at a sub-path keyed by DOI.
    path("recipe/<path:recipe_id>/trial/<str:trial_id>/delete/", views.delete_trial, name="delete_trial"),
    path("recipe/<path:recipe_id>/trial/<str:trial_id>/", views.trial_detail, name="trial_detail"),
    path("recipe/<path:recipe_id>/literature/<str:lit_id>/delete/", views.delete_literature, name="delete_literature"),
    path("recipe/<path:recipe_id>/literature/<str:lit_id>/", views.literature_detail, name="literature_detail"),
    path("recipe/<path:recipe_id>/", views.recipe_detail, name="recipe_detail"),

    # DFT runs live under the material. comp_auid is the composite
    # "M:...:C:..."; ``path`` converter keeps the colons intact.
    path(
        "material/<path:material_auid>/dft/<path:comp_auid>/delete/",
        views.delete_computational,
        name="delete_computational",
    ),
    path(
        "material/<path:material_auid>/dft/<path:comp_auid>/",
        views.computational_detail,
        name="computational_detail",
    ),

    # Add / edit entry points
    path("add/", views.add_data, name="add_data"),
    path("account/", views.account, name="account"),
    path("add/experiment/", views.upload_exp_data, name="upload_exp_data"),
    path("add/literature/", views.add_literature_data, name="add_literature_data"),
    path("add/computational/", views.add_computational_data, name="add_computational_data"),
    path("upload-experimental/", views.upload_exp_data, name="add_experimental"),
    path("upload-literature/", views.add_literature_data, name="add_literature"),

    # API
    path("api/search-doi/", views.search_by_doi, name="search_doi"),
    path("api/fetch-doi/", views.fetch_doi_metadata, name="fetch_doi"),
    path("api/normalize-composition/", views.normalize_composition_api, name="normalize_composition"),

    # Per-user precursor library
    path("account/precursors/", views.precursors_manage_page, name="precursors_manage"),
    path("api/precursors/", views.precursors_list, name="precursors_list"),
    path("api/precursors/create/", views.precursors_create, name="precursors_create"),
    path("api/precursors/cas-lookup/", views.precursors_cas_lookup, name="precursors_cas_lookup"),
    path("api/precursors/<str:precursor_id>/update/", views.precursors_update, name="precursors_update"),
    path("api/precursors/<str:precursor_id>/delete/", views.precursors_delete, name="precursors_delete"),

    # Per-user protocol library
    path("account/protocols/", views.protocols_manage_page, name="protocols_manage"),
    path("api/protocols/", views.protocols_list, name="protocols_list"),
    path("api/protocols/create/", views.protocols_create, name="protocols_create"),
    path("api/protocols/<str:protocol_id>/update/", views.protocols_update, name="protocols_update"),
    path("api/protocols/<str:protocol_id>/delete/", views.protocols_delete, name="protocols_delete"),

    # Superuser: MongoDB data management
    path("data-management/", data_management_views.data_management_index, name="data_management_index"),
    path("data-management/users/", data_management_views.data_management_users, name="data_management_users"),
    path(
        "data-management/users/<int:user_id>/edit/",
        data_management_views.data_management_user_edit,
        name="data_management_user_edit",
    ),
    path(
        "data-management/<slug:collection>/<path:object_id>/delete/",
        data_management_views.data_management_delete,
        name="data_management_delete",
    ),
    path(
        "data-management/<slug:collection>/<path:object_id>/edit/",
        data_management_views.data_management_edit,
        name="data_management_edit",
    ),
    path(
        "data-management/<slug:collection>/",
        data_management_views.data_management_collection,
        name="data_management_collection",
    ),
]
