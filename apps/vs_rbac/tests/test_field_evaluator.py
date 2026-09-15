"""Field Access evaluation, the switch guards, and provisioning from prebuilt roles.

The evaluator decides who reads and writes each registered field. What these
tests pin is the decision table: defaults by kind of field, the most generous
role winning (in a single-branch school and a multi-branch one), a personal
DENY beating everything, the fixed bypasses, and a cost that stays flat however
many roles, fields and exceptions a person has.

The guard tests pin the rows that could never be honoured: a platform field on
a school role, and a write switch on a field nothing can write.
"""
import itertools
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from vs_rbac.field_evaluator import (
    FieldAccessMap,
    get_field_access,
    get_role_field_access,
)
from vs_rbac.models import (
    FieldDefinition,
    PermissionRegistryRevision,
    PermissionScope,
    PrebuiltRoleFieldAccess,
    PrebuiltRoleTemplate,
    RoleFieldAccess,
    UserFieldAccessOverride,
)
from vs_rbac.services import provision_role_from_prebuilt
from vs_tenants.models import Branch
from vs_user.models import User

from .helpers import (
    make_assignment,
    make_branch,
    make_field_definition,
    make_platform_assignment,
    make_platform_role,
    make_role,
    make_school,
    make_staff_user,
    make_vision_user,
    platform_tenant,
)

_counter = itertools.count(1)

READ, WRITE = UserFieldAccessOverride.Access.READ, UserFieldAccessOverride.Access.WRITE
ALLOW, DENY = UserFieldAccessOverride.Mode.ALLOW, UserFieldAccessOverride.Mode.DENY


def _fresh(user):
    """The user re-read, so no memoised map from an earlier call survives."""
    return User.objects.get(pk=user.pk)


def _row(role, field, read, write):
    return RoleFieldAccess.objects.create(
        role=role, field=field, can_read=read, can_write=write,
    )


def _pair(access, field):
    return access.can_read(field.key), access.can_write(field.key)


class _Fields(TestCase):
    """A school, one person in it, and one field of every kind."""

    def setUp(self):
        self.school = make_school(slug="fe-bright-star", name="Bright Star School")
        self.ikeja = make_branch(self.school, name="Ikeja Branch")
        self.tenant = self.school.tenant
        self.name = make_field_definition("fetest.vendor.name", "Name")
        self.bank = make_field_definition(
            "fetest.vendor.bank_account_number", "Bank account number", sensitive=True,
        )
        self.total = make_field_definition(
            "fetest.vendor.total_spend", "Total spend", writable=False,
        )
        self.payroll = make_field_definition(
            "fetest.staff.bank_name", "Bank name",
            sensitive=True, scope=PermissionScope.PLATFORM,
        )
        self.user = make_staff_user(self.ikeja, email="fe-user@test.com")

    def _role(self, tenant=None, **kwargs):
        return make_role(tenant or self.tenant, name=f"FE Role {next(_counter)}", **kwargs)

    def _hold(self, role, user=None, **kwargs):
        make_assignment(role.tenant, user or self.user, role, **kwargs)
        return role

    def _exception(self, field, access, mode, user=None, **kwargs):
        user = user or self.user
        kwargs.setdefault("tenant", user.tenant)
        return UserFieldAccessOverride.objects.create(
            user=user, field=field, access=access, mode=mode,
            reason="Covering bursar duties.", **kwargs,
        )

    def _access(self, user=None, tenant=None):
        return get_field_access(_fresh(user or self.user), tenant=tenant or self.tenant)


