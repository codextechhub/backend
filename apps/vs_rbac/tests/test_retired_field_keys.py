"""The eight keys that only ever stood for a field, and are gone.

Each guarded fields and nothing else. Field Access moved those fields onto
per-role Read and Write switches, so a key that remained registered would be a
box that changes nothing: Adaeze ticks "Read field-level sensitive data" for
Bright Star's nurse, saves, and the nurse still cannot see a blood group,
because the answer now lives on the Field Access screen she did not open.

Three things have to stay true together, and each is checked here rather than
inferred from the others: the seed tables name none of them, a seeded database
carries no row for them, and migration 0026 takes the grants, memberships,
defaults and exceptions of an already-running database with them.

The keys that also guard a page stay, whatever their names suggest, and the
last test names them so that a later sweep cannot quietly take them too.
"""
from io import StringIO

from django.apps import apps as global_apps
from django.core.management import call_command
from django.test import TestCase

from vs_rbac.field_conversion import CONVERSIONS
from vs_rbac.models import (
    GroupPermission,
    Permission,
    PermissionGroup,
    PermissionResource,
    PermissionScope,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRoleChangeDeltaItem,
    TenantRoleChangeRequest,
    TenantRolePermission,
    UserPermissionOverride,
)

from .helpers import (
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)

RETIRED_KEYS = (
    "procurement.vendor.view_sensitive",
    "finance.bankaccount.view_sensitive",
    "finance.payrollrun.view_sensitive",
    "payments.virtual_account.view_sensitive",
    "payments.payout.view_sensitive",
    "school.students.view_sensitive",
    "platform.staff_payroll.view",
    "platform.staff_payroll.manage",
)

#: Converted keys migration 0026 leaves because they also guard an endpoint.
SURVIVING_KEYS = (
    "platform.team.view",
    "school.students.manage",
    "import.templates.manage",
    "import.jobs.view",
    "import.batches.view",
)

# Migration 0027 later retires the two broad action keys in this historical set.
ACTIVE_SURVIVING_KEYS = tuple(key for key in SURVIVING_KEYS if not key.endswith(".manage"))


def _retire_them():
    """Migration 0026's forward function, against the live model registry."""
    from importlib import import_module

    module = import_module(
        "vs_rbac.migrations.0026_a_field_is_a_switch_and_no_longer_a_key",
    )
    module.retire_them(global_apps, None)
    return module


class RetiredKeysLeaveNoSeedTests(TestCase):
    """The tables a key is declared in, read directly.

    A seeded database is checked separately. This reads the source tables, so
    a key reintroduced into one of them is named here even if the seed that
    consumes it is not exercised.
    """

    def _seed_table_keys(self):
        from core.management.commands.seed_platform_permissions import (
            PLATFORM_RESOURCES,
        )
        from core.management.commands.seed_school_permissions import (
            SCHOOL_PERMISSIONS,
        )
        from vs_finance.management.commands.seed_finance_permissions import (
            FINANCE_RESOURCES,
        )
        from vs_payments.management.commands.seed_payments_permissions import (
            PAYMENTS_RESOURCES,
        )
        from vs_procurement.management.commands.seed_procurement_permissions import (
            PROCUREMENT_RESOURCES,
        )

        keys = {
            f"{module}.{resource}.{action}"
            for module, resource, action, _sensitivity, _roles in SCHOOL_PERMISSIONS
        }
        keys |= {
            f"platform.{resource}.{action}"
            for resource, _label, actions in PLATFORM_RESOURCES
            for action, *_rest in actions
        }
        for module, table in (
            ("finance", FINANCE_RESOURCES),
            ("payments", PAYMENTS_RESOURCES),
            ("procurement", PROCUREMENT_RESOURCES),
        ):
            keys |= {
                f"{module}.{resource}.{action}"
                for resource, _label, actions in table
                for action, *_rest in actions
            }
        return keys

    def test_no_module_seed_declares_a_retired_key(self):
        declared = self._seed_table_keys()
        self.assertEqual(
            sorted(k for k in RETIRED_KEYS if k in declared), [],
            "A retired key is back in a module seed table.",
        )

    def test_a_surviving_field_conversion_key_is_still_declared(self):
        """The field-key sweep was exact and left the staff-list key alone."""
        declared = self._seed_table_keys()
        self.assertIn("platform.team.view", declared)

    def test_no_band_names_a_retired_key(self):
        from vs_rbac.permission_bands import ACTION_BANDS, NEVER_BAND

        banded = {
            f"{module}.{resource}.{action}"
            for module, resource, action in set(NEVER_BAND) | set(ACTION_BANDS)
        }
        self.assertEqual(
            sorted(k for k in RETIRED_KEYS if k in banded), [],
            "A retired key is banded; it is not a key any more.",
        )

    def test_the_conversion_table_still_names_them_as_not_surviving(self):
        """The mapping outlives the keys, and is the authority on which went.

        Migration 0025 and ``verify_field_access_conversion`` read it to prove
        that release day changed nobody's access, so it describes a database
        from before the keys were deleted.
        """
        not_surviving = {e.key for e in CONVERSIONS if not e.survives}
        self.assertEqual(not_surviving, set(RETIRED_KEYS))
        surviving = {e.key for e in CONVERSIONS if e.survives}
        self.assertEqual(surviving, set(SURVIVING_KEYS))


