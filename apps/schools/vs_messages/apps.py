from django.apps import AppConfig


class VsMessagesConfig(AppConfig):
    """Registers the messaging app and its Field Access declarations."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_messages"
    verbose_name = "Messages"

    # Stated, not inferred: every table and every migration reads it.
    label = "vs_messages"

    def ready(self):
        # The fields an administrator may restrict per role.
        from vs_notifications.services.push import register_destination

        from . import field_access
        from .push import destination

        field_access.register()
        # Where a parent's or a pupil's message push opens.
        register_destination(destination)
