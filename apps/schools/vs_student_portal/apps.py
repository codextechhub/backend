from django.apps import AppConfig


class VsStudentPortalConfig(AppConfig):
    """The pupil portal: a pupil's own view of their school record.

    Owns no table. Every screen is read from the module that owns the fact,
    behind the link between the signed-in account and the pupil's record.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_student_portal"
    verbose_name = "Student Portal"

    # Stated, not inferred: a label is how every later migration names the app.
    label = "vs_student_portal"

    def ready(self):
        from vs_notifications.services.push import register_destination

        from .push import destination

        register_destination(destination)
