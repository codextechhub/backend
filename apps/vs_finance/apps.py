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

