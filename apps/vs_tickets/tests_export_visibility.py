"""The ticket export shows what the ticket list shows, and no more.

The rule this pins is compound rather than a branch filter, which is why the
dataset calls ``visible_tickets_qs`` instead of the Export Centre's branch
helper. Applying the helper here would have been a plausible-looking bug: it
would strip a participant's own ticket whenever it was filed for another
branch, and the person who lost it would be the one assigned to work it.
"""
from __future__ import annotations

from django.test import TestCase

from vs_exports.catalogue import ScopeContext, get_dataset
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)
from vs_tickets.constants import TicketPermission
from vs_tickets.models import Ticket


class TicketExportVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)

        manage = make_permission(
            TicketPermission.MANAGE, scope=PermissionScope.TENANT,
        )
        role = make_role(cls.school, name="Ticket Manager", key="branch_admin")
        make_role_permission(role, manage)

        # A manager pinned to Ikeja: manages Ikeja's tickets and the school's.
        cls.ikeja_manager = make_school_admin(
            None, email="manager@ikeja.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.ikeja_manager, role, branch=cls.ikeja)

        # Somebody with no ticket grant at all, who is nonetheless a participant.
        cls.staffer = make_school_admin(
            None, email="staffer@brightfield.test", tenant=cls.tenant,
        )

    _seq = 0

    def ticket(self, title, branch, requester=None, assignee=None):
        type(self)._seq += 1
        return Ticket.all_objects.create(
            tenant=self.tenant, branch=branch, title=title,
            ticket_number=f"TKT-{self._seq:04d}",
            description="body", requester=requester or self.staffer,
            assignee=assignee,
        )

    def rows(self, user):
        return {
            t.title for t in get_dataset("support.tickets").base(
                ScopeContext(tenant=self.tenant, user=user),
            )
        }

    def test_a_branch_manager_gets_their_branch_and_the_school_wide_ones(self):
        self.ticket("Ikeja printer", self.ikeja)
        self.ticket("School-wide outage", None)
        self.ticket("Lekki projector", self.lekki)

        self.assertEqual(
            self.rows(self.ikeja_manager), {"Ikeja printer", "School-wide outage"},
        )

    def test_a_branch_manager_does_not_get_another_branchs_tickets(self):
        self.ticket("Lekki projector", self.lekki)
        self.assertNotIn("Lekki projector", self.rows(self.ikeja_manager))

    def test_a_participant_keeps_a_ticket_filed_for_another_branch(self):
        """The case a plain branch filter would have broken.

        The Ikeja manager is assigned a ticket filed for Lekki. Narrowing on
        the branch column alone would take it out of her export, and she is the
        person working it.
        """
        self.ticket("Lekki projector", self.lekki, assignee=self.ikeja_manager)
        self.assertIn("Lekki projector", self.rows(self.ikeja_manager))

    def test_a_requester_keeps_their_own_ticket_whatever_its_branch(self):
        self.ticket("My laptop", self.lekki, requester=self.staffer)
        self.assertIn("My laptop", self.rows(self.staffer))

    def test_somebody_with_no_grant_gets_only_their_own_threads(self):
        """A view grant is deliberately not school-wide ticket access."""
        self.ticket("My laptop", self.lekki, requester=self.staffer)
        self.ticket("Somebody else's", self.ikeja, requester=self.ikeja_manager)
        self.assertEqual(self.rows(self.staffer), {"My laptop"})

    def test_the_export_matches_the_list_exactly(self):
        """The claim the whole change rests on, asserted rather than assumed."""
        from vs_tickets.services import visibility

        self.ticket("Ikeja printer", self.ikeja)
        self.ticket("School-wide outage", None)
        self.ticket("Lekki projector", self.lekki, assignee=self.ikeja_manager)

        on_screen = {
            t.title for t in
            visibility.visible_tickets_qs(self.ikeja_manager)
            .filter(tenant=self.tenant)
        }
        self.assertEqual(self.rows(self.ikeja_manager), on_screen)

    def test_no_caller_exports_nothing_rather_than_everything(self):
        """Fails closed, unlike the catalogue helper.

        Ticket conversations carry personal detail, so an export with nobody in
        context returns nothing rather than the whole tenant. Every real run
        carries a user, so this is the unreachable branch being pinned shut.
        """
        self.ticket("Ikeja printer", self.ikeja)
        self.assertEqual(self.rows(None), set())
