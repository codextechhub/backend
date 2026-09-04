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

        # The engine's permission bridge is keyed by dataset, and a module
        # that does not register one is refused however its key is granted.
        # Same direction as the export registration above: the domain app
        # tells the engine, and the engine imports nothing.
        from vs_import_data.permissions import register_dataset_import_key

        from .constants import PERM_STRUCTURE_IMPORT

        register_dataset_import_key("academic_structure", PERM_STRUCTURE_IMPORT)

        register_datasets()
        # After the datasets: a binding names one, and an unpublished dataset
        # would resolve to None at request time rather than at boot.
        register_screens()
