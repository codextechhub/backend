"""A school that has not gone live works its own support desk.

Alpha School is still onboarding (its tenant is PENDING). Rita files a ticket,
Paul triages and escalates it, CodeX support replies, and Rita reads the reply
and answers it. Every one of those steps is the same call a live school makes,
through the same visibility rules, and these tests hold both halves:

* the desk is open: list, thread, replies, files, triage and escalation;
* it is open no wider than after go-live: Beta School's ticket stays invisible,
  assignment stays the platform desk's own, and the platform-wide guide
  analytics summary stays closed.
"""
from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from vs_tenants.models import Tenant
from vs_user.models import User

from .constants import TicketPermission, TicketStatus
from .models import Ticket
from .tests import TicketFixtureMixin, _grant
from .tests_pending_attachments import PNG

TICKETS = "/v1/support/tickets/"


class PendingSchoolSupportDeskTests(TicketFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        Tenant.objects.filter(pk=cls.school_a.tenant_id).update(
            status=Tenant.Status.PENDING,
        )
        _grant(
            cls.school_a,
            cls.peer,
            (TicketPermission.TRIAGE, TicketPermission.TRANSITION,
             TicketPermission.ESCALATE, TicketPermission.COMMENT),
            role_name="Alpha Triage",
        )
        # Re-read so the cached tenant carries the PENDING status.
        cls.requester = User.objects.get(pk=cls.requester.pk)
        cls.peer = User.objects.get(pk=cls.peer.pk)

        cls.ticket = Ticket.objects.create(
            title="The branch step will not complete",
            description="Saving the second branch spins forever.",
            requester=cls.requester,
            tenant=cls.school_a.tenant,
        )
        cls.beta_ticket = Ticket.objects.create(
            title="Beta's own problem",
            description="Nothing Alpha should read.",
            requester=cls.outsider,
            tenant=cls.school_b.tenant,
        )

    def setUp(self):
        self.assertEqual(self.requester.tenant.status, Tenant.Status.PENDING)

    def _as(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def assertTenantNotLive(self, response):
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")

    # ── the desk is open ────────────────────────────────────────────────────

    def test_a_pending_school_lists_its_own_tickets_only(self):
        response = self._as(self.requester).get(TICKETS)
        self.assertEqual(response.status_code, 200, response.data)
        ids = {row["id"] for row in response.data["data"]}
        self.assertIn(self.ticket.pk, ids)
        self.assertNotIn(self.beta_ticket.pk, ids)

    def test_a_pending_school_opens_its_ticket(self):
        response = self._as(self.requester).get(f"{TICKETS}{self.ticket.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["id"], self.ticket.pk)

    def test_a_pending_school_reads_supports_reply_and_answers_it(self):
        escalated = self._as(self.peer).post(
            f"{TICKETS}{self.ticket.pk}/escalate/",
            {"note": "Beyond us."},
            format="json",
        )
        self.assertEqual(escalated.status_code, 200, escalated.data)

        replied = self._as(self.support).post(
            f"{TICKETS}{self.ticket.pk}/comments/",
            {"body": "Please send a screenshot of the branch form."},
            format="json",
        )
        self.assertEqual(replied.status_code, 201, replied.data)

        client = self._as(self.requester)
        thread = client.get(f"{TICKETS}{self.ticket.pk}/comments/")
        self.assertEqual(thread.status_code, 200, thread.data)
        bodies = [row["body"] for row in thread.data["data"]]
        self.assertIn("Please send a screenshot of the branch form.", bodies)

        answered = client.post(
            f"{TICKETS}{self.ticket.pk}/comments/",
            {"body": "Screenshot attached."},
            format="json",
        )
        self.assertEqual(answered.status_code, 201, answered.data)

    def test_a_pending_school_attaches_and_downloads_a_file(self):
        client = self._as(self.requester)
        uploaded = client.post(
            f"{TICKETS}{self.ticket.pk}/attachments/",
            {"file": SimpleUploadedFile("screen.png", PNG, content_type="image/png")},
            format="multipart",
        )
        self.assertEqual(uploaded.status_code, 201, uploaded.data)

        attachment_id = uploaded.data["data"]["id"]
        downloaded = client.get(
            f"{TICKETS}{self.ticket.pk}/attachments/{attachment_id}/download/",
        )
        self.assertEqual(downloaded.status_code, 200)

    def test_a_pending_schools_desk_staff_triage(self):
        response = self._as(self.peer).post(
            f"{TICKETS}{self.ticket.pk}/transition/",
            {"status": TicketStatus.IN_PROGRESS},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, TicketStatus.IN_PROGRESS)

    def test_a_pending_school_reads_its_dashboard_counts(self):
        response = self._as(self.requester).get("/v1/support/dashboard/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["requested_by_me"], 1)

    # ── no wider than after go-live ─────────────────────────────────────────

    def test_another_schools_ticket_stays_invisible(self):
        client = self._as(self.requester)
        opened = client.get(f"{TICKETS}{self.beta_ticket.pk}/")
        self.assertEqual(opened.status_code, 404, opened.data)
        thread = client.get(f"{TICKETS}{self.beta_ticket.pk}/comments/")
        self.assertEqual(thread.status_code, 404, thread.data)

    def test_assignment_stays_the_platform_desks_own(self):
        client = self._as(self.peer)
        self.assertTenantNotLive(
            client.post(f"{TICKETS}{self.ticket.pk}/assign/", {}, format="json"),
        )
        self.assertTenantNotLive(
            client.get(f"{TICKETS}{self.ticket.pk}/eligible-assignees/"),
        )

    def test_the_guide_analytics_summary_stays_closed(self):
        response = self._as(self.peer).get("/v1/support/guides/analytics/summary/")
        self.assertTenantNotLive(response)
