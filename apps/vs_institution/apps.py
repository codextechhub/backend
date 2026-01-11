from django.apps import AppConfig


class VsInstitutionConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_institution"

    def ready(self):
        from . import signals
