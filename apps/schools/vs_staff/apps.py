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

        # The staff personal details an administrator may restrict per role.
        # Field Access never imports a domain app either; the app declares its
        # own fields.
        from . import field_access

        field_access.register()

        # The rows a staff profile can be read as at an earlier date.
        from . import history

        history.register()

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

        # The shape of a school's staff profile policy, checked on every write
        # path vs_config has, since vs_config knows nothing of a staff profile.
        from vs_config.services.resolution import register_value_guard

        from .services.visibility import POLICY_KEY, guard_policy

        register_value_guard(POLICY_KEY, guard_policy)

        # The org chart a school requester's ORGANOGRAM approval stage climbs,
        # and the posts a school's stages and approver groups can name. Same
        # direction again: the engine is told, and imports nothing.
        from vs_tenants.models import Tenant
        from vs_workflow.services.positions import register_tenant_organogram

        from .services.organogram import StaffOrganogramService

        register_tenant_organogram(Tenant.Kind.SCHOOL, StaffOrganogramService)

        from .person_exit import register as register_person_exit

        register_person_exit()
