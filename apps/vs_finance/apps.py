from django.apps import AppConfig


# Registers the finance Django app.
class VsFinanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"  # Use BigAutoField for implicit primary keys.
    name = "vs_finance"
    verbose_name = "Finance"  # Human-readable app name in Django admin.

    def ready(self):
        # Publish this app's datasets to the Export Centre. Registration lives
        # here, not in vs_exports, so the engine never imports a domain app.
        from .export_datasets import register_datasets

        register_datasets()
        # Screen bindings let a filtered list screen become a one-click export.
        from .export_datasets import register_screens

        register_screens()
        # Publish this tenant's adjustment-approval ladders when its books are
        # created. Finance registers its own provisioner through the same seam
        # procurement and payments use, rather than calling itself from the
        # serializer, so all three arrive by one mechanism.
        from .provisioning import register_entity_provisioner
        from .provisioning_hooks import provision_adjustment_approvals

        register_entity_provisioner(provision_adjustment_approvals)
        # Settle customer document deliveries when vs_notifications reports the
        # outcome of an email it sent on our behalf.
        from . import receivers  # noqa: F401

