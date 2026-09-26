from django.apps import AppConfig


class VsRbacConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'vs_rbac'

    def ready(self):
        import vs_rbac.signals  # noqa: F401
        import vs_rbac.workflow_handlers  # noqa: F401 - registers rbac.role_change handler

        # Role grants and permission exceptions keep a history, so a staff
        # profile's roles can be read as at an earlier date.
        from .history import register as register_history

        register_history()
