from django.apps import AppConfig


class VsAcademicsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_academics"

    # Stated, not inferred: every table and every migration reads it.
    label = "vs_academics"

    def ready(self):
        # Registered here, never in vs_exports: the engine keeps no import of
        # a school-shaped module.
        from .export_datasets import register_datasets, register_screens

        register_datasets()
        # After the datasets: a binding names one, and an unpublished dataset
        # would resolve to None at request time rather than at boot.
        register_screens()
