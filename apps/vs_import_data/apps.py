from django.apps import AppConfig


class VsImportDataConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_import_data"

    def ready(self):
        import vs_import_data.signals  # noqa: F401