class DefaultsTests(_Fields):
    def test_a_person_with_no_roles_gets_the_defaults(self):
        access = self._access()
        self.assertEqual(_pair(access, self.name), (True, True))
        self.assertEqual(_pair(access, self.bank), (False, False))
        self.assertEqual(_pair(access, self.total), (True, False))

    def test_a_role_without_a_row_gives_the_defaults(self):
        self._hold(self._role())
        access = self._access()
        self.assertEqual(_pair(access, self.name), (True, True))
        self.assertEqual(_pair(access, self.bank), (False, False))
        self.assertEqual(_pair(access, self.total), (True, False))

    def test_a_role_row_replaces_the_default(self):
        role = self._hold(self._role())
        _row(role, self.bank, True, False)
        _row(role, self.name, False, False)
        access = self._access()
        self.assertEqual(_pair(access, self.bank), (True, False))
        self.assertEqual(_pair(access, self.name), (False, False))

    def test_a_platform_field_is_closed_inside_a_school(self):
        self.assertEqual(_pair(self._access(), self.payroll), (False, False))


class MostGenerousRoleWinsTests(_Fields):
    def test_single_branch_school_pools_a_whole_school_role_and_a_pinned_one(self):
        whole = self._hold(self._role())
        pinned = self._hold(self._role(branch=self.ikeja))
        _row(whole, self.name, False, False)
        _row(whole, self.bank, True, False)
        _row(pinned, self.name, True, True)

        access = self._access()
        self.assertEqual(_pair(access, self.name), (True, True))
        # The pinned role has no bank row, so it lends the closed default.
        self.assertEqual(_pair(access, self.bank), (True, False))

    def test_a_role_without_a_row_lends_its_default_to_the_pool(self):
        closed = self._hold(self._role())
        self._hold(self._role())
        _row(closed, self.name, False, False)
        self.assertEqual(_pair(self._access(), self.name), (True, True))

    def test_multi_branch_school_counts_every_branch_until_one_leaves_service(self):
        """Mrs Adeyemi: School Nurse at Ikeja, Teacher at Lekki.

        Nurse opens the bank field and Teacher leaves it at its closed default.
        Her roles are pooled, so she reads it. Once Ikeja is suspended, the
        Nurse grant stops counting and the field closes again.
        """
        lekki = make_branch(self.school, name="Lekki Branch", is_main=False)
        nurse = self._hold(self._role(branch=self.ikeja))
        self._hold(self._role(branch=lekki))
        _row(nurse, self.bank, True, False)

        self.assertEqual(_pair(self._access(), self.bank), (True, False))

        Branch.objects.filter(pk=self.ikeja.pk).update(status="SUSPENDED")
        self.assertEqual(_pair(self._access(), self.bank), (False, False))

    def test_a_revoked_assignment_stops_counting(self):
        role = self._role()
        _row(role, self.bank, True, True)
        assignment = make_assignment(self.tenant, self.user, role)
        self.assertEqual(_pair(self._access(), self.bank), (True, True))
        assignment.revoke()
        assignment.save()
        self.assertEqual(_pair(self._access(), self.bank), (False, False))


