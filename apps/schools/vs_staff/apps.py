from django.apps import AppConfig


class VsStaffConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "schools.vs_staff"
    label = "vs_staff"
    verbose_name = "Staff Management"

    def ready(self):
        # The workflow engine never imports a domain app; the domain app
        # registers its handler. Same direction as vs_finance, vs_procurement
        # and vs_payments.
        from . import signals, workflow_handlers  # noqa: F401

        # No default media policy exists: a file whose owner registers nothing
        # is never served. This is what makes a staff photograph and a staff
        # document readable at all, and what stops either being readable by the
        # wrong branch.
        from . import media_policies

        media_policies.register()
