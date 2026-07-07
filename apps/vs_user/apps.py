from django.apps import AppConfig


class VsUserConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_user"

    def ready(self):
        import vs_user.workflow_handlers  # noqa: F401 — registers PLATFORM_USER_CREATION handler
        import vs_user.receivers  # noqa: F401 — invitation email delivery tracking
