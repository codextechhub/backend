from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from vs_rbac.models import (
    Permission,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
)
from vs_schools.models import Branch, School, SchoolStatus
from vs_user.models import User

from .constants import CommentVisibility, TicketPermission, TicketStatus
from .models import TicketAuditLog
from .services import tickets as ticket_svc
from .services import visibility

REQUESTER_KEYS = (
    TicketPermission.VIEW,
    TicketPermission.COMMENT,
    TicketPermission.ATTACH,
)


def _school(slug, name):
    return School.objects.create(slug=slug, name=name, code=slug.upper(), status=SchoolStatus.ACTIVE)


def _branch(school, name):
    return Branch.objects.create(school=school, name=name, _type="Primary", is_main=True)


def _user(email, first, last, *, user_type, school=None, branch=None):
    # ``school`` is accepted for call-site readability but the tenant is derived
    # from the branch (or defaults to codex for CX staff) — User.school is gone.
    return User.objects.create_user(
        email=email,
        first_name=first,
        last_name=last,
        user_type=user_type,
        branch=branch,
        status=User.Status.ACTIVE,
    )


def _grant(school, user, keys, role_name="Ticket User"):
    role = make_role(school, name=role_name)
    for key in keys:
        make_role_permission(role, make_permission(key))
    make_assignment(school, user, role)
    return role


class TicketFixtureMixin:
    def build_users(self):
        self.school_a = _school("alpha", "Alpha School")
        self.branch_a = _branch(self.school_a, "Main")
        self.school_b = _school("beta", "Beta School")
        self.branch_b = _branch(self.school_b, "Main")
        self.requester = _user(
            "requester@alpha.test", "Rita", "Requester",
            user_type=User.UserType.STAFF, school=self.school_a, branch=self.branch_a,
        )
        self.peer = _user(
            "peer@alpha.test", "Paul", "Peer",
            user_type=User.UserType.STAFF, school=self.school_a, branch=self.branch_a,
        )
        self.norole = _user(
            "norole@alpha.test", "Nora", "Norole",
            user_type=User.UserType.STAFF, school=self.school_a, branch=self.branch_a,
        )
        self.outsider = _user(
            "outsider@beta.test", "Bola", "Outsider",
            user_type=User.UserType.STAFF, school=self.school_b, branch=self.branch_b,
        )
        self.support = _user(
            "support@cx.test", "Ada", "Support",
            user_type=User.UserType.CX_STAFF,
        )
        self.other_support = _user(
            "tier2@cx.test", "Tolu", "Tier",
            user_type=User.UserType.CX_STAFF,
        )
        _grant(self.school_a, self.requester, REQUESTER_KEYS, role_name="Alpha Requester")
        _grant(self.school_a, self.peer, REQUESTER_KEYS, role_name="Alpha Peer")
        _grant(self.school_b, self.outsider, REQUESTER_KEYS, role_name="Beta Requester")
        # Support authority is an RBAC grant on the platform tenant now, not a
        # user_type side effect: is_support_user checks tickets.ticket.manage.
        _grant(
            self.support.tenant, self.support,
            (TicketPermission.MANAGE,), role_name="CX Support",
        )
        _grant(
            self.other_support.tenant, self.other_support,
            (TicketPermission.MANAGE,), role_name="CX Support Tier 2",
        )


