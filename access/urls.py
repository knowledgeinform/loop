from django.urls import path

from access import views

urlpatterns = [
    path("chaos/", views.chaos_access, name="chaos_access"),
    path("chaos/terms/", views.chaos_terms, name="chaos_terms"),
]
