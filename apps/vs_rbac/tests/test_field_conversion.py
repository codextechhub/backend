"""Turning today's field guarding permission keys into Field Access switches.

The conversion has one job: after it runs, every person reads and writes
exactly the fields they read and write now. These tests pin that job from both
sides. A role that holds a key ends up with the switch on; a role that lacks
one ends up off, including the case that needs a row written to say so,
because the field's default is open. A key reached through a permission group
counts, a direct deny still beats it, Codex's defaults travel to a school
created afterwards, and a personal exception arrives as a personal exception
with its mode, expiry, author and reason intact.

They also pin the two properties an operator relies on: running the conversion
twice writes nothing the second time, and reversing it leaves nothing behind.

Every behaviour is checked in a school with one branch and in a school with
two, because a branch pinned role is a different grant and the answer must not
depend on how many sites a school runs.
"""
from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from vs_rbac.field_conversion import (
    ACCEPTED_DIFFERENCES,
    CONVERSION_REASON_PREFIX,
    CONVERSIONS,
    READ,
    WRITE,
    WRITE_REACHED_BY,
    converted_field_keys,
    converted_keys,
    gate_index,
    reverse_conversion,
    run_conversion,
)
from vs_rbac.field_evaluator import get_field_access
from vs_rbac.field_registry import all_declarations
from vs_rbac.models import (
    FieldDefinition,
    GroupPermission,
    PermissionGroup,
    PermissionScope,
    PrebuiltRoleFieldAccess,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    RoleFieldAccess,
    UserFieldAccessOverride,
    UserPermissionOverride,
)
from vs_rbac.services import provision_role_from_prebuilt
from vs_user.models import User

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_group,
    make_role_permission,
    make_school,
    make_staff_user,
)

VENDOR_KEY = "procurement.vendor.view_sensitive"
VENDOR_BANK = "procurement.vendor.bank_account_number"
STUDENT_MEDICAL_KEY = "school.students.view_sensitive"
BLOOD_GROUP = "school.students.blood_group"
ENROLMENT_DATE = "school.students.enrolment_date"
STUDENT_MANAGE_KEY = "school.students.manage"
TEAM_KEY = "platform.team.view"
LAST_LOGIN = "platform.team.last_login_at"
BANK_NUMBER = "finance.bankaccount.account_number"

#: The keys that flow through approvals, so a personal ALLOW on one of them
#: confers nothing today and must not become a field exception.
RESTRICTED_KEYS = {
    "procurement.vendor.view_sensitive",
    "finance.bankaccount.view_sensitive",
    "finance.payrollrun.view_sensitive",
    "payments.virtual_account.view_sensitive",
    "payments.payout.view_sensitive",
    "school.students.view_sensitive",
    "school.students.manage",
    "platform.staff_payroll.view",
    "platform.staff_payroll.manage",
}


def declare_converted_fields() -> dict:
    """Write a ``FieldDefinition`` for every field the conversion touches.

    Built from the code registry rather than from literals, so a declaration
    that changes shape (a field that stops being sensitive, one that stops
    being writable) reaches these tests instead of being restated here.
    """
    from vs_rbac.models import PermissionModule, PermissionResource

    specs = {}
    for declaration in all_declarations():
        for spec in declaration.fields:
            specs[declaration.key_for(spec)] = (declaration, spec)

    rows = {}
    for key in converted_field_keys():
        declaration, spec = specs[key]
        module, _ = PermissionModule.objects.get_or_create(name=declaration.module)
        resource, _ = PermissionResource.objects.get_or_create(
            module=module, name=declaration.resource,
        )
        rows[key] = FieldDefinition.objects.create(
            resource=resource,
            name=spec.name,
            api_names=list(spec.resolved_api_names),
            label=spec.label,
            group=spec.group,
            description=spec.description,
            sensitive=spec.sensitive,
            writable=spec.writable,
            open_on_create=spec.open_on_create,
            scope=spec.scope,
            sort_order=spec.sort_order,
        )
    return rows


def declare_converted_permissions() -> dict:
    """A ``Permission`` row for every key the conversion reads.

    The write reaching keys come too: the verification command asks whether a
    caller can change a value today without being able to see it, and that
    question is about ``finance.bankaccount.update`` and its like.
    """
    keys = set(converted_keys())
    for reached_by in WRITE_REACHED_BY.values():
        keys.update(reached_by)
    return {
        key: make_permission(key, is_restricted=key in RESTRICTED_KEYS)
        for key in sorted(keys)
    }


