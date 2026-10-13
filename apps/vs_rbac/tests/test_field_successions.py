"""A field split into finer ones carries every school's decisions onto each of them.

A vendor's contact people were one switch, ``procurement.vendor.contacts``. They
are now three: ``procurement.vendor_contact.name``, ``.email`` and ``.phone``.
Bright Star School had opened the old switch for its Procurement Officer and
left it shut for its Storekeeper; Mr Bello has a personal exception that lets
him read contacts although his role does not. After the split the Procurement
Officer must read and change all three details, the Storekeeper none of them,
and Mr Bello keeps his exception on each, with nobody asked to set anything
again.

The carry-over runs inside ``sync_field_registry`` once the new fields exist
and removes the old switches as it copies them, which is what makes it happen
once. These tests prove the copy, the once, the order independence (the old
switches wait if the new fields are not synced yet), and that a decision made
on a new field is never overwritten by an inherited one.
"""
from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from vs_rbac.field_conversion import (
    CONVERSIONS,
    SUCCESSIONS,
    carry_over_successions,
    converted_field_keys,
    successor_keys,
)
from vs_rbac.field_evaluator import get_field_access
from vs_rbac.field_registry import all_declarations, get_declaration
from vs_rbac.models import (
    FieldDefinition,
    PermissionResource,
    PrebuiltRoleFieldAccess,
    PrebuiltRoleTemplate,
    RBACAuditLog,
    RoleFieldAccess,
    UserFieldAccessOverride,
)
from vs_user.models import User

from .helpers import (
    make_assignment,
    make_field_definition,
    make_permission,
    make_role,
    make_school,
    make_staff_user,
    make_branch,
)

SYNC = "vs_rbac.management.commands.sync_field_registry.all_declarations"
OLD = "procurement.vendor.contacts"
NEW = (
    "procurement.vendor_contact.name",
    "procurement.vendor_contact.email",
    "procurement.vendor_contact.phone",
)


class SuccessionRecordTests(SimpleTestCase):
    """The written record of the split agrees with the registry and the conversion."""

    def declared(self):
        return {
            declaration.key_for(spec): spec
            for declaration in all_declarations() for spec in declaration.fields
        }

    def test_the_old_field_is_no_longer_declared_and_each_new_one_is(self):
        declared = self.declared()
        for succession in SUCCESSIONS:
            with self.subTest(old=succession.old_key):
                self.assertNotIn(succession.old_key, declared)
                for key in succession.new_keys:
                    self.assertIn(key, declared)

    def test_a_successor_is_as_closed_and_as_writable_as_the_field_it_replaced(self):
        """The old contacts switch was sensitive, so a role with no row stays shut out."""
        declared = self.declared()
        for key in NEW:
            self.assertTrue(declared[key].sensitive, key)
            self.assertTrue(declared[key].writable, key)
            self.assertEqual(declared[key].read_by, (), key)

    def test_the_old_keys_conversion_is_mapped_forward_onto_the_successors(self):
        """History is kept: the legacy key still converts, onto the fields that replaced contacts."""
        entry = next(e for e in CONVERSIONS if e.key == "procurement.vendor.view_sensitive")
        self.assertNotIn(OLD, entry.fields)
        for key in NEW:
            self.assertIn(key, entry.fields)
        self.assertTrue(set(NEW) <= set(converted_field_keys()))
        self.assertEqual(set(successor_keys()), set(NEW))

    def test_a_split_field_is_never_also_a_converted_field(self):
        self.assertNotIn(OLD, converted_field_keys())


