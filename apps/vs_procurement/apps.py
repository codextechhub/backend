"""Django application configuration for the procurement bounded context."""

from django.apps import AppConfig


class VsProcurementConfig(AppConfig):
    """Register procurement and connect its workflow integration at app startup."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_procurement"
    verbose_name = "Procurement"

    def ready(self):
        """Load handler decorators after Django's application registry is ready."""
        # Import for side effect: registers the spend-approval handlers with vs_workflow.
        from . import workflow_handlers  # noqa: F401
        from . import receivers  # noqa: F401

        # Publish this app's datasets to the Export Centre. Registration lives
        # here, not in vs_exports, so the engine never imports a domain app.
        from .export_datasets import register_datasets

        register_datasets()
