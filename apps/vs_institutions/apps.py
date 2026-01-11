from django.apps import AppConfig


class VsInstitutionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_institutions"

    def ready(self):
        from . import signals