class RetiredKeysAreNotSeededTests(TestCase):
    """A database built by the seeds, asked whether the keys are in it."""

    @classmethod
    def setUpTestData(cls):
        call_command(
            "seed_all_permissions", stdout=StringIO(), stderr=StringIO(), verbosity=0,
        )

    def test_the_seeds_ran(self):
        """Absence proves nothing unless the seeds actually registered keys."""
        self.assertTrue(Permission.objects.filter(key="school.students.view").exists())
        self.assertTrue(Permission.objects.filter(key="finance.bankaccount.view").exists())
        self.assertTrue(Permission.objects.filter(key="payments.payout.view").exists())
        self.assertTrue(
            Permission.objects.filter(key="procurement.vendor.view").exists()
        )

    def test_no_retired_key_is_registered(self):
        present = sorted(
            Permission.objects.filter(key__in=RETIRED_KEYS).values_list("key", flat=True)
        )
        self.assertEqual(present, [], f"Seeded and deciding nothing: {present}")

    def test_no_retired_key_is_granted_or_grouped_or_defaulted(self):
        for model, field in (
            (TenantRolePermission, "permission_id"),
            (GroupPermission, "permission_id"),
            (PrebuiltRolePermission, "permission_id"),
        ):
            with self.subTest(model=model.__name__):
                self.assertFalse(
                    model.objects.filter(**{f"{field}__in": RETIRED_KEYS}).exists(),
                )

    def test_the_emptied_resource_is_gone(self):
        """``platform.staff_payroll`` lost both its actions, so the node goes.

        A resource with nothing under it is a branch of the access catalogue
        that opens onto an empty list.
        """
        self.assertFalse(
            PermissionResource.objects.filter(
                module_id="platform", name="staff_payroll",
            ).exists(),
        )

    def test_the_still_active_conversion_keys_are_registered(self):
        for key in ACTIVE_SURVIVING_KEYS:
            with self.subTest(key=key):
                self.assertTrue(Permission.objects.filter(key=key).exists(), key)

    def test_the_view_sensitive_verb_is_still_earned(self):
        """The verb stays because a key that is not a field still uses it.

        ``platform.tasks.view_sensitive`` reads a background job's raw error and
        traceback, which is a page, not a field, and is audited on every read.
        """
        self.assertTrue(
            Permission.objects.filter(key="platform.tasks.view_sensitive").exists(),
        )