class _Converted(TestCase):
    """A school, the registry it converts against, and the keys it reads."""

    branch_names = ("Main Branch",)

    def setUp(self):
        self.fields = declare_converted_fields()
        self.permissions = declare_converted_permissions()
        self.school = make_school(slug="fc-bright-star", name="Bright Star School")
        self.tenant = self.school.tenant
        self.branches = [
            make_branch(self.school, name=name, is_main=index == 0)
            for index, name in enumerate(self.branch_names)
        ]
        self.user = make_staff_user(self.branches[0], email="fc-user@test.com")

    def permission(self, key):
        """The permission row for *key*, minted on demand for an unrelated key."""
        if key not in self.permissions:
            self.permissions[key] = make_permission(key)
        return self.permissions[key]

    def role_with(self, *keys, name="Role", branch=None):
        role = make_role(self.tenant, name=f"{name} {len(keys)} {branch or ''}".strip(),
                         branch=branch)
        for key in keys:
            make_role_permission(role, self.permission(key))
        return role

    def hold(self, role, user=None):
        make_assignment(self.tenant, user or self.user, role)
        return role

    def access(self, user=None):
        person = User.objects.get(pk=(user or self.user).pk)
        return get_field_access(person, tenant=self.tenant)

    def state(self, field_key, user=None):
        access = self.access(user)
        return access.can_read(field_key), access.can_write(field_key)

    def row(self, role, field_key):
        return RoleFieldAccess.objects.filter(role=role, field_id=field_key).first()


class MappingTests(TestCase):
    """The table itself, before any row is written."""

    def test_every_converted_field_is_declared_in_code(self):
        declared = {
            declaration.key_for(spec)
            for declaration in all_declarations()
            for spec in declaration.fields
        }
        self.assertEqual(set(converted_field_keys()) - declared, set())

    def test_every_key_names_at_least_one_field_and_one_access(self):
        for entry in CONVERSIONS:
            self.assertTrue(entry.fields, entry.key)
            self.assertTrue(entry.gates <= {READ, WRITE}, entry.key)
            self.assertTrue(entry.gates, entry.key)

    def test_a_write_reaching_key_is_only_named_where_nothing_gates_the_write(self):
        gates = gate_index()
        for field_key in WRITE_REACHED_BY:
            self.assertNotIn(WRITE, gates[field_key], field_key)

    def test_every_accepted_difference_names_a_converted_field(self):
        converted = set(converted_field_keys())
        for entry in ACCEPTED_DIFFERENCES:
            self.assertIn(entry.field_key, converted)
            self.assertIn(entry.access, (READ, WRITE))
            self.assertNotEqual(entry.old, entry.new)
            self.assertTrue(entry.reason.strip())


