from django.apps import AppConfig


class VsNoticesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_notices"
    verbose_name = "Notices"

    # Stated, not inferred: every table and every migration reads it.
    label = "vs_notices"

    def ready(self):
        # The fields an administrator may restrict per role.
        from . import field_access

        field_access.register()
