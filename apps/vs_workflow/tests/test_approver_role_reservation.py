"""Approval authority must not be obtainable by naming a role.

A ROLE stage nominates its approvers by matching a role *key* inside the tenant
that raised the document. A tenant role's key is slugified from the name whoever
created it typed. Those two facts met in the middle: a person holding role-create
could type "Payout Approver", get the key ``payout-approver`` - the key the
seeded payout ladder resolves - assign it, and be on the frozen approver list for
every payout batch the school raised, holding no payments permission at all.

Nothing on the roles screen could have granted or withdrawn that, because the ten
``*.approve`` permissions a reader would expect to govern it are listed in
``vs_rbac.unenforced``: they are seeded, grantable, and read by nothing.

Two doors are closed, and both are tested here because either alone leaves the
hole open:

* the **front** door - ``_unique_tenant_role_key`` refuses to mint a role on a
  reserved key, rather than quietly suffixing it to ``payout-approver-1``;
* the **back** door - ``_users_for_role_key`` resolves only roles flagged
  ``is_system_role``, which only provisioning sets, so a look-alike created
  before the refusal existed still confers nothing.

The reserved set is derived from the published stages rather than written down,
so a stage added tomorrow reserves its key the same day. That is tested too: a
list nobody remembers to update is how this class of bug returns.
"""
from __future__ import annotations

from django.test import TestCase
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import (
    codex_tenant,
    make_assignment,
    make_role,
    make_vision_user,
)
from vs_workflow.models import WorkflowStage, WorkflowTemplate
from vs_workflow.services.approvers import _users_for_role_key
from vs_workflow.services.roles import ensure_approver_role, reserved_role_keys

ROLE_KEY = "payout-approver"


class _StageFixture(TestCase):
    """One central stage naming one role key, resolved inside the tenant."""

    def setUp(self):
        self.tenant = codex_tenant()

        # Retire the seeded central stages so the reserved set under test is the
        # one this fixture declares, using the field the engine already filters
        # on rather than deleting seeded rows.
        WorkflowStage.objects.filter(
            template__tenant__isnull=True, retired_at__isnull=True,
        ).update(retired_at=timezone.now())

        template = WorkflowTemplate.objects.create(
            tenant=None, branch=None, document_type="reservation.doc",
            code="standard", name="Reservation",
        )
        self.stage = WorkflowStage.objects.create(
            template=template, code="approve", label="Approval", kind="APPROVAL",
            order=10, approver_source="ROLE", approver_role_key=ROLE_KEY,
            approver_scope="SCHOOL", advance_rule="ANY", on_rejection="TERMINAL",
            skip_if_no_approvers=False,
        )


class ReservedKeyDerivationTests(_StageFixture):
    """The set is read off the templates, never written down."""

    def test_a_key_a_stage_names_is_reserved(self):
        self.assertIn(ROLE_KEY, reserved_role_keys())

    def test_a_key_nothing_names_is_not_reserved(self):
        self.assertNotIn("pastoral-care-lead", reserved_role_keys())

    def test_a_newly_published_stage_reserves_its_key_immediately(self):
        """The property that makes a hardcoded list unnecessary.

        Without this, reserving a key would mean remembering to edit a constant
        every time somebody publishes a ladder - and the one nobody remembers is
        the one that gets exploited.
        """
        self.assertNotIn("board-signatory", reserved_role_keys())

        WorkflowStage.objects.create(
            template=self.stage.template, code="board", label="Board", kind="APPROVAL",
            order=20, approver_source="ROLE", approver_role_key="board-signatory",
            approver_scope="SCHOOL", advance_rule="ANY", on_rejection="TERMINAL",
            skip_if_no_approvers=False,
        )

        self.assertIn("board-signatory", reserved_role_keys())