class RetiringKeysMigrationTests(TestCase):
    """Migration 0026 against a database that already granted the keys.

    The seeds cover a fresh install. A school running today holds grants,
    group memberships, prebuilt defaults, personal exceptions and possibly a
    pending role change naming one of these keys, and every one of those has to
    go with the key: two of them protect it, so leaving either behind would
    make the migration fail rather than silently half-run.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="bright-star", name="Bright Star")
        cls.tenant = cls.school.tenant
        cls.admin = make_school_admin(
            None, email="adaeze@bright-star.example.com", tenant=cls.tenant,
        )
        cls.role = make_role(cls.school, name="Bursar", key="bursar")

        cls.permissions = {}
        for key in RETIRED_KEYS:
            scope = (
                PermissionScope.PLATFORM
                if key.startswith("platform.")
                else PermissionScope.TENANT
            )
            # Unrestricted on purpose: a restricted key cannot be placed in a
            # group at all, and a group membership is one of the rows the
            # migration has to sweep, so the fixture has to be able to hold one.
            cls.permissions[key] = make_permission(
                key, scope=scope, sensitivity_level="SENSITIVE",
            )
        #: The key that must survive every delete below untouched.
        cls.bystander = make_permission("procurement.vendor.view")

        cls.group = PermissionGroup.objects.create(
            name="Legacy vendor bundle", scope=PermissionScope.TENANT, is_system=True,
        )
        cls.prebuilt = PrebuiltRoleTemplate.objects.create(
            key="legacy_bursar", name="Legacy Bursar", scope="institution",
        )
        cls.request = TenantRoleChangeRequest.objects.create(
            tenant=cls.tenant,
            requested_by=cls.admin,
            target_role=cls.role,
            justification="Give the bursar sight of vendor banking.",
            status="PENDING",
        )

        tenant_keys = [k for k in RETIRED_KEYS if not k.startswith("platform.")]
        for key in tenant_keys + ["procurement.vendor.view"]:
            make_role_permission(cls.role, cls.permissions.get(key) or cls.bystander)
            GroupPermission.objects.create(
                group=cls.group, permission=cls.permissions.get(key) or cls.bystander,
            )
            PrebuiltRolePermission.objects.create(
                prebuilt_role=cls.prebuilt,
                permission=cls.permissions.get(key) or cls.bystander,
            )
            UserPermissionOverride.objects.create(
                user=cls.admin,
                tenant=cls.tenant,
                permission=cls.permissions.get(key) or cls.bystander,
                mode=UserPermissionOverride.Mode.DENY,
                reason="Bursar is on leave.",
            )
            TenantRoleChangeDeltaItem.objects.create(
                request=cls.request,
                permission=cls.permissions.get(key) or cls.bystander,
                operation=TenantRoleChangeDeltaItem.Operation.ADD,
            )

    def test_it_deletes_the_permission_rows(self):
        _retire_them()
        self.assertFalse(Permission.objects.filter(key__in=RETIRED_KEYS).exists())

    def test_it_deletes_every_grant_membership_default_and_exception(self):
        _retire_them()
        for model in (
            TenantRolePermission,
            GroupPermission,
            PrebuiltRolePermission,
            UserPermissionOverride,
            TenantRoleChangeDeltaItem,
        ):
            with self.subTest(model=model.__name__):
                self.assertFalse(
                    model.objects.filter(permission_id__in=RETIRED_KEYS).exists(),
                )

    def test_it_leaves_every_other_key_untouched(self):
        _retire_them()
        self.assertTrue(Permission.objects.filter(key="procurement.vendor.view").exists())
        for model in (
            TenantRolePermission,
            GroupPermission,
            PrebuiltRolePermission,
            UserPermissionOverride,
            TenantRoleChangeDeltaItem,
        ):
            with self.subTest(model=model.__name__):
                self.assertTrue(
                    model.objects.filter(
                        permission_id="procurement.vendor.view",
                    ).exists(),
                )

    def test_the_pending_request_survives_without_the_line(self):
        """A request that asked for a retired key keeps its other lines.

        The line goes because the thing it asked for no longer exists to be
        approved, and the request stays because its remaining lines still do.
        """
        _retire_them()
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, "PENDING")
        self.assertEqual(
            sorted(self.request.delta_items.values_list("permission_id", flat=True)),
            ["procurement.vendor.view"],
        )

    def test_it_drops_the_resource_that_has_nothing_left(self):
        _retire_them()
        self.assertFalse(
            PermissionResource.objects.filter(
                module_id="platform", name="staff_payroll",
            ).exists(),
        )
        # And keeps one that still has keys under it.
        self.assertTrue(
            PermissionResource.objects.filter(
                module_id="procurement", name="vendor",
            ).exists(),
        )

    def test_it_is_safe_to_run_twice(self):
        """A migration re-run on a database that has already lost the keys."""
        _retire_them()
        _retire_them()
        self.assertFalse(Permission.objects.filter(key__in=RETIRED_KEYS).exists())
