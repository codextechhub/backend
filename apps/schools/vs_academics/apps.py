from django.apps import AppConfig


class VsAcademicsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_academics"

    # Pinned for the same reason vs_schools pins its own: the package sits under
    # apps/schools/, and Django would derive the label from the last segment
    # anyway, but every table and every future migration reads it, so it is
    # stated rather than inferred.
    label = "vs_academics"

    def ready(self):
        # Publish this module's datasets to the Export Centre. Registration
        # lives in the domain app, never in vs_exports, so the engine keeps no
        # import of a school-shaped module.
        from .export_datasets import register_datasets, register_screens

        register_datasets()
        # After the datasets: a binding names the dataset it exports from, and
        # a screen bound to one that is not published yet would resolve to None
        # at request time rather than at boot.
        register_screens()
