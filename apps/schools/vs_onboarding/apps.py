from django.apps import AppConfig


class VsOnboardingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_onboarding"

    # Pinned rather than derived, for the same reason vs_schools pins its own:
    # the label is what every migration, table name and permission namespace
    # will refer to, and letting Django infer it from the last segment of
    # ``name`` makes a package move into a database migration.
    label = "vs_onboarding"

    def ready(self):
        # Publish what a support ticket may carry about onboarding. Registered
        # here, from the app that owns the vocabulary, so vs_tickets never
        # imports the school package to find out what a task key is.
        from .ticket_context import register_ticket_context

        register_ticket_context()
