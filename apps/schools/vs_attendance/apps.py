from django.apps import AppConfig


class VsAttendanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_attendance"
    verbose_name = "Attendance Management"

    # Stated, not inferred: every table and every migration reads it.
    label = "vs_attendance"

    def ready(self):
        # The register's fields that an administrator may restrict per role.
        from . import field_access

        field_access.register()
