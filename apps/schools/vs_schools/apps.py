from django.apps import AppConfig


class VsSchoolsConfig(AppConfig):
    """Register school policies and datasets at app startup.

    The app label stays ``vs_schools`` because historical migrations and table
    names depend on it. School-owned registrations live here so the shared
    media and export engines do not import a domain app.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_schools"
    label = "vs_schools"

    def ready(self):
        from . import field_access, signals  # noqa: F401

        field_access.register()
        field_access.register_audit_words()

        from .media_policies import register as register_media_policies

        register_media_policies()

        from .export_datasets import register_datasets, register_screens

        register_datasets()
        register_screens()
