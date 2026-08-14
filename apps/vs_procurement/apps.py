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
        # Screen bindings let a filtered list screen become a one-click export.
        from .export_datasets import register_screens

        register_screens()
        # Contribute the AP and GR/IR reconciliations to the finance period close.
        # Registered here, not imported by finance, so the dependency keeps running
        # procurement to finance and never back.
        from .close_checks import register as register_close_checks

        register_close_checks()
        # Publish this tenant's spend-approval ladders when its books are created,
        # so the gate is on from onboarding rather than from a remembered command.
        from vs_finance.provisioning import register_entity_provisioner

        from .provisioning import provision_approval_ladders

        register_entity_provisioner(provision_approval_ladders)
