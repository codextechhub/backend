from django.apps import AppConfig  # Django application configuration base class.


class VsFinanceConfig(AppConfig):  # Registers the finance Django app.
    default_auto_field = "django.db.models.BigAutoField"  # Use BigAutoField for implicit primary keys.
    name = "vs_finance"  # Import path for the finance app.
    verbose_name = "Finance"  # Human-readable app name in Django admin.
