from django.apps import AppConfig  # Import Django's application configuration base class.


class VsPaymentsConfig(AppConfig):  # Register the payment app metadata with Django.
    default_auto_field = "django.db.models.BigAutoField"  # Use BigAutoField for generated model primary keys.
    name = "vs_payments"  # Point Django at the import path for the payments app.
    verbose_name = "Payments"  # Display a human-readable app name in admin and diagnostics.
