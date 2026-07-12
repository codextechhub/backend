from django.apps import AppConfig


class VsAdminConsoleConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_admin_console"

    def ready(self):
        import vs_admin_console.receivers  # noqa: F401 — end impersonations on tenant deactivation
