"""Console overview aggregate - security matrix first, then per-section numbers.

The security-critical tests are the FIRST group: this endpoint requires nothing
but an active account, so every number inside it has to be gated individually.
A section the caller cannot otherwise fetch must be ABSENT from the payload -
absent, not zero, because a zero would read as real data on the screen.

The second group pins each section's arithmetic against data built for the case,
and the third pins tenant isolation on the one section that spans tenants.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from vs_notifications.constants import ChannelChoices
from vs_notifications.models import Notification, NotificationEventType
from vs_rbac.models import (
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
    UserPermissionOverride,
)
from vs_rbac.tests.helpers import (
    make_branch,
    make_permission,
    make_school,
    make_school_admin,
    make_vision_user,
)
from vs_schools.models import School, SchoolStatus
from vs_todo.models import Task
from vs_user.tokens import CodeXRefreshToken

OVERVIEW_URL = "/v1/admin/dashboard/overview/"

PERM_SCHOOLS = "platform.schools.view"
PERM_TEAM = "platform.team.view"
PERM_HEALTH = "platform.health.view"
PERM_TICKETS = "tickets.ticket.view"
PERM_ROLES = "platform.roles.view"
PERM_ORGANOGRAM = "platform.organogram.view"


def grant_without_a_role(user, key):
    """Give *user* a key via a personal ALLOW, leaving zero role assignments.

    ``grant`` below necessarily creates an assignment to carry the key, which
    would make ``setup.roles_assigned`` true as a side effect of granting the
    permission that reveals it. This is the only way to observe the false case.
    """
    UserPermissionOverride.objects.create(
        tenant=user.tenant,
        user=user,
        permission=make_permission(key),
        mode=UserPermissionOverride.Mode.ALLOW,
        reason="overview setup-flag test",
    )


def grant(user, *keys):
    """Give *user* an active role on their own tenant carrying *keys*."""
    role, _ = TenantRoleTemplate.objects.get_or_create(
        tenant=user.tenant, key=f"overview-test-{user.pk}",
        defaults={"name": f"Overview Test Role {user.pk}", "status": "ACTIVE"},
    )
    for key in keys:
        TenantRolePermission.objects.get_or_create(
            role=role, permission=make_permission(key),
        )
    TenantUserRoleAssignment.objects.get_or_create(
        tenant=user.tenant, user=user, role=role,
        defaults={"assignment_status": "ACTIVE"},
    )
    return role


class OverviewTestBase(TestCase):
    def setUp(self):
        self.school = make_school(slug="ov-school", name="Overview School")
        self.branch = make_branch(self.school)
        self.user = make_vision_user(email="ov-cx@codex.test")

    def client_for(self, user):
        client = APIClient()
        token = CodeXRefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def fetch(self, user=None, tenant=None):
        user = user or self.user
        slug = tenant or user.tenant.slug
        resp = self.client_for(user).get(f"{OVERVIEW_URL}?tenant={slug}")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]


class OverviewPermissionTests(OverviewTestBase):
    """Every gated section must be absent without its own permission key."""

    def test_anonymous_is_rejected(self):
        resp = APIClient().get(OVERVIEW_URL)
        self.assertIn(resp.status_code, (401, 403))

    def test_ungranted_user_gets_no_gated_sections(self):
        data = self.fetch()
        for section in ("schools", "team", "health", "tickets", "setup"):
            self.assertNotIn(
                section, data,
                f"{section} leaked to a user holding no permission for it",
            )

    def test_schools_section_needs_the_schools_view_key(self):
        self.assertNotIn("schools", self.fetch())
        grant(self.user, PERM_SCHOOLS)
        self.assertIn("schools", self.fetch())

    def test_team_section_needs_the_team_view_key(self):
        self.assertNotIn("team", self.fetch())
        grant(self.user, PERM_TEAM)
        self.assertIn("team", self.fetch())

    def test_health_section_needs_the_health_view_key(self):
        self.assertNotIn("health", self.fetch())
        grant(self.user, PERM_HEALTH)
        self.assertIn("health", self.fetch())

    def test_tickets_section_needs_the_ticket_view_key(self):
        self.assertNotIn("tickets", self.fetch())
        grant(self.user, PERM_TICKETS)
        self.assertIn("tickets", self.fetch())

    def test_one_key_does_not_unlock_the_others(self):
        # Guards against a single blanket gate being reintroduced over the lot.
        grant(self.user, PERM_SCHOOLS)
        data = self.fetch()
        self.assertIn("schools", data)
        for section in ("team", "health", "tickets"):
            self.assertNotIn(section, data)

    def test_own_data_sections_need_no_key(self):
        # These replace endpoints that were open to any active account and only
        # ever return the caller's own rows.
        data = self.fetch()
        for section in ("approvals", "submissions", "notifications"):
            self.assertIn(section, data)

    def test_tasks_section_is_cx_staff_only(self):
        school_admin = make_school_admin(self.branch, email="ov-admin@school.test")
        self.assertNotIn("tasks", self.fetch(school_admin, tenant=self.school.tenant.slug))
        self.assertIn("tasks", self.fetch())


class OverviewSectionTests(OverviewTestBase):
    """Each section's arithmetic, against data built for the case."""

    def test_schools_counts_only_active(self):
        make_school(slug="ov-active", name="Active", status=SchoolStatus.ACTIVE)
        make_school(slug="ov-inactive", name="Inactive", status=SchoolStatus.INACTIVE)
        grant(self.user, PERM_SCHOOLS)
        expected = School.objects.filter(status=SchoolStatus.ACTIVE).count()
        self.assertEqual(self.fetch()["schools"]["active"], expected)
        # Guard the assertion itself: an all-INACTIVE fixture would pass 0 == 0.
        self.assertGreaterEqual(expected, 1)
        self.assertTrue(School.objects.filter(status=SchoolStatus.INACTIVE).exists())

    def test_team_counts_active_cx_staff(self):
        make_vision_user(email="ov-cx2@codex.test")
        grant(self.user, PERM_TEAM)
        # Both CX users, and the school admin below must not be counted.
        make_school_admin(self.branch, email="ov-notcx@school.test")
        self.assertEqual(self.fetch()["team"]["total"], 2)

    def test_task_stats_and_the_three_listed_items(self):
        today = timezone.localdate()
        # 2 overdue, 1 high, 1 medium, 1 low, 1 done → in_progress 3, overdue 2.
        Task.objects.create(assignee=self.user, title="Overdue A", deadline=today - timedelta(days=3), priority="LOW")
        Task.objects.create(assignee=self.user, title="Overdue B", deadline=today - timedelta(days=1), priority="LOW")
        Task.objects.create(assignee=self.user, title="High", deadline=today + timedelta(days=5), priority="HIGH")
        Task.objects.create(assignee=self.user, title="Medium", deadline=today + timedelta(days=2), priority="MEDIUM")
        Task.objects.create(assignee=self.user, title="Low", deadline=today + timedelta(days=1), priority="LOW")
        Task.objects.create(assignee=self.user, title="Done", deadline=today, priority="HIGH", is_done=True)

        tasks = self.fetch()["tasks"]
        self.assertEqual(tasks["stats"]["overdue"], 2)
        self.assertEqual(tasks["stats"]["in_progress"], 3)
        self.assertEqual(tasks["stats"]["done"], 1)

        # Ordering: overdue first (nearest deadline first), then by priority.
        # Completed tasks never appear, and the list stops at three.
        self.assertEqual(
            [item["title"] for item in tasks["items"]],
            ["Overdue A", "Overdue B", "High"],
        )

    def test_tasks_are_only_the_callers_own(self):
        other = make_vision_user(email="ov-other@codex.test")
        Task.objects.create(
            assignee=other, title="Not mine",
            deadline=timezone.localdate() + timedelta(days=1), priority="HIGH",
        )
        tasks = self.fetch()["tasks"]
        self.assertEqual(tasks["stats"]["total"], 0)
        self.assertEqual(tasks["items"], [])

    def test_unread_counts_only_the_callers_unread_in_app(self):
        event = NotificationEventType.objects.create(
            key="overview.test", label="Overview test",
            source_module="vs_notifications",
            supported_channels=[ChannelChoices.IN_APP],
        )
        other = make_vision_user(email="ov-notif@codex.test")
        common = dict(tenant=self.user.tenant, event_type=event, body="b")
        Notification.objects.create(recipient=self.user, channel=ChannelChoices.IN_APP, is_read=False, **common)
        Notification.objects.create(recipient=self.user, channel=ChannelChoices.IN_APP, is_read=False, **common)
        Notification.objects.create(recipient=self.user, channel=ChannelChoices.IN_APP, is_read=True, **common)
        Notification.objects.create(recipient=self.user, channel=ChannelChoices.EMAIL, is_read=False, **common)
        Notification.objects.create(recipient=other, channel=ChannelChoices.IN_APP, is_read=False, **common)

        self.assertEqual(self.fetch()["notifications"]["unread"], 2)

    def test_health_reports_posture_not_the_whole_command_centre(self):
        grant(self.user, PERM_HEALTH)
        health = self.fetch()["health"]
        self.assertEqual(set(health), {"label", "overall", "active_incidents"})

    def test_empty_state_is_zeros_not_missing_keys(self):
        # The screen reads these unconditionally; a missing key would render as
        # "undefined" rather than 0.
        data = self.fetch()
        self.assertEqual(data["approvals"]["pending"], 0)
        self.assertEqual(data["submissions"]["returned"], 0)
        self.assertEqual(data["notifications"]["unread"], 0)
        self.assertEqual(data["tasks"]["stats"]["total"], 0)