class TicketServiceTests(TicketFixtureMixin, TestCase):
    def setUp(self):
        self.build_users()

    def test_create_ticket_scopes_to_requester_school_and_audits(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Login fails",
            description="I cannot log in.",
            category="BUG",
            priority="HIGH",
        )

        self.assertTrue(ticket.ticket_number.startswith("TCK-"))
        self.assertEqual(ticket.requester_id, self.requester.pk)
        self.assertEqual(ticket.school_id, self.school_a.pk)
        self.assertEqual(ticket.branch_id, self.branch_a.pk)
        self.assertEqual(ticket.status, TicketStatus.OPEN)
        self.assertTrue(TicketAuditLog.objects.filter(ticket=ticket, action="CREATED").exists())

    def test_ticket_numbers_are_sequential_and_unique(self):
        first = ticket_svc.create_ticket(
            actor=self.requester, title="One", description="x", category="HELP", priority="LOW",
        )
        second = ticket_svc.create_ticket(
            actor=self.requester, title="Two", description="x", category="HELP", priority="LOW",
        )
        self.assertNotEqual(first.ticket_number, second.ticket_number)
        self.assertEqual(
            int(second.ticket_number.rsplit("-", 1)[1]),
            int(first.ticket_number.rsplit("-", 1)[1]) + 1,
        )

    def test_anyone_authenticated_can_file_a_ticket_and_follow_replies(self):
        # No role grants at all: filing and following your own thread still works.
        ticket = ticket_svc.create_ticket(
            actor=self.norole, title="Locked out", description="x", category="HELP", priority="LOW",
        )
        self.assertEqual(ticket.requester_id, self.norole.pk)

        ticket_svc.assign_ticket(ticket, actor=self.support, assignee=self.support)
        ticket_svc.add_comment(
            ticket, actor=self.support, body="We are on it.", visibility=CommentVisibility.PUBLIC,
        )

        client = APIClient()
        client.force_authenticate(self.norole)
        payload = client.get(f"/v1/support/tickets/{ticket.pk}/comments/").json()["data"]
        self.assertEqual([row["body"] for row in payload], ["We are on it."])

        reply = client.post(
            f"/v1/support/tickets/{ticket.pk}/comments/",
            {"body": "Thanks!", "visibility": CommentVisibility.PUBLIC},
        )
        self.assertEqual(reply.status_code, 201)

    def test_visibility_is_requester_school_and_support_scoped(self):
        mine = ticket_svc.create_ticket(
            actor=self.requester, title="Mine", description="x", category="HELP", priority="LOW",
        )
        other = ticket_svc.create_ticket(
            actor=self.outsider, title="Other", description="x", category="HELP", priority="LOW",
        )

        self.assertIn(mine, visibility.visible_tickets_qs(self.peer))
        self.assertNotIn(other, visibility.visible_tickets_qs(self.peer))
        self.assertIn(mine, visibility.visible_tickets_qs(self.support))
        self.assertIn(other, visibility.visible_tickets_qs(self.support))

    def test_school_manage_grant_does_not_leak_cross_tenant(self):
        # A SCHOOL user holding tickets.ticket.manage manages tickets inside
        # their own tenant only — the cross-tenant support span is reserved
        # for PLATFORM-tenant staff.
        _grant(
            self.school_a, self.peer,
            (TicketPermission.MANAGE,), role_name="Alpha Ticket Manager",
        )
        other = ticket_svc.create_ticket(
            actor=self.outsider, title="Other", description="x", category="HELP", priority="LOW",
        )
        self.assertFalse(visibility.is_support_user(self.peer))
        self.assertNotIn(other, visibility.visible_tickets_qs(self.peer))
        self.assertFalse(visibility.can_view_ticket(self.peer, other))

    def test_school_wide_visibility_requires_view_grant(self):
        mine = ticket_svc.create_ticket(
            actor=self.requester, title="Mine", description="x", category="HELP", priority="LOW",
        )
        # Same school, but no role grants: only their own tickets are visible.
        self.assertNotIn(mine, visibility.visible_tickets_qs(self.norole))
        self.assertFalse(visibility.can_view_ticket(self.norole, mine))

    def test_assign_and_transition_ticket(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester, title="Broken page", description="x", category="BUG", priority="HIGH",
        )
        ticket_svc.assign_ticket(ticket, actor=self.support, assignee=self.other_support)
        ticket.refresh_from_db()
        self.assertEqual(ticket.assignee_id, self.other_support.pk)
        self.assertEqual(ticket.status, TicketStatus.ASSIGNED)

        ticket_svc.transition_ticket(ticket, actor=self.other_support, status=TicketStatus.IN_PROGRESS)
        ticket_svc.transition_ticket(ticket, actor=self.other_support, status=TicketStatus.RESOLVED)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, TicketStatus.RESOLVED)
        self.assertIsNotNone(ticket.resolved_at)
        self.assertEqual(TicketAuditLog.objects.filter(ticket=ticket, action="STATUS_CHANGED").count(), 2)

    def test_internal_notes_hidden_from_requester_but_visible_to_support(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester, title="Need help", description="x", category="HELP", priority="MEDIUM",
        )
        ticket_svc.assign_ticket(ticket, actor=self.support, assignee=self.support)
        ticket_svc.add_comment(ticket, actor=self.requester, body="public", visibility=CommentVisibility.PUBLIC)
        ticket_svc.add_comment(ticket, actor=self.support, body="internal", visibility=CommentVisibility.INTERNAL)

        client = APIClient()
        client.force_authenticate(self.requester)
        requester_payload = client.get(f"/v1/support/tickets/{ticket.pk}/comments/").json()["data"]
        self.assertEqual([row["body"] for row in requester_payload], ["public"])

        client.force_authenticate(self.support)
        support_payload = client.get(f"/v1/support/tickets/{ticket.pk}/comments/").json()["data"]
        self.assertEqual([row["body"] for row in support_payload], ["public", "internal"])