class FrontDoorTests(_StageFixture):
    """Creating a role on a reserved key is refused, not renamed."""

    def _mint(self, name):
        from vs_rbac.serializers.tenant import _unique_tenant_role_key

        return _unique_tenant_role_key(self.tenant, name)

    def test_a_look_alike_name_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            self._mint("Payout Approver")

        self.assertIn("reserved", str(caught.exception))

    def test_the_refusal_survives_creative_spelling(self):
        """The key is what matters, and several names slugify onto one key.

        A check on the *name* would pass every one of these.
        """
        for name in ("payout approver", "PAYOUT APPROVER", "Payout   Approver",
                     "Payout-Approver"):
            with self.subTest(name=name):
                with self.assertRaises(ValidationError):
                    self._mint(name)

    def test_it_is_refused_rather_than_suffixed(self):
        """Silently returning ``payout-approver-1`` would be the worst outcome.

        It tells somebody probing for approval authority to try another spelling,
        and tells an honest administrator nothing about why her new role does not
        behave like the one she was copying.
        """
        with self.assertRaises(ValidationError):
            self._mint("Payout Approver")

        self.assertFalse(
            TenantRoleTemplate.objects.filter(
                tenant=self.tenant, key__startswith="payout-approver-",
            ).exists(),
        )

    def test_an_ordinary_role_name_is_unaffected(self):
        self.assertEqual(self._mint("Pastoral Care Lead"), "pastoral-care-lead")

    def test_ordinary_uniqueness_suffixing_still_works(self):
        """The reservation must not have replaced the behaviour it sits in front of."""
        make_role(self.tenant, name="Library Monitor", key="library-monitor")

        self.assertEqual(self._mint("Library Monitor"), "library-monitor-1")


class BackDoorTests(_StageFixture):
    """A role on a reserved key confers approval only when provisioning made it."""

    def _held_role(self, *, is_system_role):
        role = make_role(
            self.tenant, name="Look-alike", key=ROLE_KEY,
            is_system_role=is_system_role,
        )
        self.holder = make_vision_user(email="holder@codex.test")
        make_assignment(self.tenant, self.holder, role)
        return role

    def test_a_hand_made_role_nominates_nobody(self):
        """The escalation, closed.

        The role exists, is ACTIVE, is held by an active user in the right
        tenant, and carries the exact key the stage resolves. It still resolves
        to nobody, because provisioning did not create it.
        """
        self._held_role(is_system_role=False)

        self.assertEqual(_users_for_role_key(ROLE_KEY, self.tenant, None), [])

    def test_a_provisioned_role_nominates_its_holders(self):
        """The other half: the legitimate path must keep working.

        A fix that closed the hole by resolving nobody at all would park every
        approval on the platform, which is a worse outage than the bug.
        """
        self._held_role(is_system_role=True)

        self.assertEqual(
            [u.pk for u in _users_for_role_key(ROLE_KEY, self.tenant, None)],
            [self.holder.pk],
        )

    def test_ensure_approver_role_flags_what_it_creates(self):
        role, created = ensure_approver_role(self.tenant, ROLE_KEY)

        self.assertTrue(created)
        self.assertTrue(role.is_system_role)

    def test_ensure_approver_role_repairs_an_unflagged_row(self):
        """Provisioning naming the key IS the assertion the flag records.

        This is the supported way out for a tenant whose approver role was
        created before the flag meant anything - and the reason the backfill
        migration is a convenience rather than the only route.
        """
        existing = self._held_role(is_system_role=False)

        role, created = ensure_approver_role(self.tenant, ROLE_KEY)

        self.assertFalse(created)
        self.assertEqual(role.pk, existing.pk)
        existing.refresh_from_db()
        self.assertTrue(existing.is_system_role)
        self.assertEqual(
            [u.pk for u in _users_for_role_key(ROLE_KEY, self.tenant, None)],
            [self.holder.pk],
        )

    def test_provisioning_does_not_reactivate_a_switched_off_role(self):
        """Flagging is not the same as overruling an administrator.

        The flag says "the platform names this key"; ``status`` says whether the
        tenant wants it live. Provisioning may assert the first and must not
        touch the second.
        """
        role = make_role(
            self.tenant, name="Look-alike", key=ROLE_KEY,
            is_system_role=False, status="INACTIVE",
        )

        ensure_approver_role(self.tenant, ROLE_KEY)

        role.refresh_from_db()
        self.assertTrue(role.is_system_role)
        self.assertEqual(role.status, "INACTIVE")
        # An inactive role still nominates nobody - the resolver filters on
        # ACTIVE, and the flag does not override that.
        self.assertEqual(_users_for_role_key(ROLE_KEY, self.tenant, None), [])