class OverviewSetupFlagTests(OverviewTestBase):
    """The "Getting started" flags - each gated by the screen it describes.

    These back a checklist, so a wrong answer is cosmetic; a leaked one is not.
    Each flag has to be absent without its own key, and present-and-correct with
    it, exactly like the sections above.
    """

    def test_setup_is_absent_without_either_key(self):
        self.assertNotIn("setup", self.fetch())

    def test_roles_flag_needs_a_roles_view_key(self):
        grant(self.user, PERM_ORGANOGRAM)
        self.assertNotIn("roles_assigned", self.fetch()["setup"])
        grant(self.user, PERM_ROLES)
        self.assertIn("roles_assigned", self.fetch()["setup"])

    def test_organogram_flag_needs_the_organogram_view_key(self):
        grant(self.user, PERM_ROLES)
        self.assertNotIn("organogram_built", self.fetch()["setup"])
        grant(self.user, PERM_ORGANOGRAM)
        self.assertIn("organogram_built", self.fetch()["setup"])

    def test_roles_assigned_is_false_with_no_active_assignment(self):
        grant_without_a_role(self.user, PERM_ROLES)
        self.assertIs(self.fetch()["setup"]["roles_assigned"], False)

    def test_roles_assigned_is_true_once_a_role_is_assigned(self):
        # `grant` assigns a role to carry the key, which is itself the condition.
        grant(self.user, PERM_ROLES)
        self.assertIs(self.fetch()["setup"]["roles_assigned"], True)

    def test_roles_assigned_ignores_revoked_assignments(self):
        grant_without_a_role(self.user, PERM_ROLES)
        role = TenantRoleTemplate.objects.create(
            tenant=self.user.tenant, key="ov-revoked", name="Revoked",
            status="ACTIVE",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=self.user.tenant, user=self.user, role=role,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.REVOKED,
        )
        self.assertIs(self.fetch()["setup"]["roles_assigned"], False)

    def test_roles_assigned_is_scoped_to_the_callers_tenant(self):
        # The platform tenant has assignments (self.user's own); a school actor
        # must not see them reflected in their own checklist.
        grant(self.user, PERM_ROLES)
        school_admin = make_school_admin(self.branch, email="ov-setup@school.test")
        grant_without_a_role(school_admin, PERM_ROLES)
        data = self.fetch(school_admin, tenant=self.school.tenant.slug)
        self.assertIs(data["setup"]["roles_assigned"], False)

    def test_organogram_built_flips_when_a_node_exists(self):
        from vs_user.models import OrgNode

        grant(self.user, PERM_ORGANOGRAM)
        self.assertIs(self.fetch()["setup"]["organogram_built"], False)
        OrgNode.objects.create(
            name="Engineering", code="DV-ENG", kind=OrgNode.Kind.DIVISION,
        )
        self.assertIs(self.fetch()["setup"]["organogram_built"], True)

    def test_organogram_built_ignores_inactive_nodes(self):
        from vs_user.models import OrgNode

        grant(self.user, PERM_ORGANOGRAM)
        OrgNode.objects.create(
            name="Retired", code="DV-OLD", kind=OrgNode.Kind.DIVISION,
            is_active=False,
        )
        self.assertIs(self.fetch()["setup"]["organogram_built"], False)


class OverviewTenantIsolationTests(OverviewTestBase):
    """The team count is the one section that could span tenants."""

    def test_school_actor_counts_no_platform_staff(self):
        school_admin = make_school_admin(self.branch, email="ov-iso@school.test")
        grant(school_admin, PERM_TEAM)
        # Several CX staff exist on the platform tenant (self.user among them).
        make_vision_user(email="ov-iso-cx@codex.test")
        data = self.fetch(school_admin, tenant=self.school.tenant.slug)
        # Scoped to the school's own tenant, which has no CX_STAFF rows.
        self.assertEqual(data["team"]["total"], 0)

    def test_platform_actor_keeps_the_platform_wide_count(self):
        grant(self.user, PERM_TEAM)
        make_vision_user(email="ov-wide-cx@codex.test")
        self.assertEqual(self.fetch()["team"]["total"], 2)