class SingleBranchConversionTests(_Converted):
    """One school, one branch: the shape most schools have."""

    def test_a_role_holding_the_key_reads_and_writes_the_field(self):
        role = self.hold(self.role_with(VENDOR_KEY))
        run_conversion()
        self.assertEqual((True, True), self.state(VENDOR_BANK))
        row = self.row(role, VENDOR_BANK)
        self.assertEqual((True, True), (row.can_read, row.can_write))

    def test_a_role_without_the_key_reads_nothing_and_needs_no_row(self):
        role = self.hold(self.role_with("finance.bankaccount.view"))
        run_conversion()
        self.assertEqual((False, False), self.state(VENDOR_BANK))
        self.assertIsNone(self.row(role, VENDOR_BANK))

    def test_a_read_only_key_leaves_a_field_nothing_can_write_read_only(self):
        role = self.hold(self.role_with(TEAM_KEY))
        run_conversion()
        self.assertEqual((True, False), self.state(LAST_LOGIN))
        row = self.row(role, LAST_LOGIN)
        self.assertEqual((True, False), (row.can_read, row.can_write))

    def test_an_open_field_needs_a_row_to_say_the_write_is_off(self):
        """The case a missing row would widen: the default is on."""
        role = self.hold(self.role_with("school.students.view"))
        run_conversion()
        row = self.row(role, ENROLMENT_DATE)
        self.assertIsNotNone(row, "an open field needs an explicit off row")
        self.assertEqual((True, False), (row.can_read, row.can_write))
        self.assertEqual((True, False), self.state(ENROLMENT_DATE))

    def test_holding_the_write_key_leaves_the_open_field_at_its_default(self):
        role = self.hold(self.role_with(STUDENT_MANAGE_KEY))
        run_conversion()
        self.assertIsNone(self.row(role, ENROLMENT_DATE))
        self.assertEqual((True, True), self.state(ENROLMENT_DATE))

    def test_a_key_held_only_through_a_group_converts(self):
        group = PermissionGroup.objects.create(
            name="Staff account reading", scope=PermissionScope.TENANT, is_active=True,
        )
        GroupPermission.objects.create(group=group, permission=self.permissions[TEAM_KEY])
        role = self.hold(self.role_with(name="Grouped"))
        make_role_group(role, group)
        run_conversion()
        self.assertEqual((True, False), self.state(LAST_LOGIN))
        self.assertIsNotNone(self.row(role, LAST_LOGIN))

    def test_a_direct_deny_beats_the_group_grant(self):
        group = PermissionGroup.objects.create(
            name="Staff account reading", scope=PermissionScope.TENANT, is_active=True,
        )
        GroupPermission.objects.create(group=group, permission=self.permissions[TEAM_KEY])
        role = self.hold(self.role_with(name="Denied"))
        make_role_group(role, group)
        make_role_permission(role, self.permissions[TEAM_KEY], granted=False)
        run_conversion()
        self.assertEqual((False, False), self.state(LAST_LOGIN))
        self.assertIsNone(self.row(role, LAST_LOGIN))

    def test_a_platform_field_gets_no_row_on_a_school_role(self):
        self.hold(self.role_with("school.students.view"))
        run_conversion()
        self.assertFalse(
            RoleFieldAccess.objects.filter(
                role__tenant=self.tenant,
                field__key__startswith="platform.staff_profile.",
            ).exists()
        )

    def test_the_most_generous_role_wins_after_conversion(self):
        self.hold(self.role_with(VENDOR_KEY, name="Bursar"))
        self.hold(self.role_with("school.students.view", name="Storekeeper"))
        run_conversion()
        self.assertEqual((True, True), self.state(VENDOR_BANK))


class MultiBranchConversionTests(SingleBranchConversionTests):
    """The same answers in a school running two branches.

    Every role here is pinned to a branch, which is a different grant from a
    school wide one, and the switches must not notice. Inheriting the
    single branch cases is the point: one file, both shapes.
    """

    branch_names = ("Ikeja Branch", "Lekki Branch")

    def role_with(self, *keys, name="Role", branch=None):
        return super().role_with(*keys, name=name, branch=branch or self.branches[0])

    def test_a_role_at_another_branch_still_counts(self):
        """Roles pool across branches, so the far branch's key counts here."""
        self.hold(self.role_with(VENDOR_KEY, name="Lekki Bursar", branch=self.branches[1]))
        run_conversion()
        self.assertEqual((True, True), self.state(VENDOR_BANK))

    def test_a_branch_pinned_role_without_the_key_is_still_closed(self):
        self.hold(self.role_with("school.students.view", name="Ikeja Clerk"))
        self.hold(self.role_with("school.students.view", name="Lekki Clerk",
                                 branch=self.branches[1]))
        run_conversion()
        self.assertEqual((False, False), self.state(BLOOD_GROUP))
        self.assertEqual((True, False), self.state(ENROLMENT_DATE))


class PrebuiltRoleConversionTests(_Converted):
    """Codex's defaults, and the school created after the conversion."""

    def setUp(self):
        super().setUp()
        self.prebuilt = PrebuiltRoleTemplate.objects.create(
            key="fc-bursar", name="FC Bursar", scope="institution", is_active=True,
        )
        PrebuiltRolePermission.objects.create(
            prebuilt_role=self.prebuilt, permission=self.permissions[VENDOR_KEY],
        )

    def test_a_prebuilt_default_becomes_a_prebuilt_switch(self):
        run_conversion()
        row = PrebuiltRoleFieldAccess.objects.get(
            prebuilt_role=self.prebuilt, field_id=VENDOR_BANK,
        )
        self.assertEqual((True, True), (row.can_read, row.can_write))

    def test_a_prebuilt_role_without_the_key_still_closes_the_open_field(self):
        run_conversion()
        row = PrebuiltRoleFieldAccess.objects.get(
            prebuilt_role=self.prebuilt, field_id=ENROLMENT_DATE,
        )
        self.assertEqual((True, False), (row.can_read, row.can_write))

    def test_a_platform_field_never_reaches_a_prebuilt_tenant_blueprint(self):
        run_conversion()
        self.assertFalse(
            PrebuiltRoleFieldAccess.objects.filter(
                field__key__startswith="platform.staff_profile.",
            ).exists()
        )

    def test_a_role_provisioned_afterwards_carries_the_defaults(self):
        run_conversion()
        role = provision_role_from_prebuilt(
            tenant=self.tenant, prebuilt_key=self.prebuilt.key,
        )
        self.hold(role)
        self.assertEqual((True, True), self.state(VENDOR_BANK))
        self.assertEqual((True, False), self.state(ENROLMENT_DATE))


