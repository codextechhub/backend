from django.apps import AppConfig


class VsAcademicsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_academics"

    # Pinned for the same reason vs_schools pins its own: the package sits under
    # apps/schools/, and Django would derive the label from the last segment
    # anyway, but every table and every future migration reads it, so it is
    # stated rather than inferred.
    label = "vs_academics"
