from django.apps import AppConfig


class VsSchoolsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_schools"

    def ready(self):
        from . import signals