class OverrideConversionTests(_Converted):
    """A personal permission exception becomes a personal field exception."""

    def setUp(self):
        super().setUp()
        self.author = make_staff_user(self.branches[0], email="fc-author@test.com")
        self.expiry = timezone.now() + timedelta(days=14)

    def _override(self, key, mode, reason="Covering bursar duties 14-28 Sept"):
        return UserPermissionOverride.objects.create(
            tenant=self.tenant, user=self.user, permission=self.permissions[key],
            mode=mode, reason=reason, created_by=self.author, expires_at=self.expiry,
        )

    def test_a_deny_carries_its_mode_expiry_author_and_reason(self):
        self._override(STUDENT_MEDICAL_KEY, UserPermissionOverride.Mode.DENY)
        run_conversion()
        rows = UserFieldAccessOverride.objects.filter(
            user=self.user, field_id=BLOOD_GROUP,
        ).order_by("access")
        self.assertEqual(
            [UserFieldAccessOverride.Access.READ, UserFieldAccessOverride.Access.WRITE],
            [row.access for row in rows],
        )
        for row in rows:
            self.assertEqual(UserFieldAccessOverride.Mode.DENY, row.mode)
            self.assertEqual(self.expiry, row.expires_at)
            self.assertEqual(self.author, row.created_by)
            self.assertEqual(
                f"{CONVERSION_REASON_PREFIX}Covering bursar duties 14-28 Sept",
                row.reason,
            )

    def test_a_deny_beats_the_role_that_granted_the_key(self):
        self.hold(self.role_with(STUDENT_MEDICAL_KEY))
        self._override(STUDENT_MEDICAL_KEY, UserPermissionOverride.Mode.DENY)
        run_conversion()
        self.assertEqual((False, False), self.state(BLOOD_GROUP))

    def test_an_allow_on_an_ordinary_key_becomes_an_allow(self):
        self._override(TEAM_KEY, UserPermissionOverride.Mode.ALLOW)
        run_conversion()
        row = UserFieldAccessOverride.objects.get(user=self.user, field_id=LAST_LOGIN)
        self.assertEqual(UserFieldAccessOverride.Mode.ALLOW, row.mode)
        self.assertEqual(UserFieldAccessOverride.Access.READ, row.access)
        self.assertEqual((True, False), self.state(LAST_LOGIN))

    def test_an_allow_on_a_restricted_key_confers_nothing_and_is_left_behind(self):
        """It grants nothing today, so converting it would hand out access.

        The model refuses such a row and the evaluator ignores one anyway, so
        the only way to have one is the way a database gets one: written
        around the guard, by an older build or a restored backup. It is
        written that way here for the same reason the evaluator keeps its
        runtime backstop.
        """
        UserPermissionOverride.objects.get_queryset().bulk_create([
            UserPermissionOverride(
                tenant=self.tenant, user=self.user,
                permission=self.permissions[VENDOR_KEY],
                mode=UserPermissionOverride.Mode.ALLOW,
                reason="Restored from a backup", created_by=self.author,
            ),
        ])
        run_conversion()
        self.assertFalse(
            UserFieldAccessOverride.objects.filter(
                user=self.user, field_id=VENDOR_BANK,
            ).exists()
        )
        self.assertEqual((False, False), self.state(VENDOR_BANK))

    def test_an_exception_never_lands_on_a_field_nothing_can_write(self):
        self._override(TEAM_KEY, UserPermissionOverride.Mode.ALLOW)
        run_conversion()
        self.assertFalse(
            UserFieldAccessOverride.objects.filter(
                user=self.user, field_id=LAST_LOGIN,
                access=UserFieldAccessOverride.Access.WRITE,
            ).exists()
        )


