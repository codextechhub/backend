from django.apps import AppConfig


class VsProcurementConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_procurement"
    verbose_name = "Procurement"

    def ready(self):
        # Import for side effect: registers the spend-approval handlers with vs_workflow.
        from . import workflow_handlers  # noqa: F401