class TicketApiSecurityTests(TicketFixtureMixin, TestCase):
    def setUp(self):
        self.build_users()
        self.ticket = ticket_svc.create_ticket(
            actor=self.requester, title="Broken export", description="x", category="BUG", priority="HIGH",
        )
        self.client_api = APIClient()

    def test_cross_tenant_retrieve_is_hidden_as_404(self):
        self.client_api.force_authenticate(self.outsider)
        response = self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/")
        self.assertEqual(response.status_code, 404)

    def test_requester_cannot_transition_own_ticket(self):
        self.client_api.force_authenticate(self.requester)
        response = self.client_api.post(
            f"/v1/support/tickets/{self.ticket.pk}/transition/",
            {"status": TicketStatus.CLOSED},
        )
        self.assertEqual(response.status_code, 403)

    def test_consultant_role_can_create_a_ticket(self):
        consultant = _user(
            "consultant@cx.test", "Cora", "Consultant",
            user_type=User.UserType.CX_STAFF,
        )
        call_command("seed_consultant_role", verbosity=0)
        role = TenantRoleTemplate.objects.get(
            tenant=consultant.tenant,
            key="xvs_consultant",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=consultant.tenant,
            user=consultant,
            role=role,
        )
        self.assertFalse(
            TenantRolePermission.objects.filter(
                role=role,
            ).exclude(permission__action_id="view").exists()
        )

        self.client_api.force_authenticate(consultant)
        response = self.client_api.post("/v1/support/tickets/", {
            "title": "Consultant needs assistance",
            "description": "Please review this request.",
            "category": "HELP",
            "priority": "LOW",
        }, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["data"]["requester"]["id"], consultant.pk)

    def test_requester_cannot_assign_ticket(self):
        self.client_api.force_authenticate(self.requester)
        response = self.client_api.post(
            f"/v1/support/tickets/{self.ticket.pk}/assign/",
            {"assignee_id": self.support.pk},
        )
        self.assertEqual(response.status_code, 403)

    def test_school_manager_with_grant_can_transition_via_api(self):
        _grant(self.school_a, self.peer, (TicketPermission.MANAGE,), role_name="Alpha Manager")
        self.client_api.force_authenticate(self.peer)
        response = self.client_api.post(
            f"/v1/support/tickets/{self.ticket.pk}/transition/",
            {"status": TicketStatus.IN_PROGRESS},
        )
        self.assertEqual(response.status_code, 200)

    def test_requester_cannot_view_audit_trail(self):
        self.client_api.force_authenticate(self.requester)
        response = self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/audit/")
        self.assertEqual(response.status_code, 403)

    def test_internal_note_attachment_hidden_from_requester(self):
        ticket_svc.assign_ticket(self.ticket, actor=self.support, assignee=self.support)
        note = ticket_svc.add_comment(
            self.ticket, actor=self.support, body="internal", visibility=CommentVisibility.INTERNAL,
        )
        from django.core.files.uploadedfile import SimpleUploadedFile

        ticket_svc.add_attachment(
            self.ticket,
            actor=self.support,
            file_obj=SimpleUploadedFile("secret.pdf", b"%PDF-1.4 internal-only"),
            comment=note,
        )

        self.client_api.force_authenticate(self.requester)
        data = self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/").json()["data"]
        self.assertNotIn("secret.pdf", [row["original_filename"] for row in data["attachments"]])

        self.client_api.force_authenticate(self.support)
        data = self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/").json()["data"]
        self.assertIn("secret.pdf", [row["original_filename"] for row in data["attachments"]])

    def test_empty_comment_list_shape(self):
        self.client_api.force_authenticate(self.requester)
        payload = self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/comments/").json()
        # success_response coerces empty lists to {}.
        self.assertEqual(payload["data"], {})

    def test_dashboard_counts_visible_tickets_only(self):
        ticket_svc.create_ticket(
            actor=self.outsider, title="Beta issue", description="x", category="HELP", priority="LOW",
        )
        self.client_api.force_authenticate(self.requester)
        data = self.client_api.get("/v1/support/dashboard/").json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["requested_by_me"], 1)
        self.assertEqual(data["by_status"][TicketStatus.OPEN], 1)


class TicketPermissionSeedTests(TestCase):
    def test_seed_ticket_permissions_registers_and_attaches_school_defaults(self):
        call_command("seed_actions", verbosity=0)
        call_command("seed_prebuilt_role_templates", verbosity=0)
        call_command("seed_ticket_permissions", verbosity=0)

        self.assertTrue(Permission.objects.filter(key="tickets.ticket.view").exists())
        self.assertTrue(Permission.objects.filter(key="tickets.comment.post").exists())
        # Creation is keyless by design — the key must not exist.
        self.assertFalse(Permission.objects.filter(key="tickets.ticket.create").exists())
        teacher = PrebuiltRoleTemplate.objects.get(key="teacher")
        self.assertTrue(
            PrebuiltRolePermission.objects.filter(
                prebuilt_role=teacher,
                permission_id="tickets.ticket.view",
            ).exists()
        )