class _Carried(TestCase):
    """A school with the old switch set four ways, and the new fields declared."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="succ-bright-star", name="Bright Star School")
        cls.tenant = cls.school.tenant
        cls.branch = make_branch(cls.school, name="Main Branch")
        cls.other_school = make_school(slug="succ-greenfield", name="Greenfield School")
        cls.other_tenant = cls.other_school.tenant
        cls.other_branch = make_branch(cls.other_school, name="Main Branch")

        make_permission("procurement.vendor.view")
        make_field_definition(
            OLD, "Contact people", sensitive=True, writable=True,
        )
        cls.new_fields = {}
        resource, _ = PermissionResource.objects.get_or_create(
            module_id="procurement", name="vendor_contact",
        )
        declaration = get_declaration("procurement", "vendor_contact")
        for spec in declaration.fields:
            cls.new_fields[declaration.key_for(spec)] = FieldDefinition.objects.create(
                resource=resource, name=spec.name, label=spec.label, group=spec.group,
                sensitive=spec.sensitive, writable=spec.writable, scope=spec.scope,
                sort_order=spec.sort_order, api_names=list(spec.resolved_api_names),
            )

        cls.opened = cls.role(cls.tenant, "Procurement Officer", read=True, write=True)
        cls.read_only = cls.role(cls.tenant, "Auditor", read=True, write=False)
        cls.closed = cls.role(cls.tenant, "Storekeeper", read=False, write=False)
        cls.unset = make_role(cls.tenant, name="Clerk")
        cls.elsewhere = cls.role(cls.other_tenant, "Procurement Officer", read=True, write=True)

        cls.prebuilt = PrebuiltRoleTemplate.objects.create(
            key="succ-officer", name="Officer", scope="institution", is_active=True,
        )
        PrebuiltRoleFieldAccess.objects.create(
            prebuilt_role=cls.prebuilt, field_id=OLD, can_read=True, can_write=False,
        )

        cls.bello = make_staff_user(cls.branch, email="bello@succ.test")
        cls.reason = "Covers the sourcing desk while Mrs Okafor is on leave."
        cls.expiry = timezone.now() + timedelta(days=30)
        UserFieldAccessOverride.objects.create(
            tenant=cls.tenant, user=cls.bello, field_id=OLD, access="READ", mode="ALLOW",
            reason=cls.reason, expires_at=cls.expiry,
        )
        cls.tunde = make_staff_user(cls.branch, email="tunde@succ.test")
        UserFieldAccessOverride.objects.create(
            tenant=cls.tenant, user=cls.tunde, field_id=OLD, access="READ", mode="DENY",
            reason="Left the desk",
        )

    @classmethod
    def role(cls, tenant, name, *, read, write):
        role = make_role(tenant, name=name)
        RoleFieldAccess.objects.create(
            role=role, field_id=OLD, can_read=read, can_write=write,
        )
        return role

    def state(self, role, key):
        row = RoleFieldAccess.objects.filter(role=role, field_id=key).first()
        return None if row is None else (row.can_read, row.can_write)

    def sync(self):
        declarations = [get_declaration("procurement", "vendor_contact")]
        out = StringIO()
        with mock.patch(SYNC, return_value=declarations):
            call_command("sync_field_registry", stdout=out)
        return out.getvalue()


class CarryOverTests(_Carried):
    def test_every_role_switch_lands_on_each_new_field(self):
        carry_over_successions()
        for key in NEW:
            with self.subTest(field=key):
                self.assertEqual(self.state(self.opened, key), (True, True))
                self.assertEqual(self.state(self.read_only, key), (True, False))
                self.assertEqual(self.state(self.closed, key), (False, False))
                self.assertEqual(self.state(self.elsewhere, key), (True, True))
                self.assertIsNone(self.state(self.unset, key))

    def test_the_old_switches_are_removed_as_they_are_copied(self):
        carry_over_successions()
        self.assertFalse(RoleFieldAccess.objects.filter(field_id=OLD).exists())
        self.assertFalse(PrebuiltRoleFieldAccess.objects.filter(field_id=OLD).exists())
        self.assertFalse(UserFieldAccessOverride.objects.filter(field_id=OLD).exists())

    def test_a_library_default_carries_so_a_new_school_starts_where_an_old_one_lands(self):
        carry_over_successions()
        for key in NEW:
            row = PrebuiltRoleFieldAccess.objects.get(prebuilt_role=self.prebuilt, field_id=key)
            self.assertEqual((row.can_read, row.can_write), (True, False))

    def test_a_personal_exception_keeps_its_mode_expiry_author_and_reason(self):
        carry_over_successions()
        for key in NEW:
            allow = UserFieldAccessOverride.objects.get(
                user=self.bello, field_id=key, access="READ",
            )
            self.assertEqual(allow.mode, "ALLOW")
            self.assertEqual(allow.reason, self.reason)
            self.assertEqual(allow.expires_at, self.expiry)
            self.assertEqual(allow.tenant_id, self.tenant.pk)
            deny = UserFieldAccessOverride.objects.get(
                user=self.tunde, field_id=key, access="READ",
            )
            self.assertEqual(deny.mode, "DENY")

    def test_what_a_person_can_do_is_the_same_after_the_split_as_before(self):
        """Mr Bello reads contacts through his exception; the Storekeeper does not."""
        officer = make_staff_user(self.branch, email="officer@succ.test")
        make_assignment(self.tenant, officer, self.opened)
        storekeeper = make_staff_user(self.branch, email="store@succ.test")
        make_assignment(self.tenant, storekeeper, self.closed)
        make_assignment(self.tenant, self.bello, self.closed)

        def reads(user):
            access = get_field_access(User.objects.get(pk=user.pk), tenant=self.tenant)
            return [access.can_read(key) for key in NEW]

        def writes(user):
            access = get_field_access(User.objects.get(pk=user.pk), tenant=self.tenant)
            return [access.can_write(key) for key in NEW]

        carry_over_successions()
        self.assertEqual(reads(officer), [True, True, True])
        self.assertEqual(writes(officer), [True, True, True])
        self.assertEqual(reads(storekeeper), [False, False, False])
        self.assertEqual(reads(self.bello), [True, True, True])
        self.assertEqual(writes(self.bello), [False, False, False])

    def test_a_second_run_writes_nothing_and_does_not_hand_back_a_reset_decision(self):
        carry_over_successions()
        # The school resets Name to its closed default, which deletes the row.
        RoleFieldAccess.objects.filter(
            role=self.opened, field_id="procurement.vendor_contact.name",
        ).delete()
        with CaptureQueriesContext(connection) as queries:
            again = carry_over_successions()
        self.assertEqual(sum(again.values()), 0)
        writes = [
            q["sql"] for q in queries.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(writes, [])
        self.assertIsNone(self.state(self.opened, "procurement.vendor_contact.name"))
        self.assertEqual(self.state(self.opened, "procurement.vendor_contact.email"), (True, True))

    def test_a_decision_already_made_on_a_new_field_is_not_overwritten(self):
        RoleFieldAccess.objects.create(
            role=self.opened, field_id="procurement.vendor_contact.phone",
            can_read=False, can_write=False,
        )
        carry_over_successions()
        self.assertEqual(self.state(self.opened, "procurement.vendor_contact.phone"), (False, False))
        self.assertEqual(self.state(self.opened, "procurement.vendor_contact.name"), (True, True))

    def test_the_old_switches_wait_when_a_new_field_is_not_synced_yet(self):
        """Run before the sync, it moves nothing and loses nothing."""
        FieldDefinition.objects.filter(key=NEW[2]).delete()
        moved = carry_over_successions()
        self.assertEqual(sum(moved.values()), 0)
        self.assertEqual(self.state(self.opened, OLD), (True, True))
        self.assertEqual(UserFieldAccessOverride.objects.filter(field_id=OLD).count(), 2)
        self.assertEqual(PrebuiltRoleFieldAccess.objects.filter(field_id=OLD).count(), 1)

    def test_a_school_never_receives_another_schools_decision(self):
        carry_over_successions()
        self.assertEqual(
            set(RoleFieldAccess.objects.filter(field_id__in=NEW, role=self.elsewhere)
                .values_list("role__tenant_id", flat=True)),
            {self.other_tenant.pk},
        )
        self.assertEqual(
            RoleFieldAccess.objects.filter(field_id__in=NEW).exclude(
                role__tenant__in=[self.tenant, self.other_tenant]).count(),
            0,
        )


class SyncCarriesTheSwitchesTests(_Carried):
    """The carry-over as the deploy runs it, inside ``sync_field_registry``."""

    def test_sync_carries_deactivates_the_old_field_and_audits_it_once(self):
        out = self.sync()
        self.assertIn("carried onto split fields", out)
        for key in NEW:
            self.assertEqual(self.state(self.opened, key), (True, True))
            self.assertEqual(self.state(self.closed, key), (False, False))
        old = FieldDefinition.objects.get(key=OLD)
        self.assertFalse(old.is_active)
        audits = RBACAuditLog.objects.filter(
            action_type="FIELD_REGISTRY_SYNCED", summary__startswith="Field switches carried",
        )
        self.assertEqual(audits.count(), 1)
        self.assertEqual(audits.get().metadata["carried"]["role_field_access"], 12)

        again = self.sync()
        self.assertNotIn("carried onto split fields", again)
        self.assertEqual(audits.count(), 1)

    def test_it_works_whether_or_not_the_new_fields_were_synced_before(self):
        """Fields already present and fields created by this very run end the same."""
        FieldDefinition.objects.filter(key__in=NEW).delete()
        self.sync()
        for key in NEW:
            self.assertEqual(self.state(self.opened, key), (True, True))
            self.assertEqual(self.state(self.closed, key), (False, False))
            self.assertEqual(self.state(self.read_only, key), (True, False))
        self.assertFalse(RoleFieldAccess.objects.filter(field_id=OLD).exists())