class PersonalExceptionTests(_Fields):
    def test_deny_read_beats_a_role_grant_and_an_allow_write(self):
        role = self._hold(self._role())
        _row(role, self.bank, True, True)
        self._exception(self.bank, WRITE, ALLOW)
        self._exception(self.bank, READ, DENY)
        self.assertEqual(_pair(self._access(), self.bank), (False, False))

    def test_deny_write_beats_a_role_write(self):
        self._exception(self.name, WRITE, DENY)
        self.assertEqual(_pair(self._access(), self.name), (True, False))

    def test_allow_write_implies_read(self):
        self._exception(self.bank, WRITE, ALLOW)
        self.assertEqual(_pair(self._access(), self.bank), (True, True))

    def test_allow_read_grants_read_only(self):
        self._exception(self.bank, READ, ALLOW)
        self.assertEqual(_pair(self._access(), self.bank), (True, False))

    def test_allow_write_cannot_make_a_non_writable_field_writable(self):
        self._exception(self.total, READ, ALLOW)
        self.assertEqual(_pair(self._access(), self.total), (True, False))

    def test_an_expired_exception_stops_applying_without_a_sweep(self):
        self._exception(
            self.name, READ, DENY, expires_at=timezone.now() - timedelta(minutes=1),
        )
        self.assertEqual(_pair(self._access(), self.name), (True, True))
        self.assertEqual(UserFieldAccessOverride.objects.count(), 1)

    def test_an_unexpired_exception_applies(self):
        self._exception(
            self.name, READ, DENY, expires_at=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(_pair(self._access(), self.name), (False, False))

    def test_an_exception_recorded_in_another_tenant_is_ignored(self):
        other = make_school(slug="fe-greenfield", name="Greenfield School")
        self._exception(self.name, READ, DENY, tenant=other.tenant)
        self.assertEqual(_pair(self._access(), self.name), (True, True))

    def test_role_state_ignores_exceptions(self):
        self._exception(self.name, READ, DENY)
        role_only = get_role_field_access(_fresh(self.user), tenant=self.tenant)
        self.assertEqual(role_only.state(self.name.key), {"read": True, "write": True})
        self.assertEqual(_pair(self._access(), self.name), (False, False))


class FixedRulesTests(_Fields):
    def test_vision_super_admin_reads_and_writes_everything(self):
        admin = make_vision_user(email="fe-super@test.com", super_admin=True)
        access = self._access(user=admin, tenant=admin.tenant)
        for field in (self.name, self.bank, self.total, self.payroll):
            self.assertEqual(_pair(access, field), (True, True), field.key)

    def test_a_platform_role_can_open_a_platform_field(self):
        cx = make_vision_user(email="fe-cx@test.com")
        role = make_platform_role(name="FE Payroll Officer")
        make_platform_assignment(cx, role)
        tenant = platform_tenant()
        self.assertEqual(_pair(self._access(user=cx, tenant=tenant), self.payroll), (False, False))
        _row(role, self.payroll, True, True)
        self.assertEqual(_pair(self._access(user=cx, tenant=tenant), self.payroll), (True, True))

    def test_an_unregistered_key_is_open(self):
        access = self._access()
        self.assertNotIn("fetest.vendor.nothing", access)
        self.assertEqual(access.state("fetest.vendor.nothing"), {"read": True, "write": True})

    def test_an_inactive_field_is_not_in_the_map(self):
        FieldDefinition.objects.filter(key=self.bank.key).update(is_active=False)
        access = self._access()
        self.assertNotIn(self.bank.key, access)
        self.assertEqual(_pair(access, self.bank), (True, True))

    def test_an_identity_from_another_tenant_gets_every_field_closed(self):
        other = make_school(slug="fe-elsewhere", name="Elsewhere School")
        access = get_field_access(_fresh(self.user), tenant=other.tenant)
        self.assertEqual(_pair(access, self.name), (False, False))
        self.assertTrue(access.can_read("fetest.vendor.nothing"))

    def test_the_map_is_immutable(self):
        access = self._access()
        with self.assertRaises(AttributeError):
            access._states = {}
        with self.assertRaises(TypeError):
            access._states[self.name.key] = (False, False)
        self.assertIsInstance(access, FieldAccessMap)


class MemoAndCostTests(_Fields):
    def test_the_memo_is_reused_until_the_registry_revision_moves(self):
        PermissionRegistryRevision.objects.get_or_create(pk=1)
        user = _fresh(self.user)
        first = get_field_access(user, tenant=self.tenant)
        self.assertFalse(first.can_read(self.bank.key))

        self._exception(self.bank, READ, ALLOW)
        with self.assertNumQueries(1):
            again = get_field_access(user, tenant=self.tenant)
        self.assertIs(again, first)

        PermissionRegistryRevision.bump()
        self.assertTrue(get_field_access(user, tenant=self.tenant).can_read(self.bank.key))

    def _cold_queries(self):
        user = _fresh(self.user)
        with CaptureQueriesContext(connection) as queries:
            get_field_access(user, tenant=self.tenant)
        return len(queries)

    def test_a_cold_call_costs_three_queries_however_much_there_is(self):
        self._hold(self._role())
        small = self._cold_queries()
        self.assertEqual(small, 3)

        extra_fields = [
            make_field_definition(f"fetest.vendor.extra_{n}", f"Extra {n}", sensitive=n % 2 == 0)
            for n in range(6)
        ]
        for _ in range(4):
            role = self._hold(self._role())
            for field in [self.name, self.bank, *extra_fields]:
                _row(role, field, True, field.writable)
        self._exception(self.name, READ, DENY)
        self._exception(self.bank, WRITE, ALLOW)
        self._exception(extra_fields[0], READ, ALLOW)

        self.assertEqual(self._cold_queries(), small)


class RoleFieldAccessGuardTests(_Fields):
    def test_a_platform_field_is_refused_on_a_school_role(self):
        role = self._role()
        with self.assertRaises(ValidationError):
            _row(role, self.payroll, True, False)
        with self.assertRaises(ValidationError):
            RoleFieldAccess.objects.bulk_create([
                RoleFieldAccess(role=role, field=self.payroll, can_read=True),
            ])
        self.assertFalse(RoleFieldAccess.objects.exists())

    def test_a_platform_role_may_hold_a_platform_field(self):
        role = make_platform_role(name="FE Platform Payroll")
        _row(role, self.payroll, True, True)
        self.assertTrue(RoleFieldAccess.objects.filter(role=role).exists())

    def test_a_write_switch_on_a_non_writable_field_is_refused(self):
        role = self._role()
        with self.assertRaises(ValidationError):
            _row(role, self.total, True, True)
        with self.assertRaises(ValidationError):
            RoleFieldAccess.objects.bulk_create([
                RoleFieldAccess(role=role, field=self.total, can_read=True, can_write=True),
            ])
        _row(role, self.total, True, False)

    def test_clean_runs_the_guard(self):
        row = RoleFieldAccess(role=self._role(), field=self.payroll, can_read=True)
        with self.assertRaises(ValidationError):
            row.clean()

    def test_the_database_refuses_write_without_read(self):
        row = _row(self._role(), self.name, True, True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoleFieldAccess.objects.filter(pk=row.pk).update(can_read=False)

    def test_one_row_per_role_and_field(self):
        role = self._role()
        _row(role, self.name, True, True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _row(role, self.name, False, False)


class PrebuiltRoleFieldAccessGuardTests(_Fields):
    def setUp(self):
        super().setUp()
        self.prebuilt = PrebuiltRoleTemplate.objects.create(
            key="fe_guard_prebuilt", name="FE Guard", scope="institution",
        )

    def test_a_platform_field_is_refused(self):
        with self.assertRaises(ValidationError):
            PrebuiltRoleFieldAccess.objects.create(
                prebuilt_role=self.prebuilt, field=self.payroll, can_read=True,
            )
        with self.assertRaises(ValidationError):
            PrebuiltRoleFieldAccess.objects.bulk_create([
                PrebuiltRoleFieldAccess(prebuilt_role=self.prebuilt, field=self.payroll),
            ])

    def test_a_write_switch_on_a_non_writable_field_is_refused(self):
        with self.assertRaises(ValidationError):
            PrebuiltRoleFieldAccess.objects.create(
                prebuilt_role=self.prebuilt, field=self.total, can_read=True, can_write=True,
            )

    def test_the_database_refuses_write_without_read(self):
        row = PrebuiltRoleFieldAccess.objects.create(
            prebuilt_role=self.prebuilt, field=self.name, can_read=True, can_write=True,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PrebuiltRoleFieldAccess.objects.filter(pk=row.pk).update(can_read=False)


class UserFieldAccessOverrideGuardTests(_Fields):
    def test_any_exception_on_a_platform_field_is_refused_in_a_school(self):
        for mode in (ALLOW, DENY):
            with self.assertRaises(ValidationError):
                self._exception(self.payroll, READ, mode)
        with self.assertRaises(ValidationError):
            UserFieldAccessOverride.objects.bulk_create([
                UserFieldAccessOverride(
                    tenant=self.tenant, user=self.user, field=self.payroll,
                    access=READ, mode=DENY, reason="Bulk.",
                ),
            ])
        self.assertFalse(UserFieldAccessOverride.objects.exists())

    def test_the_platform_tenant_may_record_one(self):
        cx = make_vision_user(email="fe-cx-exception@test.com")
        self._exception(self.payroll, READ, ALLOW, user=cx)
        self.assertTrue(UserFieldAccessOverride.objects.filter(user=cx).exists())

    def test_allow_write_on_a_non_writable_field_is_refused_and_deny_write_is_not(self):
        with self.assertRaises(ValidationError):
            self._exception(self.total, WRITE, ALLOW)
        self._exception(self.total, WRITE, DENY)

    def test_clean_refuses_a_user_from_another_tenant_and_a_blank_reason(self):
        other = make_school(slug="fe-clean-other", name="Clean Other School")
        row = UserFieldAccessOverride(
            tenant=other.tenant, user=self.user, field=self.name,
            access=READ, mode=DENY, reason="Mismatch.",
        )
        with self.assertRaises(ValidationError):
            row.clean()
        blank = UserFieldAccessOverride(
            tenant=self.tenant, user=self.user, field=self.name,
            access=READ, mode=DENY, reason="   ",
        )
        with self.assertRaises(ValidationError):
            blank.clean()

    def test_one_exception_per_user_field_and_access(self):
        self._exception(self.name, READ, DENY)
        self._exception(self.name, WRITE, ALLOW)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._exception(self.name, READ, ALLOW)


class ProvisioningCopiesFieldDefaultsTests(TestCase):
    """A role created from a prebuilt template starts with Codex's field switches."""

    def setUp(self):
        self.school = make_school(slug="fe-provision", name="Provisioning School")
        make_branch(self.school)
        self.prebuilt = PrebuiltRoleTemplate.objects.create(
            key="fe_storekeeper", name="FE Storekeeper", scope="institution",
        )
        self.name = make_field_definition("feprov.vendor.name", "Name")
        self.bank = make_field_definition(
            "feprov.vendor.bank_account_number", "Bank account number", sensitive=True,
        )
        self.retired = make_field_definition("feprov.vendor.fax", "Fax")
        self.reclassified = make_field_definition("feprov.vendor.tax_id", "Tax ID")
        self.frozen = make_field_definition("feprov.vendor.rating", "Rating")

        for field, read, write in (
            (self.name, True, False),
            (self.bank, True, True),
            (self.retired, True, True),
            (self.reclassified, True, False),
            (self.frozen, True, True),
        ):
            PrebuiltRoleFieldAccess.objects.create(
                prebuilt_role=self.prebuilt, field=field, can_read=read, can_write=write,
            )
        # Changes made to the registry after the defaults were written.
        FieldDefinition.objects.filter(key=self.retired.key).update(is_active=False)
        FieldDefinition.objects.filter(key=self.reclassified.key).update(
            scope=PermissionScope.PLATFORM,
        )
        FieldDefinition.objects.filter(key=self.frozen.key).update(writable=False)

    def _switches(self, role):
        return {
            row.field_id: (row.can_read, row.can_write)
            for row in RoleFieldAccess.objects.filter(role=role)
        }

    def test_a_school_role_gets_the_rows_it_can_hold(self):
        role = provision_role_from_prebuilt(
            tenant=self.school.tenant, prebuilt_key=self.prebuilt.key,
        )
        self.assertEqual(self._switches(role), {
            self.name.key: (True, False),
            self.bank.key: (True, True),
            self.frozen.key: (True, False),
        })

    def test_the_platform_tenant_keeps_a_platform_scoped_default(self):
        role = provision_role_from_prebuilt(
            tenant=platform_tenant(), prebuilt_key=self.prebuilt.key,
        )
        self.assertIn(self.reclassified.key, self._switches(role))
        self.assertNotIn(self.retired.key, self._switches(role))

    def test_an_existing_role_is_not_rewritten(self):
        role = provision_role_from_prebuilt(
            tenant=self.school.tenant, prebuilt_key=self.prebuilt.key,
        )
        RoleFieldAccess.objects.filter(role=role).delete()
        provision_role_from_prebuilt(
            tenant=self.school.tenant, prebuilt_key=self.prebuilt.key,
        )
        self.assertEqual(self._switches(role), {})
