from __future__ import annotations

import datetime
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

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
from schools.vs_schools.models import School, SchoolStatus
from vs_tenants.models import Branch
from vs_user.models import User

from . import analytics as guide_analytics
from .constants import (
    CommentVisibility,
    GuideAnalyticsEventName,
    TicketPermission,
    TicketStatus,
)
from .models import GuideAnalyticsEvent, TicketAuditLog, TicketSubscription
from .services import tickets as ticket_svc
from .services import visibility
from .views import GuideAnalyticsEventView

REQUESTER_KEYS = (
    TicketPermission.VIEW,
    TicketPermission.COMMENT,
    TicketPermission.ATTACH,
)


def _school(slug, name):
    return School.objects.create(slug=slug, name=name, code=slug.upper(), status=SchoolStatus.ACTIVE)


def _branch(school, name):
    return Branch.objects.create(
        tenant=school.tenant, name=name, _type="Primary", is_main=True,
    )


def _user(email, first, last, *, school=None, branch=None, tenant=None):
    """A ticket-side account. ``school`` is accepted for call-site readability.

    With no branch and no tenant the account is platform support: being support
    staff IS being on the platform tenant, and there is no persona column left
    to say so on the account itself. The tenant is named here rather than
    derived, because a derivation from an absence is exactly what this change
    removed.
    """
    if tenant is None and branch is None:
        from vs_tenants.models import Tenant

        tenant = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
    return User.objects.create_user(
        email=email,
        first_name=first,
        last_name=last,
        tenant=tenant,
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
            school=self.school_a, branch=self.branch_a,
        )
        self.peer = _user(
            "peer@alpha.test", "Paul", "Peer",
            school=self.school_a, branch=self.branch_a,
        )
        self.norole = _user(
            "norole@alpha.test", "Nora", "Norole",
            school=self.school_a, branch=self.branch_a,
        )
        self.outsider = _user(
            "outsider@beta.test", "Bola", "Outsider",
            school=self.school_b, branch=self.branch_b,
        )
        self.support = _user(
            "support@cx.test", "Ada", "Support",
        )
        self.other_support = _user(
            "tier2@cx.test", "Tolu", "Tier",
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


class GuideAnalyticsTests(TicketFixtureMixin, TestCase):
    def setUp(self):
        self.build_users()
        self.client = APIClient()
        self.client.force_authenticate(self.requester)

    def test_active_user_can_record_only_closed_guide_event_fields(self):
        response = self.client.post(
            "/v1/support/guides/analytics/events/",
            {
                "name": GuideAnalyticsEventName.HELPFUL_VOTED,
                "guide_id": "getting-started.console-basics",
                "outcome": "helpful",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        event = GuideAnalyticsEvent.objects.get()
        self.assertEqual(event.guide_id, "getting-started.console-basics")
        self.assertEqual(event.outcome, "helpful")
        self.assertNotIn("tenant", {
            field.name for field in GuideAnalyticsEvent._meta.get_fields()
        })

        refused = self.client.post(
            "/v1/support/guides/analytics/events/",
            {
                "name": GuideAnalyticsEventName.GUIDE_VIEWED,
                "guide_id": "getting-started.console-basics",
                "email": "reader@alpha.test",
            },
            format="json",
        )
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(GuideAnalyticsEvent.objects.count(), 1)

    def test_event_ingest_is_scoped_and_rate_limited_per_user(self):
        payload = {
            "name": GuideAnalyticsEventName.GUIDE_VIEWED,
            "guide_id": "getting-started.console-basics",
        }

        with (
            mock.patch.object(
                GuideAnalyticsEventView,
                "throttle_classes",
                [ScopedRateThrottle],
            ),
            mock.patch.object(
                ScopedRateThrottle,
                "THROTTLE_RATES",
                {"guide_analytics": "2/minute"},
            ),
        ):
            cache.clear()
            first = self.client.post(
                "/v1/support/guides/analytics/events/", payload, format="json",
            )
            second = self.client.post(
                "/v1/support/guides/analytics/events/", payload, format="json",
            )
            refused = self.client.post(
                "/v1/support/guides/analytics/events/", payload, format="json",
            )
            cache.clear()

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(refused.status_code, 429, refused.content)
        self.assertEqual(GuideAnalyticsEvent.objects.count(), 2)

    def test_no_result_search_redacts_unknown_words_and_numbers(self):
        response = self.client.post(
            "/v1/support/guides/analytics/events/",
            {
                "name": GuideAnalyticsEventName.SEARCH_NO_RESULTS,
                "query": "permission denied for Ada Okafor invoice 8842",
                "route_pattern": "/support/guides",
                "result_count": 0,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        event = GuideAnalyticsEvent.objects.get()
        self.assertEqual(
            event.search_query,
            "permission denied [redacted] invoice",
        )
        self.assertNotIn("Ada", event.search_query)
        self.assertNotIn("8842", event.search_query)

    def test_summary_requires_platform_health_permission_and_exposes_no_tenant_split(self):
        GuideAnalyticsEvent.objects.create(
            name=GuideAnalyticsEventName.GUIDE_VIEWED,
            guide_id="getting-started.console-basics",
        )
        GuideAnalyticsEvent.objects.create(
            name=GuideAnalyticsEventName.GUIDE_VIEWED,
            guide_id="audit.investigate-event",
        )

        denied = self.client.get("/v1/support/guides/analytics/summary/")
        self.assertEqual(denied.status_code, 403)

        _grant(
            self.support.tenant,
            self.support,
            ("platform.health.view",),
            role_name="Guide Editor",
        )
        self.client.force_authenticate(self.support)
        response = self.client.get("/v1/support/guides/analytics/summary/?days=30")
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(data["totals"][GuideAnalyticsEventName.GUIDE_VIEWED], 2)
        self.assertEqual(
            [row["guide_id"] for row in data["guides"]],
            ["audit.investigate-event", "getting-started.console-basics"],
        )
        self.assertNotIn("tenant", str(data))

    def test_prune_deletes_only_events_past_retention(self):
        from .tasks import prune_guide_analytics_task

        old = GuideAnalyticsEvent.objects.create(
            name=GuideAnalyticsEventName.GUIDE_VIEWED,
            guide_id="getting-started.console-basics",
        )
        recent = GuideAnalyticsEvent.objects.create(
            name=GuideAnalyticsEventName.GUIDE_COMPLETED,
            guide_id="getting-started.console-basics",
        )
        cutoff = timezone.now() - datetime.timedelta(days=guide_analytics.RETENTION_DAYS + 1)
        GuideAnalyticsEvent.objects.filter(pk=old.pk).update(occurred_at=cutoff)

        self.assertEqual(prune_guide_analytics_task()["deleted"], 1)
        self.assertFalse(GuideAnalyticsEvent.objects.filter(pk=old.pk).exists())
        self.assertTrue(GuideAnalyticsEvent.objects.filter(pk=recent.pk).exists())


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

        today = timezone.localdate()
        self.assertEqual(ticket.ticket_number, f"TK-{ticket.tenant_id}{today:%y%m%d}1")
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
        # TK-<tenant_id><YYMMDD><n>: same tenant + day share the prefix; n is a plain,
        # un-padded integer that starts at 1 and increments.
        from django.utils import timezone
        prefix = f"TK-{first.tenant_id}{timezone.localdate():%y%m%d}"
        self.assertTrue(first.ticket_number.startswith(prefix))
        self.assertEqual(first.ticket_number[len(prefix):], "1")
        self.assertEqual(second.ticket_number[len(prefix):], "2")

    def test_ticket_number_counter_is_per_tenant(self):
        # Each tenant counts independently: tenant B starts at 1 even after
        # tenant A has already raised a ticket the same day.
        from django.utils import timezone
        a1 = ticket_svc.create_ticket(
            actor=self.requester, title="A1", description="x", category="HELP", priority="LOW",
        )
        b1 = ticket_svc.create_ticket(
            actor=self.outsider, title="B1", description="x", category="HELP", priority="LOW",
        )
        a2 = ticket_svc.create_ticket(
            actor=self.requester, title="A2", description="x", category="HELP", priority="LOW",
        )
        self.assertNotEqual(a1.tenant_id, b1.tenant_id)
        today = f"{timezone.localdate():%y%m%d}"
        self.assertEqual(a1.ticket_number, f"TK-{a1.tenant_id}{today}1")
        self.assertEqual(b1.ticket_number, f"TK-{b1.tenant_id}{today}1")
        self.assertEqual(a2.ticket_number, f"TK-{a2.tenant_id}{today}2")

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

    def test_ticket_creation_keeps_only_validated_product_context(self):
        client = APIClient()
        client.force_authenticate(self.requester)
        response = client.post("/v1/support/tickets/", {
            "title": "Guide did not resolve the issue",
            "description": "I still need help.",
            "category": "HELP",
            "priority": "LOW",
            "context": {
                "guide_id": "getting-started.console-basics",
                "route_pattern": "/support/tickets/:id",
                "product_area": "Support",
                "app_version": "2026.8.13",
            },
        }, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["data"]["context"], {
            "guide_id": "getting-started.console-basics",
            "route_pattern": "/support/tickets/:id",
            "product_area": "Support",
            "app_version": "2026.8.13",
        })

        export_response = client.post("/v1/support/tickets/", {
            "title": "Export guide did not resolve the issue",
            "description": "I still need help.",
            "category": "HELP",
            "priority": "LOW",
            "context": {
                "route_pattern": "/export/files",
                "product_area": "Exports",
            },
        }, format="json")
        self.assertEqual(export_response.status_code, 201, export_response.content)

    def test_ticket_context_rejects_unknown_keys_and_live_url_data(self):
        client = APIClient()
        client.force_authenticate(self.requester)
        base = {
            "title": "Unsafe context",
            "description": "This payload must fail.",
            "category": "HELP",
            "priority": "LOW",
        }

        unknown = client.post("/v1/support/tickets/", {
            **base,
            "context": {"route_pattern": "/overview", "email": "person@example.test"},
        }, format="json")
        live_url = client.post("/v1/support/tickets/", {
            **base,
            "context": {"route_pattern": "/support/tickets/4831?tab=activity"},
        }, format="json")

        self.assertEqual(unknown.status_code, 400)
        self.assertIn("email", str(unknown.json()))
        self.assertEqual(live_url.status_code, 400)

    def test_a_ticket_may_carry_the_registered_onboarding_context(self):
        """Registered by vs_onboarding from its own AppConfig, not declared here.

        The values are asserted against the school module's own constants, so
        this test fails if the registration is dropped, and it does it without
        this app importing anything under ``apps/schools/`` in production code.
        """
        client = APIClient()
        client.force_authenticate(self.requester)

        response = client.post("/v1/support/tickets/", {
            "title": "Cannot finish the books step",
            "description": "The set of books task will not complete.",
            "category": "HELP",
            "priority": "LOW",
            "context": {
                "product_area": "Onboarding",
                "route_pattern": "/onboarding/tasks",
                "onboarding_task_key": "SCHOOL_METADATA",
                "onboarding_readiness_state": "NOT_READY",
            },
        }, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["data"]["context"], {
            "product_area": "Onboarding",
            "route_pattern": "/onboarding/tasks",
            "onboarding_task_key": "SCHOOL_METADATA",
            "onboarding_readiness_state": "NOT_READY",
        })

    def test_a_registered_key_still_refuses_a_value_outside_its_vocabulary(self):
        """Registering a key widens the allowlist; it does not open the field."""
        client = APIClient()
        client.force_authenticate(self.requester)

        response = client.post("/v1/support/tickets/", {
            "title": "Smuggled value",
            "description": "This payload must fail.",
            "category": "HELP",
            "priority": "LOW",
            "context": {"onboarding_task_key": "Ada Obi, 12 Marina Road"},
        }, format="json")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("onboarding_task_key", str(response.json()))

    def test_an_unregistered_key_is_still_rejected_after_registration_exists(self):
        """The allowlist is still an allowlist, not a door left open."""
        client = APIClient()
        client.force_authenticate(self.requester)

        response = client.post("/v1/support/tickets/", {
            "title": "Unregistered key",
            "description": "This payload must fail.",
            "category": "HELP",
            "priority": "LOW",
            "context": {
                "onboarding_task_key": "DEFAULT_ROLES",
                "onboarding_student_name": "Ada",
            },
        }, format="json")

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("onboarding_student_name", str(response.json()))

    def test_requester_reply_on_unassigned_ticket_notifies_support_queue(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Still locked out",
            description="x",
            category="HELP",
            priority="LOW",
        )

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.add_comment(
                    ticket,
                    actor=self.requester,
                    body="Is anyone there?",
                    visibility=CommentVisibility.PUBLIC,
                )

        send.assert_called_once()
        recipients = send.call_args.kwargs["recipients"]
        self.assertEqual(
            {user.pk for user in recipients},
            {self.support.pk, self.other_support.pk},
        )

    def test_commenters_follow_and_receive_later_public_comments(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Resolvers are collaborating",
            description="x",
            category="HELP",
            priority="MEDIUM",
        )
        ticket_svc.add_comment(
            ticket,
            actor=self.support,
            body="I have started the investigation.",
            visibility=CommentVisibility.PUBLIC,
        )
        subscription = TicketSubscription.objects.get(
            ticket=ticket,
            user=self.support,
        )
        self.assertEqual(subscription.source, TicketSubscription.Source.COMMENTED)
        self.assertIsNone(subscription.muted_at)

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.add_comment(
                    ticket,
                    actor=self.other_support,
                    body="The logs point to the export worker.",
                    visibility=CommentVisibility.PUBLIC,
                )

        recipients = send.call_args.kwargs["recipients"]
        self.assertEqual(
            {user.pk for user in recipients},
            {self.requester.pk, self.support.pk},
        )

    def test_commenter_receives_resolution_notification(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Resolution should reach collaborators",
            description="x",
            category="HELP",
            priority="MEDIUM",
        )
        ticket_svc.add_comment(
            ticket,
            actor=self.support,
            body="I found the cause.",
            visibility=CommentVisibility.PUBLIC,
        )

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.transition_ticket(
                    ticket,
                    actor=self.other_support,
                    status=TicketStatus.RESOLVED,
                )

        self.assertEqual(send.call_args.kwargs["event_key"], "ticket.resolved")
        self.assertEqual(
            {user.pk for user in send.call_args.kwargs["recipients"]},
            {self.requester.pk, self.support.pk},
        )

    def test_internal_comment_notifies_only_authorized_followers(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Private resolver collaboration",
            description="x",
            category="HELP",
            priority="MEDIUM",
        )
        ticket_svc.add_comment(
            ticket,
            actor=self.support,
            body="I am following this ticket.",
            visibility=CommentVisibility.PUBLIC,
        )

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.add_comment(
                    ticket,
                    actor=self.other_support,
                    body="Private diagnostic detail.",
                    visibility=CommentVisibility.INTERNAL,
                )

        self.assertEqual(
            {user.pk for user in send.call_args.kwargs["recipients"]},
            {self.support.pk},
        )

    def test_stale_follower_without_ticket_access_is_not_notified(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Tenant boundary remains enforced",
            description="x",
            category="HELP",
            priority="MEDIUM",
        )
        TicketSubscription.objects.create(
            ticket=ticket,
            user=self.outsider,
            source=TicketSubscription.Source.MANUAL,
        )

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.add_comment(
                    ticket,
                    actor=self.support,
                    body="This stays inside the authorized audience.",
                    visibility=CommentVisibility.PUBLIC,
                )

        self.assertEqual(
            {user.pk for user in send.call_args.kwargs["recipients"]},
            {self.requester.pk},
        )

    def test_follower_can_mute_and_commenting_follows_again(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Mute a noisy collaboration",
            description="x",
            category="HELP",
            priority="MEDIUM",
        )
        ticket_svc.add_comment(
            ticket,
            actor=self.support,
            body="I can help.",
            visibility=CommentVisibility.PUBLIC,
        )
        client = APIClient()
        client.force_authenticate(self.support)

        muted = client.delete(f"/v1/support/tickets/{ticket.pk}/follow/")
        self.assertEqual(muted.status_code, 200, muted.content)
        self.assertFalse(muted.json()["data"]["is_following"])
        self.assertFalse(
            client.get(f"/v1/support/tickets/{ticket.pk}/").json()["data"]["is_following"]
        )

        with mock.patch(
            "vs_tickets.services.notifications.NotificationService.send"
        ) as send:
            with self.captureOnCommitCallbacks(execute=True):
                ticket_svc.add_comment(
                    ticket,
                    actor=self.other_support,
                    body="Muted resolvers should not receive this.",
                    visibility=CommentVisibility.PUBLIC,
                )
        self.assertEqual(
            {user.pk for user in send.call_args.kwargs["recipients"]},
            {self.requester.pk},
        )

        ticket_svc.add_comment(
            ticket,
            actor=self.support,
            body="I am participating again.",
            visibility=CommentVisibility.PUBLIC,
        )
        self.assertTrue(
            client.get(f"/v1/support/tickets/{ticket.pk}/").json()["data"]["is_following"]
        )

    def test_visible_resolver_can_follow_without_commenting(self):
        ticket = ticket_svc.create_ticket(
            actor=self.requester,
            title="Follow before joining the conversation",
            description="x",
            category="HELP",
            priority="MEDIUM",
        )
        client = APIClient()
        client.force_authenticate(self.support)
        self.assertFalse(
            client.get(f"/v1/support/tickets/{ticket.pk}/").json()["data"]["is_following"]
        )

        response = client.post(f"/v1/support/tickets/{ticket.pk}/follow/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["data"]["is_following"])
        self.assertTrue(
            TicketSubscription.objects.filter(
                ticket=ticket,
                user=self.support,
                muted_at__isnull=True,
            ).exists()
        )

    def test_visibility_is_participant_manager_and_support_scoped(self):
        mine = ticket_svc.create_ticket(
            actor=self.requester, title="Mine", description="x", category="HELP", priority="LOW",
        )
        other = ticket_svc.create_ticket(
            actor=self.outsider, title="Other", description="x", category="HELP", priority="LOW",
        )

        self.assertNotIn(mine, visibility.visible_tickets_qs(self.peer))
        self.assertNotIn(other, visibility.visible_tickets_qs(self.peer))
        self.assertIn(mine, visibility.visible_tickets_qs(self.support))
        self.assertIn(other, visibility.visible_tickets_qs(self.support))

    def test_school_manage_grant_does_not_leak_cross_tenant(self):
        # A SCHOOL user holding tickets.ticket.manage manages tickets inside
        # their own tenant only - the cross-tenant support span is reserved
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

    def test_school_wide_visibility_is_not_granted_by_view_permission(self):
        mine = ticket_svc.create_ticket(
            actor=self.requester, title="Mine", description="x", category="HELP", priority="LOW",
        )
        # Same school, but no role grants: only their own tickets are visible.
        self.assertNotIn(mine, visibility.visible_tickets_qs(self.norole))
        self.assertFalse(visibility.can_view_ticket(self.norole, mine))

        # Even a peer with the ordinary view/comment/attach grants must not
        # enter another employee's private ticket thread.
        self.assertNotIn(mine, visibility.visible_tickets_qs(self.peer))
        self.assertFalse(visibility.can_view_ticket(self.peer, mine))

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

    def test_cross_tenant_user_cannot_follow_ticket(self):
        self.client_api.force_authenticate(self.outsider)
        response = self.client_api.post(f"/v1/support/tickets/{self.ticket.pk}/follow/")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            TicketSubscription.objects.filter(
                ticket=self.ticket,
                user=self.outsider,
            ).exists()
        )

    def test_same_tenant_peer_cannot_list_or_open_another_users_ticket(self):
        self.client_api.force_authenticate(self.peer)
        listing = self.client_api.get("/v1/support/tickets/").json()["data"]
        rows = listing.get("results", listing) if isinstance(listing, dict) else listing
        self.assertNotIn(self.ticket.pk, [row["id"] for row in rows])
        self.assertEqual(
            self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/").status_code,
            404,
        )
        self.assertEqual(
            self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/comments/").status_code,
            404,
        )

    def test_requester_cannot_transition_own_ticket(self):
        self.client_api.force_authenticate(self.requester)
        response = self.client_api.post(
            f"/v1/support/tickets/{self.ticket.pk}/transition/",
            {"status": TicketStatus.CLOSED},
        )
        self.assertEqual(response.status_code, 403)

    def test_requester_can_edit_own_ticket_details(self):
        self.client_api.force_authenticate(self.requester)

        detail = self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/")
        response = self.client_api.patch(
            f"/v1/support/tickets/{self.ticket.pk}/",
            {"title": "Requester clarified the export failure"},
            format="json",
        )

        self.assertTrue(detail.json()["data"]["capabilities"]["can_update"])
        self.assertEqual(response.status_code, 200, response.content)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.title, "Requester clarified the export failure")

    def test_assigned_resolver_cannot_edit_requester_details(self):
        ticket_svc.assign_ticket(
            self.ticket,
            actor=self.support,
            assignee=self.support,
        )
        self.client_api.force_authenticate(self.support)

        detail = self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/")
        response = self.client_api.patch(
            f"/v1/support/tickets/{self.ticket.pk}/",
            {"description": "Resolver replaced the report."},
            format="json",
        )

        self.assertFalse(detail.json()["data"]["capabilities"]["can_update"])
        self.assertEqual(response.status_code, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.description, "x")

    def test_tenant_manager_cannot_edit_requester_details(self):
        _grant(
            self.school_a,
            self.peer,
            (TicketPermission.MANAGE, TicketPermission.UPDATE),
            role_name="Alpha Ticket Manager",
        )
        self.client_api.force_authenticate(self.peer)

        detail = self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/")
        response = self.client_api.patch(
            f"/v1/support/tickets/{self.ticket.pk}/",
            {"priority": "LOW"},
            format="json",
        )

        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertFalse(detail.json()["data"]["capabilities"]["can_update"])
        self.assertEqual(response.status_code, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.priority, "HIGH")

    def test_consultant_role_can_create_a_ticket(self):
        consultant = _user(
            "consultant@cx.test", "Cora", "Consultant",
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

    def test_assignment_options_include_only_active_ticket_handlers(self):
        non_handler = _user(
            "observer@cx.test", "Olivia", "Observer",
        )
        self.client_api.force_authenticate(self.support)

        response = self.client_api.get(
            f"/v1/support/tickets/{self.ticket.pk}/eligible-assignees/",
        )

        self.assertEqual(response.status_code, 200, response.content)
        ids = {row["id"] for row in response.json()["data"]}
        self.assertIn(self.support.pk, ids)
        self.assertIn(self.other_support.pk, ids)
        self.assertNotIn(non_handler.pk, ids)

        rejected = self.client_api.post(
            f"/v1/support/tickets/{self.ticket.pk}/assign/",
            {"assignee_id": non_handler.pk},
        )
        self.assertEqual(rejected.status_code, 400)

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

    def test_attachment_download_is_authenticated_and_ticket_scoped(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        attachment = ticket_svc.add_attachment(
            self.ticket,
            actor=self.requester,
            file_obj=SimpleUploadedFile(
                "screen.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, content_type="image/png",
            ),
        )
        url = f"/v1/support/tickets/{self.ticket.pk}/attachments/{attachment.pk}/download/"

        self.client_api.force_authenticate(self.requester)
        response = self.client_api.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")

        self.client_api.force_authenticate(self.peer)
        self.assertEqual(self.client_api.get(url).status_code, 404)

    def test_declared_content_type_cannot_decide_how_a_file_is_served(self):
        """The stored type must come from the bytes, not the multipart declaration.

        The download view serves the stored content type back and uses inline
        disposition for anything image/*, so a caller who could name the type could
        have SVG markup rendered - and its script executed - in the next reader's
        session. Two independent guards now: the bytes must match the extension, and
        the stored type is derived rather than accepted.
        """
        from django.core.files.uploadedfile import SimpleUploadedFile
        from rest_framework.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            ticket_svc.add_attachment(
                self.ticket,
                actor=self.requester,
                file_obj=SimpleUploadedFile(
                    "harmless.png",
                    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                    content_type="image/svg+xml",
                ),
            )

        # A genuine PNG that merely lies about its type is stored under the real one.
        attachment = ticket_svc.add_attachment(
            self.ticket,
            actor=self.requester,
            file_obj=SimpleUploadedFile(
                "real.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, content_type="image/svg+xml",
            ),
        )
        self.assertEqual(attachment.content_type, "image/png")

    def test_spreadsheet_and_csv_attachments_are_still_accepted(self):
        """Tightening the check must not cost tickets the file types they rely on."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        csv_row = ticket_svc.add_attachment(
            self.ticket, actor=self.requester,
            file_obj=SimpleUploadedFile("export.csv", b"id,name\n1,Ada\n"),
        )
        self.assertEqual(csv_row.content_type, "text/csv")

        xlsx_row = ticket_svc.add_attachment(
            self.ticket, actor=self.requester,
            file_obj=SimpleUploadedFile("book.xlsx", b"PK\x03\x04" + b"0" * 32),
        )
        self.assertEqual(
            xlsx_row.content_type,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    def test_empty_comment_list_shape(self):
        self.client_api.force_authenticate(self.requester)
        payload = self.client_api.get(f"/v1/support/tickets/{self.ticket.pk}/comments/").json()
        # A ticket with no comments answers with an empty list, not an object.
        self.assertEqual(payload["data"], [])

    def test_dashboard_counts_visible_tickets_only(self):
        ticket_svc.create_ticket(
            actor=self.outsider, title="Beta issue", description="x", category="HELP", priority="LOW",
        )
        self.client_api.force_authenticate(self.requester)
        data = self.client_api.get("/v1/support/dashboard/").json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["requested_by_me"], 1)
        self.assertEqual(data["by_status"][TicketStatus.OPEN], 1)

    def _assigned_to_support(self, title, status):
        """One ticket assigned to ``self.support``, parked at *status*."""
        ticket = ticket_svc.create_ticket(
            actor=self.requester, title=title, description="x",
            category="SUPPORT", priority="LOW",
        )
        ticket = ticket_svc.assign_ticket(ticket, actor=self.support, assignee=self.support)
        if status != TicketStatus.ASSIGNED:
            ticket = ticket_svc.transition_ticket(ticket, actor=self.support, status=status)
        return ticket

    def test_assigned_to_me_counts_live_work_not_finished_tickets(self):
        # The defect this pins: clearing your queue left the counter unchanged,
        # because every ticket you had ever been assigned kept counting.
        self._assigned_to_support("Still mine", TicketStatus.IN_PROGRESS)
        self._assigned_to_support("Just picked up", TicketStatus.ASSIGNED)
        self._assigned_to_support("Done with it", TicketStatus.RESOLVED)
        self._assigned_to_support("Shut", TicketStatus.CLOSED)

        self.client_api.force_authenticate(self.support)
        data = self.client_api.get("/v1/support/dashboard/").json()["data"]
        self.assertEqual(data["assigned_to_me"], 2)
        # The population totals still see all of them - only the personal
        # workload counters are scoped to unfinished work.
        self.assertEqual(data["by_status"][TicketStatus.RESOLVED], 1)
        self.assertEqual(data["by_status"][TicketStatus.CLOSED], 1)

    def test_requested_by_me_counts_live_work_only(self):
        self._assigned_to_support("Mine, open", TicketStatus.IN_PROGRESS)
        self._assigned_to_support("Mine, closed", TicketStatus.CLOSED)
        self.client_api.force_authenticate(self.requester)
        data = self.client_api.get("/v1/support/dashboard/").json()["data"]
        # The setUp ticket plus the in-progress one; the closed one drops out.
        self.assertEqual(data["requested_by_me"], 2)

    def test_list_state_active_and_assignee_me_match_the_counters(self):
        live = self._assigned_to_support("Live one", TicketStatus.IN_PROGRESS)
        self._assigned_to_support("Finished one", TicketStatus.RESOLVED)

        self.client_api.force_authenticate(self.support)
        payload = self.client_api.get(
            "/v1/support/tickets/?assignee=me&state=active"
        ).json()["data"]
        rows = payload.get("results", []) if isinstance(payload, dict) else payload
        self.assertEqual([str(row["id"]) for row in rows], [str(live.pk)])

    def test_assignee_me_cannot_be_used_to_read_another_users_queue(self):
        # `me` resolves from the request, never from the value, so it can only
        # ever narrow the caller's own visible set.
        self._assigned_to_support("Support's ticket", TicketStatus.IN_PROGRESS)
        self.client_api.force_authenticate(self.peer)
        payload = self.client_api.get("/v1/support/tickets/?assignee=me").json()["data"]
        rows = payload.get("results", []) if isinstance(payload, dict) else payload
        self.assertEqual(list(rows), [])


class TicketPermissionSeedTests(TestCase):
    def test_seed_ticket_permissions_registers_and_attaches_school_defaults(self):
        call_command("seed_actions", verbosity=0)
        call_command("seed_prebuilt_role_templates", verbosity=0)
        call_command("seed_ticket_permissions", verbosity=0)

        self.assertTrue(Permission.objects.filter(key="tickets.ticket.view").exists())
        self.assertTrue(Permission.objects.filter(key="tickets.comment.post").exists())
        # Creation is keyless by design - the key must not exist.
        self.assertFalse(Permission.objects.filter(key="tickets.ticket.create").exists())
        teacher = PrebuiltRoleTemplate.objects.get(key="teacher")
        self.assertTrue(
            PrebuiltRolePermission.objects.filter(
                prebuilt_role=teacher,
                permission_id="tickets.ticket.view",
            ).exists()
        )


class TicketBranchTenantGuardTests(TestCase):
    """``Ticket.clean`` - the branch tenancy guard, after Phase C.

    The guard reads ``self.branch.tenant_id`` now instead of walking
    ``branch.school.tenant_id``. A rewrite that stopped comparing would let a
    ticket be filed against another tenant's branch, so the denial is asserted
    directly rather than only through the API.
    """

    @classmethod
    def setUpTestData(cls):
        cls.school = _school("guard-alpha", "Guard Alpha")
        cls.branch = _branch(cls.school, "Main")
        cls.rival = _school("guard-beta", "Guard Beta")
        cls.rival_branch = _branch(cls.rival, "Main")
        # Branch-optional shape: this tenant owns no branches at all.
        cls.branchless = _school("guard-solo", "Guard Solo")
        cls.requester = _user(
            "guard.requester@alpha.test", "Gina", "Guard",
            branch=cls.branch,
        )
        # A tenant user with no branch: this tenant owns no branches, and a
        # null branch is a legal school-wide posting - the shape tested here.
        cls.branchless_requester = User.objects.create_user(
            email="guard.solo@solo.test", first_name="Solo", last_name="Guard",
            status=User.Status.ACTIVE,
            tenant=cls.branchless.tenant,
        )

    def _ticket(self, **kwargs):
        from .models import Ticket

        defaults = {
            "tenant": self.school.tenant,
            "requester": self.requester,
            "title": "Guard",
            "description": "Guard",
        }
        defaults.update(kwargs)
        return Ticket(**defaults)

    def test_a_branch_from_another_tenant_is_rejected(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            self._ticket(branch=self.rival_branch).clean()

    def test_a_branch_in_the_same_tenant_is_accepted(self):
        self._ticket(branch=self.branch).clean()  # must not raise

    def test_a_ticket_without_a_branch_is_accepted(self):
        self._ticket(branch=None).clean()  # must not raise

    def test_a_branchless_tenant_cannot_borrow_a_branch(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            self._ticket(
                tenant=self.branchless.tenant,
                requester=self.branchless_requester,
                branch=self.branch,
            ).clean()


# ---------------------------------------------------------------------------
# The context registry itself
# ---------------------------------------------------------------------------

class TicketContextRegistryTests(TestCase):
    """The mechanism that lets a module widen the allowlist without an import.

    The same shape the Export Centre uses for datasets: the owning app calls in
    from its own ``AppConfig.ready``. What is tested here is that calling in is
    the only way, and that it cannot be used to smuggle a free-text field past
    an allowlist that exists precisely to prevent one.
    """

    def setUp(self):
        from vs_tickets import context

        self.context = context
        self.original = dict(context._REGISTERED)
        self.addCleanup(self._restore)

    def _restore(self):
        self.context._REGISTERED.clear()
        self.context._REGISTERED.update(self.original)

    def test_onboarding_registered_its_two_keys_with_closed_vocabularies(self):
        from schools.vs_onboarding.constants import ReadinessState, TaskKey

        registered = self.context.registered_choice_fields()

        self.assertEqual(
            registered["onboarding_task_key"], tuple(TaskKey.values),
        )
        self.assertEqual(
            registered["onboarding_readiness_state"], tuple(ReadinessState.values),
        )
        self.assertIn("onboarding_task_key", self.context.allowed_keys())
        self.assertIn("guide_id", self.context.allowed_keys())

    def test_a_value_that_is_not_a_plain_identifier_cannot_be_registered(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            self.context.register_context_choice_field(
                "demo_key", choices=["Ada Obi, 12 Marina Road"],
            )

    def test_a_key_cannot_shadow_one_this_app_declares_itself(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            self.context.register_context_choice_field(
                "product_area", choices=["Anything"],
            )

    def test_re_registering_the_same_values_is_a_no_op(self):
        self.context.register_context_choice_field("demo_key", choices=["A", "B"])
        self.context.register_context_choice_field("demo_key", choices=["A", "B"])

        self.assertEqual(
            self.context.registered_choice_fields()["demo_key"], ("A", "B"),
        )

    def test_re_registering_different_values_is_refused(self):
        from django.core.exceptions import ImproperlyConfigured

        self.context.register_context_choice_field("demo_key", choices=["A"])

        with self.assertRaises(ImproperlyConfigured):
            self.context.register_context_choice_field("demo_key", choices=["B"])

    def test_an_empty_vocabulary_is_refused(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaises(ImproperlyConfigured):
            self.context.register_context_choice_field("demo_key", choices=[])

    def test_vs_tickets_does_not_import_the_school_package(self):
        """The boundary this mechanism exists to keep, asserted as text.

        A grep, deliberately: an import test that only checked ``sys.modules``
        would pass in a process where something else had already imported the
        school app.
        """
        import pathlib

        app_dir = pathlib.Path(__file__).resolve().parent
        offenders = []
        for path in app_dir.rglob("*.py"):
            if path.name == "tests.py":
                continue  # the tests may name the module they are asserting about
            source = path.read_text()
            if "from schools." in source or "import schools." in source:
                offenders.append(path.name)

        self.assertEqual(offenders, [])
