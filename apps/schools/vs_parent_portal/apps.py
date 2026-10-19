from django.apps import AppConfig


class VsParentPortalConfig(AppConfig):
    """The parent portal: a parent's view of their own children at the school.

    Owns no table. Every screen is read from the module that owns the fact,
    behind the link between the signed-in account and a guardian record.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_parent_portal"
    verbose_name = "Parent Portal"

    # Stated, not inferred: a label is how every later migration names the app.
    label = "vs_parent_portal"

    def ready(self):
        from vs_notifications.services.push import register_destination

        from . import sign_in
        from .push import destination

        register_destination(destination)
        sign_in.register()
