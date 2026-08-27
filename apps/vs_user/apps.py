from django.apps import AppConfig


class VsUserConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_user"

    def ready(self):
        import vs_user.workflow_handlers  # noqa: F401 - registers PLATFORM_USER_CREATION handler
        import vs_user.receivers  # noqa: F401 - invitation email delivery tracking

        # Declare who may read staff photos through /media/.
        from .media_policies import register as register_media_policies

        register_media_policies()

        # Publish this app's datasets to the Export Centre. Registration lives
        # here, not in vs_exports, so the engine never imports a domain app.
        from .export_datasets import register_datasets

        register_datasets()
        # Screen bindings let a filtered list screen become a one-click export.
        from .export_datasets import register_screens

        register_screens()
