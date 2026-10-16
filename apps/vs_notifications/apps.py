# =============================================================================
# vs_notifications / apps.py
# =============================================================================

from django.apps import AppConfig


class VsNotificationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name  = "vs_notifications"
    label = "vs_notifications"
    verbose_name = "Notification & Messaging Engine"

    def ready(self):
        # Importing the module registers its @register(Tags.database) check.
        # It has to happen here rather than at import time so the check only
        # exists when the app is actually installed.
        from . import checks  # noqa: F401

        # A device session that ends takes its pushes with it.
        from vs_user.signals import device_session_ended

        from .services.push import forget_device

        device_session_ended.connect(
            lambda sender, device_key, **kwargs: forget_device(device_key),
            weak=False, dispatch_uid="vs_notifications.forget_push_device",
        )
