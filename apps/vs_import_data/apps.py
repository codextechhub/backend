from django.apps import AppConfig


class VsImportDataConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_import_data"

    def ready(self):
        import vs_import_data.signals  # noqa: F401

        # Declare the import fields an administrator may restrict per role.
        from .field_access import register as register_field_access

        register_field_access()
