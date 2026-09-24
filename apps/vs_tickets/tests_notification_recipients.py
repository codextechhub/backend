"""Who a ticket's notifications reach, and why it is the list the picker offers.

A desk agent whose ``tickets.ticket.triage`` key arrives through a permission
group is an ordinary shape, not an exotic one: a group is how a desk's keys are
bundled and attached, and a role built that way carries no permission of its
own. Such an agent passes every permission gate, appears in the assignee picker
and can be handed a ticket, so a recipient list that reads only the keys written
directly on a role gives her work and tells her nothing about it.

These pin the agreement itself: whoever a ticket may be assigned to is somebody
its notifications can reach.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from vs_rbac.models import GroupPermission, PermissionGroup, PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_group,
)

from .constants import TicketPermission
from .services import notifications as notify_svc
from .services import tickets as ticket_svc
from .services import visibility
from .tests import TicketFixtureMixin, _user


def _grant_through_group(tenant, user, key, *, group_name, role_name):
    """Give ``user`` ``key`` the way a seeded desk role carries it.

    The role holds no permission of its own: everything it grants arrives
    through the attached group. That is the shape a recipient query reading
    ``role_permissions`` alone cannot see, and the reason these tests build it
    rather than granting the key directly.
    """
    group = PermissionGroup.objects.create(
        name=group_name, scope=PermissionScope.TENANT,
    )
    GroupPermission.objects.create(group=group, permission=make_permission(key))
    role = make_role(tenant, name=role_name)
    make_role_group(role, group)
    make_assignment(tenant, user, role)
    return role


def _sent_recipients(send):
    return {user.pk for user in send.call_args.kwargs["recipients"]}


class TicketRecipientsHoldingAKeyThroughAGroupTests(TicketFixtureMixin, TestCase):
    def setUp(self):
        self.build_users()

        # Ngozi works the CodeX desk. Her triage key is in the Support Desk
        # group, not on her role.
        self.group_agent = _user("desk-group@cx.test", "Ngozi", "Desk")
        _grant_through_group(
            self.group_agent.tenant,
            self.group_agent,
            TicketPermission.TRIAGE,
            group_name="Support Desk",
            role_name="CX Desk Agent",
        )

        # Ola is on the platform tenant and works no queue at all.
        self.no_key = _user("observer@cx.test", "Ola", "Observer")

        # Bisi triages Alpha School's own tickets, her key held the same way.
        self.school_group_triager = _user(
            "triage-group@alpha.test", "Bisi", "Triage",
            school=self.school_a, branch=self.branch_a,
        )
        _grant_through_group(
            self.school_a.tenant,
            self.school_group_triager,
            TicketPermission.TRIAGE,
            group_name="School Support Desk",
            role_name="Alpha Desk Agent",
        )

    # ── the platform desk ───────────────────────────────────────────────────

    def test_a_grouped_key_reaches_the_desk_when_a_ticket_is_raised(self):
        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.create_ticket(
                    actor=self.support,
                    title="Payout reconciliation job is stuck",
                    description="x",
                    category="BUG",
                    priority="HIGH",
                )

        self.assertTrue(send.called, "a new desk ticket notified nobody")
        self.assertIn(self.group_agent.pk, _sent_recipients(send))

    def test_a_grouped_key_reaches_the_desk_when_a_school_escalates(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Projector in Room 3 will not power on",
            description="x",
            category="SUPPORT",
            priority="MEDIUM",
        )

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                self.escalate(ticket)

        self.assertEqual(send.call_args.kwargs["event_key"], "ticket.escalated")
        self.assertIn(self.group_agent.pk, _sent_recipients(send))

    def test_a_key_written_on_the_role_still_reaches_the_desk(self):
        # The direct grant is the older shape and keeps working; sharing the
        # picker's query must not trade one holder for the other.
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Fee receipts are not printing",
            description="x",
            category="BUG",
            priority="HIGH",
        )

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                self.escalate(ticket)

        recipients = _sent_recipients(send)
        self.assertIn(self.support.pk, recipients)
        self.assertIn(self.other_support.pk, recipients)

    def test_a_platform_account_holding_no_ticket_key_is_told_nothing(self):
        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.create_ticket(
                    actor=self.support,
                    title="Nightly export failed",
                    description="x",
                    category="BUG",
                    priority="LOW",
                )

        self.assertNotIn(self.no_key.pk, _sent_recipients(send))
        self.assertNotIn(
            self.no_key.pk,
            {user.pk for user in notify_svc.support_recipients()},
        )

    def test_everybody_the_picker_offers_can_be_reached(self):
        """The agreement itself, rather than one holder inside it.

        A name the picker offers and the recipient list omits is a ticket that
        can be given to somebody who will never hear about it, whichever way
        their key is held.
        """
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Timetable import rejects every row",
            description="x",
            category="BUG",
            priority="URGENT",
        )
        self.escalate(ticket)

        assignable = {user.pk for user in visibility.eligible_support_users_qs(ticket)}
        reachable = {user.pk for user in notify_svc.support_recipients()}

        self.assertIn(self.group_agent.pk, assignable)
        self.assertTrue(
            assignable.issubset(reachable),
            f"assignable but unreachable: {assignable - reachable}",
        )

    # ── a school's own queue ────────────────────────────────────────────────

    def test_a_grouped_key_reaches_a_schools_own_triage_queue(self):
        # Before escalation the queue is the school's own, so this is the
        # same defect one tenant down: Bisi is given the school's tickets and
        # hears about none of them.
        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.create_ticket(
                    actor=self.requester,
                    title="Staff cannot sign in on the library machines",
                    description="x",
                    category="SUPPORT",
                    priority="HIGH",
                )

        self.assertTrue(send.called, "a new school ticket notified nobody")
        recipients = _sent_recipients(send)
        self.assertIn(self.school_group_triager.pk, recipients)
        # Still the school's own business, not CodeX's.
        self.assertNotIn(self.support.pk, recipients)
        self.assertNotIn(self.group_agent.pk, recipients)