class IdempotenceTests(_Converted):
    """Running it twice, and putting it back."""

    def setUp(self):
        super().setUp()
        self.role = self.hold(self.role_with(VENDOR_KEY))
        self.plain = self.hold(self.role_with("school.students.view", name="Clerk"))
        UserPermissionOverride.objects.create(
            tenant=self.tenant, user=self.user,
            permission=self.permissions[STUDENT_MEDICAL_KEY],
            mode=UserPermissionOverride.Mode.DENY, reason="On leave",
        )

    def test_a_second_run_writes_nothing(self):
        first = run_conversion()
        self.assertTrue(any(first.values()))
        before = (
            RoleFieldAccess.objects.count(),
            PrebuiltRoleFieldAccess.objects.count(),
            UserFieldAccessOverride.objects.count(),
        )
        second = run_conversion()
        self.assertEqual({key: 0 for key in second}, second)
        self.assertEqual(before, (
            RoleFieldAccess.objects.count(),
            PrebuiltRoleFieldAccess.objects.count(),
            UserFieldAccessOverride.objects.count(),
        ))

    def test_a_rerun_leaves_an_administrator_s_own_decision_alone(self):
        run_conversion()
        row = self.row(self.role, VENDOR_BANK)
        RoleFieldAccess.objects.filter(pk=row.pk).update(can_read=True, can_write=False)
        run_conversion()
        row.refresh_from_db()
        self.assertEqual((True, False), (row.can_read, row.can_write))

    def test_the_reverse_leaves_no_rows_behind(self):
        run_conversion()
        reverse_conversion()
        self.assertEqual(0, RoleFieldAccess.objects.filter(
            field_id__in=converted_field_keys()).count())
        self.assertEqual(0, PrebuiltRoleFieldAccess.objects.filter(
            field_id__in=converted_field_keys()).count())
        self.assertEqual(0, UserFieldAccessOverride.objects.filter(
            reason__startswith=CONVERSION_REASON_PREFIX).count())

    def test_the_reverse_spares_a_switch_the_conversion_never_planned(self):
        run_conversion()
        kept = RoleFieldAccess.objects.create(
            role=self.plain, field_id=VENDOR_BANK, can_read=True, can_write=True,
        )
        reverse_conversion()
        self.assertTrue(RoleFieldAccess.objects.filter(pk=kept.pk).exists())


class VerifyCommandTests(_Converted):
    """The snapshot and compare pair, on data the conversion produced."""

    branch_names = ("Ikeja Branch", "Lekki Branch")

    def setUp(self):
        super().setUp()
        self.snapshot = self._snapshot_path()
        self.hold(self.role_with(VENDOR_KEY, STUDENT_MEDICAL_KEY, STUDENT_MANAGE_KEY))
        self.clerk = make_staff_user(self.branches[1], email="fc-clerk@test.com")
        self.hold(self.role_with("school.students.view", name="Clerk"), user=self.clerk)
        self.roleless = make_staff_user(self.branches[0], email="fc-new@test.com")

    def _snapshot_path(self):
        import tempfile

        directory = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        return f"{directory}/snapshot.json"

    def _run(self, *args):
        call_command("verify_field_access_conversion", *args, "--path", self.snapshot)

    def test_the_conversion_preserves_what_every_user_had(self):
        self._run("--snapshot")
        run_conversion()
        self._run("--compare")

    def test_a_flipped_switch_fails_the_check(self):
        self._run("--snapshot")
        run_conversion()
        RoleFieldAccess.objects.filter(field_id=VENDOR_BANK).update(
            can_read=False, can_write=False,
        )
        with self.assertRaises(CommandError) as raised:
            self._run("--compare")
        self.assertIn("not allowed to make", str(raised.exception))

    def test_the_declared_tightening_on_a_bank_number_is_allowed(self):
        """A role that may change a number it cannot see stops being able to."""
        self.hold(self.role_with("finance.bankaccount.update", name="Bank Editor"),
                  user=self.clerk)
        self._run("--snapshot")
        run_conversion()
        self.assertEqual((False, False), self.state(BANK_NUMBER, user=self.clerk))
        self._run("--compare")

    def test_a_snapshot_from_another_format_is_refused(self):
        from pathlib import Path

        Path(self.snapshot).write_text('{"format": 0, "users": {}}')
        with self.assertRaises(CommandError):
            self._run("--compare")

    def test_choosing_neither_mode_is_refused(self):
        with self.assertRaises(CommandError):
            call_command("verify_field_access_conversion", "--path", self.snapshot)
