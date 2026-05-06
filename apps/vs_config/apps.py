from django.apps import AppConfig


class VsConfigConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'vs_config'

    def ready(self):
        import vs_config.signals  # noqa: F401
