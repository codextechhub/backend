from django.apps import AppConfig


class VsAttendanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_attendance"
    verbose_name = "Attendance Management"

    # Stated, not inferred: every table and every migration reads it.
    label = "vs_attendance"

    def ready(self):
        # Its sections of a person's data copy (core.personal_data).
        from .personal_data import register_sections as register_personal_data

        register_personal_data()

        # The register's fields that an administrator may restrict per role.
        from . import field_access

        field_access.register()
