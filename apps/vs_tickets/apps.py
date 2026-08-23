from django.apps import AppConfig


class VsTicketsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "vs_tickets"
    verbose_name = "Support Tickets"

    def ready(self):
        # Publish this app's datasets to the Export Centre. Registration lives
        # here, not in vs_exports, so the engine never imports a domain app.
        from .export_datasets import register_datasets

        register_datasets()
        # Screen bindings let a filtered list screen become a one-click export.
        from .export_datasets import register_screens

        register_screens()

