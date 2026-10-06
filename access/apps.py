from django.apps import AppConfig


class AccessConfig(AppConfig):
    """One account for s4e.ai, two curtains: LOOP and CHAOS."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "access"
    verbose_name = "Site access"
