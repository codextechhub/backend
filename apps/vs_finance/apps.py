from django.apps import AppConfig


# Registers the finance Django app.
class VsFinanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"  # Use BigAutoField for implicit primary keys.
    name = "vs_finance"
    verbose_name = "Finance"  # Human-readable app name in Django admin.
