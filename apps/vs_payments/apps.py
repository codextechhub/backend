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

