from django.apps import AppConfig


class VsGradebookConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_gradebook"
    verbose_name = "Gradebook and Assessments"

    # Stated, not inferred: every table and every migration reads it.
    label = "vs_gradebook"

    def ready(self):
        # The fields an administrator may restrict per role.
        from . import field_access

        field_access.register()

        # Taking a student out of an elective must not orphan their scores.
        from schools.vs_academics.services.electives import register_removal_guard

        from .services.electives import refuse_removal_with_scores

        register_removal_guard(refuse_removal_with_scores)
