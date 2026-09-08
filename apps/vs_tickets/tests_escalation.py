"""Escalation: a school triages its own tickets, and sends up what it cannot fix.

The rule these tests hold is a change of meaning, not just a new column. Before,
every ticket a school filed went straight to CodeX and the platform desk saw all
of them. Now a ticket is the school's until somebody there says otherwise, and
the desk sees only what was sent up.

Two failures matter more than the rest and each has a test of its own:

* a support user opening an unescalated school ticket by id, which would read a
  school's internal business through a door their own list had closed;
* an escalation that loses the thread, which is what happens if escalating
  copies the ticket instead of stamping it.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import make_permission, make_role, make_role_permission, make_assignment

from .constants import CommentVisibility, TicketAuditAction, TicketPermission
from .models import Ticket, TicketAuditLog
from .services import tickets as ticket_svc
from .services import visibility
from .tests import TicketFixtureMixin, _grant


class EscalationTests(TicketFixtureMixin, TestCase):
    def setUp(self):
        self.build_users()
        self.client = APIClient()

        # The school's own triage person: tickets.ticket.manage inside their
        # tenant, which is what the visibility rule already keys on.
        self.school_admin = self.peer
        _grant(
            self.school_a,
            self.school_admin,
            (TicketPermission.MANAGE, TicketPermission.COMMENT),
            role_name="Alpha Ticket Manager",
        )

        self.ticket = Ticket.objects.create(
            title="Projector in Room 3 will not power on",
            description="Tried two sockets.",
            requester=self.requester,
            tenant=self.school_a.tenant,
        )

    # ── who the ticket belongs to ───────────────────────────────────────────

    def test_a_new_ticket_is_the_schools_own(self):
        self.assertIsNone(self.ticket.escalated_at)
        self.assertIsNone(self.ticket.escalated_by)

    def test_the_platform_desk_does_not_list_a_schools_own_ticket(self):
        # The projector in Room 3 is not CodeX's to read.
        visible = visibility.visible_tickets_qs(self.support)
        self.assertNotIn(self.ticket.pk, [t.pk for t in visible])

    def test_the_platform_desk_cannot_open_it_by_id_either(self):
        # The list and the object check have to agree, or the narrowing is a
        # display preference rather than a rule.
        self.assertFalse(visibility.can_view_ticket(self.support, self.ticket))

    def test_the_school_still_sees_its_own_ticket(self):
        for user in (self.requester, self.school_admin):
            visible = visibility.visible_tickets_qs(user)
            self.assertIn(self.ticket.pk, [t.pk for t in visible], user.email)

    # ── sending it up ───────────────────────────────────────────────────────

    def test_escalating_stamps_the_same_ticket(self):
        ticket = ticket_svc.escalate_ticket(self.ticket, actor=self.school_admin)

        self.assertEqual(ticket.pk, self.ticket.pk)
        self.assertIsNotNone(ticket.escalated_at)
        self.assertEqual(ticket.escalated_by, self.school_admin)

    def test_the_reference_and_the_thread_survive_it(self):
        # The teacher who raised it was given this reference and is watching
        # this thread. Escalating must not hand them a dead number.
        number = self.ticket.ticket_number
        ticket_svc.add_comment(
            self.ticket, actor=self.requester,
            body="It was working on Friday.", visibility=CommentVisibility.PUBLIC,
        )

        ticket_svc.escalate_ticket(self.ticket, actor=self.school_admin)
        self.ticket.refresh_from_db()

        self.assertEqual(self.ticket.ticket_number, number)
        self.assertEqual(Ticket.all_objects.filter(ticket_number=number).count(), 1)
        self.assertEqual(self.ticket.comments.count(), 1)

    def test_the_desk_sees_it_once_it_is_sent_up(self):
        ticket_svc.escalate_ticket(self.ticket, actor=self.school_admin)

        visible = visibility.visible_tickets_qs(self.support)
        self.assertIn(self.ticket.pk, [t.pk for t in visible])
        self.assertTrue(visibility.can_view_ticket(self.support, self.ticket))

    def test_a_note_reaches_the_person_who_raised_it(self):
        # Public, because a school escalating behind the reporter's back is how
        # a ticket goes quiet from their side for a week.
        ticket_svc.escalate_ticket(
            self.ticket, actor=self.school_admin, note="We replaced the cable, no change.",
        )

        comment = self.ticket.comments.get()
        self.assertEqual(comment.visibility, CommentVisibility.PUBLIC)
        self.assertIn("replaced the cable", comment.body)

    def test_it_is_written_to_the_audit_trail(self):
        ticket_svc.escalate_ticket(self.ticket, actor=self.school_admin)

        entry = TicketAuditLog.objects.filter(
            ticket=self.ticket, action=TicketAuditAction.ESCALATED,
        ).get()
        self.assertIn("escalated", entry.summary.lower())

    # ── what escalation refuses ─────────────────────────────────────────────

    def test_a_school_user_without_triage_cannot_escalate(self):
        # Raising a ticket is open to everyone; deciding the school is beaten
        # by it is not.
        with self.assertRaises(PermissionDenied):
            ticket_svc.escalate_ticket(self.ticket, actor=self.norole)

    def test_escalating_twice_is_refused_rather_than_repeated(self):
        ticket_svc.escalate_ticket(self.ticket, actor=self.school_admin)

        with self.assertRaises(ValidationError):
            ticket_svc.escalate_ticket(self.ticket, actor=self.school_admin)

        self.assertEqual(
            TicketAuditLog.objects.filter(
                ticket=self.ticket, action=TicketAuditAction.ESCALATED,
            ).count(),
            1,
        )

    def test_a_codex_ticket_cannot_be_escalated_to_codex(self):
        own = Ticket.objects.create(
            title="Internal: rotate the staging key",
            description="Housekeeping.",
            requester=self.support,
            tenant=self.support.tenant,
        )
        with self.assertRaises(ValidationError):
            ticket_svc.escalate_ticket(own, actor=self.support)

    def test_codex_keeps_seeing_its_own_tickets_without_escalation(self):
        own = Ticket.objects.create(
            title="Internal: rotate the staging key",
            description="Housekeeping.",
            requester=self.support,
            tenant=self.support.tenant,
        )
        visible = visibility.visible_tickets_qs(self.support)
        self.assertIn(own.pk, [t.pk for t in visible])

    def test_a_support_user_keeps_a_ticket_they_are_assigned_to(self):
        # If an escalation were ever withdrawn, the person working it must not
        # lose the thread mid-conversation.
        self.ticket.assignee = self.support
        self.ticket.save(update_fields=["assignee"])

        visible = visibility.visible_tickets_qs(self.support)
        self.assertIn(self.ticket.pk, [t.pk for t in visible])

    # ── who hears about it ──────────────────────────────────────────────────

    def test_the_schools_own_queue_is_told_about_a_new_ticket(self):
        # The failure this stops is silent. `_eligible_recipients` drops anybody
        # who cannot view the ticket, so paging CodeX about an unescalated
        # school ticket does not send a wrong email - it sends none at all, and
        # the ticket sits unread with nobody aware it exists.
        from unittest import mock

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.create_ticket(
                    actor=self.requester,
                    title="Nobody has looked at this",
                    description="x",
                    category="HELP",
                    priority="LOW",
                )

        recipients = send.call_args.kwargs["recipients"]
        self.assertIn(self.school_admin.pk, {u.pk for u in recipients})
        self.assertNotIn(self.support.pk, {u.pk for u in recipients})

    def test_the_desk_is_told_at_the_moment_of_escalation(self):
        # Not only on the next comment. The school escalated because they were
        # waiting, so a desk that only hears when somebody writes again is a
        # desk that hears when the waiting has already failed.
        from unittest import mock

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.escalate_ticket(self.ticket, actor=self.school_admin)

        self.assertTrue(send.called, "escalation notified nobody")
        self.assertEqual(send.call_args.kwargs["event_key"], "ticket.escalated")
        recipients = send.call_args.kwargs["recipients"]
        self.assertIn(self.support.pk, {u.pk for u in recipients})

    def test_the_escalation_message_names_the_school(self):
        """The desk's other ticket messages are about tickets it already had.
        This is the one that arrives from somewhere, so it says where from."""
        from unittest import mock

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.escalate_ticket(self.ticket, actor=self.school_admin)

        context = send.call_args.kwargs["context"]
        self.assertEqual(context["school_name"], self.ticket.tenant.name)
        self.assertEqual(context["ticket_number"], self.ticket.ticket_number)

    def test_codex_is_told_once_it_has_been_escalated(self):
        from unittest import mock

        ticket_svc.escalate_ticket(self.ticket, actor=self.school_admin)

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.add_comment(
                    self.ticket, actor=self.requester,
                    body="Any news?", visibility=CommentVisibility.PUBLIC,
                )

        recipients = send.call_args.kwargs["recipients"]
        self.assertIn(self.support.pk, {u.pk for u in recipients})

    # ── through the API ─────────────────────────────────────────────────────

    def test_the_endpoint_escalates_and_returns_the_state(self):
        self.client.force_authenticate(user=self.school_admin)

        response = self.client.post(
            f"/v1/support/tickets/{self.ticket.pk}/escalate/",
            {"note": "Beyond us, the unit is under warranty."},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertIsNotNone(data["escalated_at"])
        self.assertEqual(data["escalated_by"]["email"], self.school_admin.email)

    def test_the_endpoint_refuses_somebody_who_may_not_triage(self):
        self.client.force_authenticate(user=self.norole)

        response = self.client.post(
            f"/v1/support/tickets/{self.ticket.pk}/escalate/", {}, format="json",
        )

        self.assertIn(response.status_code, (403, 404), response.content)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.escalated_at)
