from django.apps import AppConfig


class VsSchoolsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_schools"

    def ready(self):
        from . import signals  # noqa: F401

        # Publish this app's datasets to the Export Centre, and bind its list
        # screen so "Export what this table is showing" can translate its
        # filters. Registration lives here, not in vs_exports, so the engine
        # never imports a domain app.
        from .export_datasets import register_datasets, register_screens

        register_datasets()
        register_screens()
