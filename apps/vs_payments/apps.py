from django.apps import AppConfig


# Register the payment app metadata with Django.
class VsPaymentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"  # Use BigAutoField for generated model primary keys.
    name = "vs_payments"  # Point Django at the import path for the payments app.
    verbose_name = "Payments"  # Display a human-readable app name in admin and diagnostics.

    def ready(self):
        # Publish this app's datasets to the Export Centre. Registration lives
        # here, not in vs_exports, so the engine never imports a domain app.
        from .export_datasets import register_datasets

        register_datasets()
        # Screen bindings let a filtered list screen become a one-click export.
        from .export_datasets import register_screens

        register_screens()
        # Publish this tenant's payout-approval ladder when its books are created, so
        # the gate over the highest-risk cash-out path is on from onboarding rather
        # than from a remembered command.
        from vs_finance.provisioning import register_entity_provisioner

        from .provisioning import provision_payout_approval

        register_entity_provisioner(provision_payout_approval)

