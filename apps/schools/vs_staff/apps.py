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

        # The engine's permission bridge is keyed by dataset, and a module that
        # registers none is refused the wizard however its key is granted. Same
        # direction as the registrations above: the domain app tells the
        # engine, and the engine imports nothing.
        from vs_import_data.permissions import register_dataset_import_key

        from .constants import PERM_IMPORT

        register_dataset_import_key("staff", PERM_IMPORT)
