"""
Tests for vs_user auth flows.

Covers the security-review fixes:
- B13: lock state is only revealed after a correct password (no oracle).
- B14: failed attempts record the email as entered, even for unknown accounts.
- B10: logout ends only the submitted session, not every device.
- B11: refresh rotation updates only the matching session's JTI.
"""
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from vs_user.models import AccountLockout, AuthAttempt, LoginSession, User
from vs_user.services.auth import LoginService


class PlatformUserCreationTests(TestCase):
    """CX hires receive staff IDs and failed workflow setup is atomic."""

    def setUp(self):
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_tenants.models import Tenant

        self.tenant = Tenant.objects.get(slug="codex", kind="PLATFORM")
        self.actor = make_cx_user(email="creator@codex.test")
        self.actor.first_name = "Sole"
        self.actor.last_name = "Admin"
        self.actor.save(update_fields=["first_name", "last_name", "updated_at"])
        self.super_role = TenantRoleTemplate.objects.create(
            tenant=self.tenant, key="xvs_super_admin", name="XVS Super Admin",
        )
        self.hire_role = TenantRoleTemplate.objects.create(
            tenant=self.tenant, key="xvs_platform_admin", name="Platform Admin",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=self.tenant, user=self.actor, role=self.super_role,
            assignment_status="ACTIVE",
        )

    def _validated_data(self, email, employee_id=None):
        profile_prefill = {}
        if employee_id:
            profile_prefill["employee_id"] = employee_id
        return {
            "email": email,
            "first_name": "New",
            "last_name": "Hire",
            "gender": "MALE",
            "phone": "08012345678",
            "tenant": self.tenant,
            "user_type": "CX_STAFF",
            "role": self.hire_role.name,
            "role_instance": self.hire_role,
            "branch": None,
            "position_instance": None,
            "profile_prefill": profile_prefill,
        }

    def test_missing_employee_ids_are_generated_sequentially_before_approval(self):
        from vs_user.services.user import UserCreationService

        first = UserCreationService.create_pending(
            self._validated_data("first.hire@codex.test"), self.actor,
        )
        second = UserCreationService.create_pending(
            self._validated_data("second.hire@codex.test"), self.actor,
        )

        self.assertEqual(first.platform_staff_profile.employee_id, "CX-1")
        self.assertEqual(second.platform_staff_profile.employee_id, "CX-2")
        self.assertEqual(first.status, User.Status.PENDING_APPROVAL)

    def test_explicit_employee_id_is_preserved(self):
        from vs_user.services.user import UserCreationService

        user = UserCreationService.create_pending(
            self._validated_data("manual.id@codex.test", "CX-42"), self.actor,
        )

        self.assertEqual(user.platform_staff_profile.employee_id, "CX-42")

    def test_local_nigerian_phone_number_is_accepted(self):
        from types import SimpleNamespace
        from vs_user.serializers import UserCreateSerializer

        request = SimpleNamespace(user=self.actor, tenant=self.tenant)
        serializer = UserCreateSerializer(data={
            "first_name": "Local",
            "last_name": "Phone",
            "email": "local.phone@codex.test",
            "gender": "FEMALE",
            "phone": "08012345678",
            "role": self.hire_role.key,
        }, context={"request": request})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["phone"], "08012345678")

    def test_workflow_failure_rolls_back_the_pending_user(self):
        from vs_workflow.exceptions import TemplateNotFoundError

        client = APIClient()
        client.force_authenticate(user=self.actor)
        with mock.patch(
            "vs_user.views.accounts._wf_submit",
            side_effect=TemplateNotFoundError("missing template"),
        ):
            response = client.post("/v1/user/users/", {
                "first_name": "Rolled",
                "last_name": "Back",
                "email": "rolled.back@codex.test",
                "gender": "MALE",
                "phone": "08012345678",
                "role": self.hire_role.key,
            }, format="json")

        self.assertEqual(response.status_code, 404)
        self.assertFalse(User.objects.filter(email="rolled.back@codex.test").exists())

    def test_sole_admin_creation_auto_approves_and_sends_invitation(self):
        from vs_user.models import UserInvitation

        client = APIClient()
        client.force_authenticate(user=self.actor)
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            response = client.post("/v1/user/users/", {
                "first_name": "Auto",
                "last_name": "Approved",
                "email": "auto.approved@codex.test",
                "gender": "FEMALE",
                "phone": "08012345678",
                "role": self.hire_role.key,
            }, format="json")

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["workflow_instance"]["status"], "APPROVED")
        user = User.objects.get(email="auto.approved@codex.test")
        self.assertEqual(user.status, User.Status.PENDING)
        self.assertTrue(UserInvitation.objects.filter(user=user).exists())

    def test_repair_command_submits_an_existing_orphan_once(self):
        from vs_user.models import UserInvitation
        from vs_user.services.user import UserCreationService
        from vs_workflow.models import WorkflowInstance

        orphan = UserCreationService.create_pending(
            self._validated_data("orphaned.hire@codex.test"), self.actor,
        )
        orphan.platform_staff_profile.employee_id = None
        orphan.platform_staff_profile.save(update_fields=["employee_id", "updated_at"])

        output = StringIO()
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay"):
            call_command(
                "repair_pending_user_approvals",
                email=orphan.email,
                stdout=output,
            )
            call_command(
                "repair_pending_user_approvals",
                email=orphan.email,
                stdout=output,
            )

        orphan.refresh_from_db()
        self.assertEqual(orphan.status, User.Status.PENDING)
        self.assertIsNotNone(orphan.platform_staff_profile.employee_id)
        self.assertTrue(UserInvitation.objects.filter(user=orphan).exists())
        self.assertEqual(
            WorkflowInstance.objects.filter(document_object_id=str(orphan.pk)).count(),
            1,
        )


class UserListScopeTests(TestCase):
    """Platform user lists keep CX and tenant-bound accounts separate."""

    def setUp(self):
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory
        from vs_user.views.accounts import UserAccountViewSet

        self.cx_user = make_cx_user(email="scope-cx@codex.test")
        school = make_school(name="Scope School", slug="scope-school")
        self.school_user = make_school_admin(school, email="scope-admin@school.test")
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        role = TenantRoleTemplate.objects.create(
            tenant=school.tenant, key="school-administrator", name="School Administrator",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=school.tenant,
            user=self.school_user,
            role=role,
            assignment_status="ACTIVE",
        )
        self.request_class = Request
        self.request_factory = APIRequestFactory()
        self.view_class = UserAccountViewSet

    def _queryset_for(self, query: str):
        request = self.request_class(self.request_factory.get(f"/v1/user/users/{query}"))
        request._user = self.cx_user
        view = self.view_class()
        view.request = request
        return view.get_queryset()

    def test_cx_user_type_filter_returns_only_cx_staff(self):
        users = self._queryset_for("?user_type=CX_STAFF")
        self.assertQuerySetEqual(users, [self.cx_user], transform=lambda user: user)

    def test_school_scope_excludes_cx_staff(self):
        users = self._queryset_for("?scope=school")
        self.assertQuerySetEqual(users, [self.school_user], transform=lambda user: user)

    def test_school_scope_serializes_placement_and_active_role(self):
        from vs_user.serializers import UserListSerializer

        user = self._queryset_for("?scope=school").get(pk=self.school_user.pk)
        data = UserListSerializer(user).data

        self.assertEqual(data["school_name"], "Scope School")
        self.assertEqual(data["role"], "School Administrator")


def make_cx_user(email="staff@codex.test", password="Str0ng!pass123"):
    return User.objects.create_user(
        email=email,
        password=password,
        user_type="CX_STAFF",
        status="ACTIVE",
        first_name="Code",
        last_name="Xer",
    )


def make_school(name="Caleb International College", slug="caleb"):
    from vs_schools.models import School
    return School.objects.create(name=name, slug=slug, status="ACTIVE")


def make_school_admin(school, email="admin@caleb.test", password="Str0ng!pass123"):
    return User.objects.create_user(
        email=email,
        password=password,
        user_type="SCHOOL_ADMIN",
        status="ACTIVE",
        first_name="Ada",
        last_name="Obi",
        tenant=school.tenant,
    )


class MyPositionAssignmentsTests(TestCase):
    """Self-service history never exposes another staff member's assignments."""

    def setUp(self):
        from vs_user.models import OrgNode, Position, PositionAssignment

        self.user = make_cx_user(email="my-history@codex.test")
        self.other = make_cx_user(email="other-history@codex.test")
        division = OrgNode.objects.create(
            name="History Division", code="HISTORY", kind=OrgNode.Kind.DIVISION,
        )
        own_position = Position.objects.create(
            title="My Position", code="MY-POS", org_node=division,
        )
        other_position = Position.objects.create(
            title="Other Position", code="OTHER-POS", org_node=division,
        )
        self.own_assignment = PositionAssignment.objects.create(
            user=self.user, position=own_position,
        )
        self.other_assignment = PositionAssignment.objects.create(
            user=self.other, position=other_position,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_user_without_organogram_permission_can_read_own_history(self):
        response = self.client.get("/v1/user/organogram/assignments/mine/")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["pagination"]["totalItems"], 1)
        self.assertEqual(response.json()["data"][0]["id"], self.own_assignment.id)

    def test_user_query_parameter_cannot_expose_another_users_history(self):
        response = self.client.get(
            "/v1/user/organogram/assignments/mine/",
            {"user": str(self.other.id)},
        )

        self.assertEqual(response.status_code, 200, response.content)
        returned_ids = {item["id"] for item in response.json()["data"]}
        self.assertEqual(returned_ids, {self.own_assignment.id})
        self.assertNotIn(self.other_assignment.id, returned_ids)


class OrganogramTreeTests(TestCase):
    """build_tree nests active seats and never drops a subtree whose parent seat
    is inactive/removed (which would blank the chart)."""

    def setUp(self):
        from vs_user.models import OrgNode

        self.division = OrgNode.objects.create(
            name="Eng", code="ENG", kind=OrgNode.Kind.DIVISION,
        )

    def test_active_root_and_child_nest(self):
        from vs_user.models import Position
        from vs_user.services.organogram import OrganogramService

        root = Position.objects.create(title="CTO", code="CTO", org_node=self.division)
        child = Position.objects.create(
            title="Eng Lead", code="LEAD", org_node=self.division, reports_to=root,
        )
        tree = OrganogramService.build_tree()
        self.assertEqual([n["id"] for n in tree], [root.id])
        self.assertEqual([c["id"] for c in tree[0]["direct_reports"]], [child.id])

    def test_child_of_inactive_parent_surfaces_as_root(self):
        from vs_user.models import Position
        from vs_user.services.organogram import OrganogramService

        parent = Position.objects.create(
            title="Ghost", code="GHOST", org_node=self.division, is_active=False,
        )
        child = Position.objects.create(
            title="Orphan", code="ORPHAN", org_node=self.division, reports_to=parent,
        )
        tree = OrganogramService.build_tree()
        root_ids = [n["id"] for n in tree]
        self.assertIn(child.id, root_ids)       # surfaced, not dropped
        self.assertNotIn(parent.id, root_ids)   # inactive parent excluded


class OrganogramListQueryTests(TestCase):
    """The org-node and position list endpoints must not be N+1 — three queries
    per seat (holders/vacancy/open-seats) made the Manage page hang over a
    high-latency DB. The query count must stay bounded as seats grow."""

    def setUp(self):
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_tenants.models import Tenant
        from vs_user.models import OrgNode, Position, PositionAssignment

        tenant = Tenant.objects.get(slug="codex", kind="PLATFORM")
        self.actor = make_cx_user(email="org-viewer@codex.test")
        super_role = TenantRoleTemplate.objects.create(
            tenant=tenant, key="xvs_super_admin", name="Super",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=tenant, user=self.actor, role=super_role, assignment_status="ACTIVE",
        )

        division = OrgNode.objects.create(
            name="Eng", code="ENG", kind=OrgNode.Kind.DIVISION,
        )
        # A dozen occupied seats: under N+1 this list would fire ~36 extra
        # queries; with the prefetch it is a small constant.
        for i in range(12):
            pos = Position.objects.create(
                title=f"Seat {i}", code=f"SEAT-{i}", org_node=division,
            )
            holder = make_cx_user(email=f"holder{i}@codex.test")
            PositionAssignment.objects.create(user=holder, position=pos)

        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)

    def test_positions_list_is_not_n_plus_one(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get("/v1/user/organogram/positions/?page_size=100")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.json()["data"]), 12)
        # Bounded well below the ~40+ an N+1 over 12 seats would produce.
        self.assertLess(len(ctx.captured_queries), 20)

    def test_org_nodes_list_is_not_n_plus_one(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get("/v1/user/organogram/nodes/?page_size=100")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertLess(len(ctx.captured_queries), 20)


class LoginLockoutOracleTests(TestCase):
    """B13 — wrong-password attempts must never reveal the locked state."""

    def setUp(self):
        self.password = "Str0ng!pass123"
        self.user = make_cx_user(password=self.password)

    def _lock(self):
        lockout, _ = AccountLockout.objects.get_or_create(user=self.user)
        lockout.register_failure(ip="127.0.0.1", lock_threshold=1, lock_minutes=15)
        lockout.save()
        self.assertTrue(lockout.is_locked_now())

    def test_wrong_password_on_locked_account_says_invalid_credentials(self):
        self._lock()
        with self.assertRaises(ValueError) as ctx:
            LoginService.login(self.user.email, "wrong-password")
        self.assertEqual(ctx.exception.args[0]["code"], "INVALID_CREDENTIALS")

    def test_correct_password_on_locked_account_reveals_lock(self):
        self._lock()
        with self.assertRaises(ValueError) as ctx:
            LoginService.login(self.user.email, self.password)
        self.assertEqual(ctx.exception.args[0]["code"], "ACCOUNT_LOCKED")

    def test_successful_login_returns_tokens(self):
        result = LoginService.login(self.user.email, self.password)
        self.assertIn("access", result)
        self.assertIn("refresh", result)
        self.assertTrue(
            LoginSession.objects.filter(user=self.user, is_active=True).exists()
        )


class FailedAttemptAuditTests(TestCase):
    """B14 — the attempted email is recorded even when no account matches."""

    def test_unknown_email_is_recorded_as_entered(self):
        with self.assertRaises(ValueError):
            LoginService.login("ghost@nowhere.test", "whatever")
        attempt = AuthAttempt.objects.latest("id")
        self.assertEqual(attempt.email_entered, "ghost@nowhere.test")
        self.assertEqual(attempt.result, AuthAttempt.Result.FAIL)

    def test_known_email_wrong_password_recorded(self):
        user = make_cx_user()
        with self.assertRaises(ValueError):
            LoginService.login(user.email, "wrong-password")
        attempt = AuthAttempt.objects.latest("id")
        self.assertEqual(attempt.email_entered, user.email)


class SessionScopedLogoutTests(TestCase):
    """B10/B11 — multi-device session integrity."""

    def setUp(self):
        self.password = "Str0ng!pass123"
        self.user = make_cx_user(password=self.password)
        self.device_a = LoginService.login(self.user.email, self.password)
        self.device_b = LoginService.login(self.user.email, self.password)
        self.assertEqual(
            LoginSession.objects.filter(user=self.user, is_active=True).count(), 2
        )

    def _client(self, login_result):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login_result['access']}")
        return client

    def test_logout_ends_only_the_submitted_session(self):
        resp = self._client(self.device_a).post(
            "/v1/user/auth/logout/", {"refresh": self.device_a["refresh"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        active = LoginSession.objects.filter(user=self.user, is_active=True)
        self.assertEqual(active.count(), 1)
        self.assertEqual(active.first().id, self.device_b["session_id"])

    def test_refresh_updates_only_matching_session_jti(self):
        session_b_before = LoginSession.objects.get(pk=self.device_b["session_id"])

        resp = self._client(self.device_a).post(
            "/v1/user/auth/token/refresh/", {"refresh": self.device_a["refresh"]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)

        session_b_after = LoginSession.objects.get(pk=self.device_b["session_id"])
        self.assertEqual(session_b_before.refresh_jti, session_b_after.refresh_jti)


class SelfServiceSecurityScopeTests(TestCase):
    """The My Security endpoints expose and revoke only the caller's records."""

    def setUp(self):
        self.password = "Str0ng!pass123"
        self.user = make_cx_user(email="my-security@codex.test", password=self.password)
        self.other = make_cx_user(email="other-security@codex.test", password=self.password)
        self.own_login = LoginService.login(self.user.email, self.password)
        self.other_login = LoginService.login(self.other.email, self.password)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_my_sessions_lists_only_the_caller(self):
        response = self.client.get("/v1/user/sessions/mine/?page_size=50")

        self.assertEqual(response.status_code, 200, response.content)
        returned_users = {item["user"]["id"] for item in response.json()["data"]}
        self.assertEqual(returned_users, {self.user.id})

    def test_my_auth_attempts_lists_only_the_caller(self):
        response = self.client.get("/v1/user/auth-attempts/mine/?page_size=50")

        self.assertEqual(response.status_code, 200, response.content)
        returned_emails = {item["email_entered"] for item in response.json()["data"]}
        self.assertEqual(returned_emails, {self.user.email})

    def test_user_without_security_permission_cannot_use_admin_lists(self):
        sessions = self.client.get("/v1/user/sessions/")
        attempts = self.client.get("/v1/user/auth-attempts/")

        self.assertEqual(sessions.status_code, 403, sessions.content)
        self.assertEqual(attempts.status_code, 403, attempts.content)

    def test_user_cannot_end_another_users_session(self):
        response = self.client.post(
            f"/v1/user/sessions/{self.other_login['session_id']}/end-mine/",
            format="json",
        )

        self.assertEqual(response.status_code, 404, response.content)
        self.assertTrue(
            LoginSession.all_objects.get(pk=self.other_login["session_id"]).is_active,
        )

    def test_end_all_mine_leaves_another_users_session_active(self):
        response = self.client.post("/v1/user/sessions/end-all-mine/", format="json")

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(LoginSession.all_objects.get(pk=self.own_login["session_id"]).is_active)
        self.assertTrue(LoginSession.all_objects.get(pk=self.other_login["session_id"]).is_active)


# =============================================================================
# WP-B1 — school branding in auth payloads (A.1)
# =============================================================================

class SchoolBrandingPayloadTests(TestCase):
    """The login / me payloads carry a nested `school` object for school users.

    Login is exercised through ``LoginService.login`` (the service returns the
    payload dict that the view wraps verbatim) with a real request so absolute
    logo URLs are built — this mirrors the existing tests in this module and
    sidesteps the login rate-throttle. ``/me`` is hit over HTTP.
    """

    def setUp(self):
        from django.test import RequestFactory
        self.password = "Str0ng!pass123"
        self.school = make_school()
        self.admin = make_school_admin(self.school, password=self.password)
        self.factory = RequestFactory()

    def _login(self, user, password):
        request = self.factory.post("/v1/user/auth/login/")
        return LoginService.login(user.email, password, request=request)

    def _me(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get("/v1/user/auth/me/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]

    def _add_branding(self, logo=True):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from vs_schools.models import SchoolBranding
        branding = SchoolBranding(school=self.school)
        if logo:
            branding.logo = SimpleUploadedFile(
                "caleb.png", b"\x89PNG\r\n\x1a\n-fake-png", content_type="image/png"
            )
        branding.save()
        return branding

    def test_login_includes_school_object_with_logo(self):
        self._add_branding(logo=True)
        data = self._login(self.admin, self.password)

        self.assertIn("school", data)
        school = data["school"]
        self.assertIsNotNone(school)
        self.assertEqual(school["id"], self.school.id)
        self.assertEqual(school["name"], self.school.name)
        self.assertEqual(school["slug"], self.school.slug)
        self.assertIsNotNone(school["logo"])
        # Absolute URL built from the request.
        self.assertTrue(school["logo"].startswith("http"))
        self.assertIn("caleb", school["logo"])

    def test_login_school_object_logo_null_when_no_branding(self):
        data = self._login(self.admin, self.password)
        self.assertIsNotNone(data["school"])
        self.assertEqual(data["school"]["name"], self.school.name)
        self.assertIsNone(data["school"]["logo"])

    def test_login_school_object_logo_null_when_branding_has_no_logo(self):
        self._add_branding(logo=False)
        data = self._login(self.admin, self.password)
        self.assertIsNotNone(data["school"])
        self.assertIsNone(data["school"]["logo"])

    def test_login_school_null_for_cx_staff(self):
        cx = make_cx_user(password=self.password)
        data = self._login(cx, self.password)
        self.assertIn("school", data)
        self.assertIsNone(data["school"])

    def test_existing_flat_fields_unchanged(self):
        # console-fe compatibility: additive change must not touch existing fields.
        # The user payload now carries tenant identity (tenant_slug/tenant_name)
        # instead of the legacy flat school_name; school identity lives in the
        # nested `school` object.
        data = self._login(self.admin, self.password)
        self.assertIn("user", data)
        self.assertIn("access", data)
        self.assertIn("permissions", data)
        self.assertEqual(data["user"]["tenant_name"], self.school.name)
        self.assertEqual(data["user"]["tenant_slug"], self.school.slug)
        self.assertEqual(data["school"]["name"], self.school.name)

    def test_me_returns_same_school_object(self):
        self._add_branding(logo=True)
        login_data = self._login(self.admin, self.password)
        me_data = self._me(self.admin)

        self.assertIn("school", me_data)
        self.assertEqual(me_data["school"]["id"], login_data["school"]["id"])
        self.assertEqual(me_data["school"]["name"], login_data["school"]["name"])
        self.assertEqual(me_data["school"]["slug"], login_data["school"]["slug"])
        self.assertEqual(me_data["school"]["logo"], login_data["school"]["logo"])

    def test_me_school_null_for_cx_staff(self):
        cx = make_cx_user(password=self.password)
        me_data = self._me(cx)
        self.assertIsNone(me_data["school"])


class EmailFailureResilienceTests(TestCase):
    """
    Regression — an SMTP outage during eager (in-process) email sending must
    not 500 the request.

    Email now flows through the vs_notifications engine: the vs_user task
    dispatches (synchronously, cheaply) and the engine's
    deliver_email_notification does the SMTP send. Under eager mode the delivery
    task runs in-process; its eager guard treats the first failure as final, so
    celery.exceptions.Retry never propagates through the HTTP request even when
    smtp.zoho.com is unreachable — the PasswordResetRequest / UserInvitation row
    is already persisted.
    """

    RESET_URL = "/v1/user/auth/password/reset/request/"

    def setUp(self):
        from apps.celery import app as celery_app
        from vs_notifications.services.seed import (
            seed_event_types, seed_notification_templates,
        )

        # The event registry + DB templates are not seeded by migrations, so the
        # engine has nothing to render/dispatch without this.
        seed_event_types()
        seed_notification_templates()

        self.celery_app = celery_app
        self._old_eager = celery_app.conf.task_always_eager
        self._old_propagates = celery_app.conf.task_eager_propagates
        # Mirror staging: tasks run in-process and exceptions propagate.
        celery_app.conf.task_always_eager = True
        celery_app.conf.task_eager_propagates = True
        self.client = APIClient()

    def tearDown(self):
        self.celery_app.conf.task_always_eager = self._old_eager
        self.celery_app.conf.task_eager_propagates = self._old_propagates

    @staticmethod
    def _smtp_down(*args, **kwargs):
        import smtplib
        raise smtplib.SMTPConnectError(421, "smtp.zoho.com unreachable")

    def test_reset_request_returns_200_when_eager_smtp_send_fails(self):
        from unittest import mock

        from vs_user.models import PasswordResetRequest

        user = make_cx_user(email="reset-smtp@codex.test")
        with mock.patch("vs_notifications.tasks.send_email", side_effect=self._smtp_down):
            resp = self.client.post(self.RESET_URL, {"email": user.email}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            PasswordResetRequest.objects.filter(user=user, used_at__isnull=True).exists(),
            "reset row must exist so the emailed link (once SMTP recovers) works",
        )

    def test_reset_request_returns_200_when_broker_down_and_smtp_fails(self):
        """Broker unreachable → .delay() raises → .apply() fallback → SMTP fails."""
        from unittest import mock

        from vs_user import tasks
        from vs_user.models import PasswordResetRequest

        user = make_cx_user(email="reset-broker@codex.test")
        self.celery_app.conf.task_always_eager = False  # force the broker path
        with mock.patch.object(
            tasks.send_password_reset_email_task, "delay",
            side_effect=Exception("broker connection refused"),
        ), mock.patch("vs_notifications.tasks.send_email", side_effect=self._smtp_down):
            resp = self.client.post(self.RESET_URL, {"email": user.email}, format="json")

        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            PasswordResetRequest.objects.filter(user=user, used_at__isnull=True).exists()
        )

    def test_invitation_email_eager_smtp_failure_marks_failed_without_raising(self):
        """Engine path: dispatch → deliver task fails under eager → receiver marks
        the invitation FAILED via the notification_failed signal, no retry."""
        from datetime import timedelta
        from unittest import mock

        from django.utils import timezone

        from vs_user.models import UserInvitation
        from vs_user.tasks import send_invitation_email_task

        user = make_cx_user(email="invitee@codex.test")
        invitation = UserInvitation.objects.create(
            user=user,
            invited_by=user,
            expires_at=timezone.now() + timedelta(days=7),
            is_used=False,
        )

        with mock.patch("vs_notifications.tasks.send_email", side_effect=self._smtp_down):
            # dispatch enqueues the delivery task via transaction.on_commit —
            # capture and execute it so the eager delivery runs and fails.
            with self.captureOnCommitCallbacks(execute=True):
                send_invitation_email_task.apply(
                    kwargs={"activation_key": str(user.activation_key)}
                )

        invitation.refresh_from_db()
        self.assertEqual(invitation.email_status, UserInvitation.EmailStatus.FAILED)
        self.assertEqual(
            invitation.email_attempts, 1,
            "eager mode must not retry in-process",
        )
        # The engine stores the raw exception string on failure_reason, which the
        # receiver copies into email_last_error (the old per-exception label
        # classifier is gone).
        self.assertIn("unreachable", invitation.email_last_error)


from django.test import override_settings


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="CodeX System <system@codexng.com>",
    EMAIL_CC=[],
    FRONTEND_BASE_URL="https://intranet.codexng.com",
)
class InvitationEngineDispatchTests(TestCase):
    """The vs_user email tasks now flow through the notification engine.

    Verifies the dispatch record + metadata, the receiver-driven invitation
    tracking on success, and the per-message From (from_name) parity.
    """

    def setUp(self):
        from vs_notifications.services.seed import (
            seed_event_types, seed_notification_templates,
        )
        seed_event_types()
        seed_notification_templates()

    def _invitation_for(self, user, invited_by=None):
        from datetime import timedelta

        from django.utils import timezone

        from vs_user.models import UserInvitation
        return UserInvitation.objects.create(
            user=user, invited_by=invited_by or user,
            expires_at=timezone.now() + timedelta(days=7), is_used=False,
        )

    def test_invitation_dispatch_creates_notification_with_activation_key(self):
        from unittest import mock

        from vs_notifications.constants import ChannelChoices
        from vs_notifications.models import Notification
        from vs_user.tasks import send_invitation_email_task

        user = make_cx_user(email="invited@codex.test")
        user.invited_by_name = "Ada Admin"
        user.save(update_fields=["invited_by_name"])

        with mock.patch("vs_notifications.tasks.deliver_email_notification.delay"):
            send_invitation_email_task.apply(
                kwargs={"activation_key": str(user.activation_key)}
            )

        notif = Notification.objects.get(recipient=user, channel=ChannelChoices.EMAIL)
        self.assertEqual(notif.event_type.key, "user.invited")
        self.assertEqual(notif.metadata.get("activation_key"), str(user.activation_key))
        self.assertEqual(notif.metadata.get("from_name"), "Ada Admin")

    def test_successful_delivery_updates_invitation_via_receiver(self):
        from django.core import mail

        from vs_user.models import UserInvitation
        from vs_user.tasks import send_invitation_email_task

        user = make_cx_user(email="invited-ok@codex.test")
        invitation = self._invitation_for(user)

        with self.captureOnCommitCallbacks(execute=True):
            send_invitation_email_task.apply(
                kwargs={"activation_key": str(user.activation_key)}
            )

        invitation.refresh_from_db()
        self.assertEqual(invitation.email_status, UserInvitation.EmailStatus.SENT)
        self.assertEqual(invitation.email_attempts, 1)
        self.assertIsNotNone(invitation.email_sent_at)
        self.assertEqual(len(mail.outbox), 1)

    def test_from_name_lands_in_outgoing_from_header(self):
        from django.core import mail

        from vs_user.tasks import send_invitation_email_task

        user = make_cx_user(email="fromname@codex.test")
        user.invited_by_name = "Bola Inviter"
        user.save(update_fields=["invited_by_name"])
        self._invitation_for(user)

        with self.captureOnCommitCallbacks(execute=True):
            send_invitation_email_task.apply(
                kwargs={"activation_key": str(user.activation_key)}
            )

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Bola Inviter", mail.outbox[0].from_email)
        # Address portion is preserved from DEFAULT_FROM_EMAIL.
        self.assertIn("system@codexng.com", mail.outbox[0].from_email)

    def test_password_reset_dispatch_creates_notification(self):
        from unittest import mock

        from vs_notifications.constants import ChannelChoices
        from vs_notifications.models import Notification
        from vs_user.tasks import send_password_reset_email_task

        user = make_cx_user(email="pwreset@codex.test")

        with mock.patch("vs_notifications.tasks.deliver_email_notification.delay"):
            send_password_reset_email_task.apply(
                kwargs={
                    "activation_key": str(user.activation_key),
                    "origin": "SELF",
                    "sender_name": "CodeX System",
                }
            )

        notif = Notification.objects.get(recipient=user, channel=ChannelChoices.EMAIL)
        self.assertEqual(notif.event_type.key, "user.password_reset")
        self.assertEqual(notif.metadata.get("from_name"), "CodeX System")
        self.assertIn("reset-password", notif.body)


class PasswordPolicyTests(TestCase):
    """The canonical policy (12 + upper/lower/digit/special) is enforced by the
    validator and advertised, unauthenticated, by the policy endpoint."""

    POLICY_URL = "/v1/user/auth/password/policy/"

    def test_validator_rejects_passwords_that_miss_any_rule(self):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError

        weak = [
            "Sh0rt!Aa",          # only 8 chars — too short
            "alllowercase1!",    # no uppercase
            "ALLUPPERCASE1!",    # no lowercase
            "NoDigitsHere!!",    # no digit
            "NoSpecialChar12",   # no special character
        ]
        for password in weak:
            with self.assertRaises(ValidationError, msg=f"expected {password!r} to be rejected"):
                validate_password(password)

    def test_validator_accepts_a_compliant_password(self):
        from django.contrib.auth.password_validation import validate_password

        validate_password("Str0ng!pass123")  # 14 chars, upper+lower+digit+special

    def test_policy_endpoint_is_public_and_lists_requirements(self):
        resp = APIClient().get(self.POLICY_URL)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()["data"]
        self.assertEqual(data["min_length"], 12)
        self.assertTrue(data["require_special"])
        self.assertEqual(len(data["requirements"]), 5)


class DraftAndBulkUserTests(TestCase):
    """Save-as-draft and CSV bulk upload park DRAFT CX hires; submit promotes a
    draft into the normal approval flow."""

    def setUp(self):
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_tenants.models import Tenant

        self.tenant = Tenant.objects.get(slug="codex", kind="PLATFORM")
        self.actor = make_cx_user(email="bulk.creator@codex.test")
        self.hire_role = TenantRoleTemplate.objects.create(
            tenant=self.tenant, key="xvs_platform_admin", name="Platform Admin",
        )
        # Super-admin assignment gives the actor the RBAC bypass used by the
        # sibling platform-user tests.
        self.super_role = TenantRoleTemplate.objects.create(
            tenant=self.tenant, key="xvs_super_admin", name="XVS Super Admin",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=self.tenant, user=self.actor, role=self.super_role,
            assignment_status="ACTIVE",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)

    def test_save_as_draft_creates_draft_without_role_or_workflow(self):
        from vs_rbac.models import TenantUserRoleAssignment

        resp = self.client.post("/v1/user/users/", {
            "first_name": "Draft", "last_name": "Hire",
            "email": "draft.hire@codex.test", "gender": "MALE",
            "save_as_draft": True,
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        user = User.objects.get(email="draft.hire@codex.test")
        self.assertEqual(user.status, User.Status.DRAFT)
        self.assertFalse(user.is_active)
        # No role given → no assignment written until the draft is submitted.
        self.assertFalse(TenantUserRoleAssignment.objects.filter(user=user).exists())

    def test_submit_draft_with_role_enters_approval(self):
        self.client.post("/v1/user/users/", {
            "first_name": "Ready", "last_name": "Hire",
            "email": "ready.hire@codex.test", "gender": "FEMALE",
            "role": self.hire_role.key, "save_as_draft": True,
        }, format="json")
        user = User.objects.get(email="ready.hire@codex.test")
        self.assertEqual(user.status, User.Status.DRAFT)

        with mock.patch("vs_user.views.accounts._wf_submit") as wf, \
                mock.patch("vs_user.views.accounts._WFInstanceSerializer") as wf_ser:
            wf.return_value = object()
            wf_ser.return_value.data = {"id": "wf-1"}
            resp = self.client.post(f"/v1/user/users/{user.id}/submit/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.PENDING_APPROVAL)
        wf.assert_called_once()

    def test_submit_draft_without_role_is_rejected(self):
        self.client.post("/v1/user/users/", {
            "first_name": "Roleless", "last_name": "Draft",
            "email": "roleless.draft@codex.test", "gender": "MALE",
            "save_as_draft": True,
        }, format="json")
        user = User.objects.get(email="roleless.draft@codex.test")
        resp = self.client.post(f"/v1/user/users/{user.id}/submit/", {}, format="json")
        self.assertEqual(resp.status_code, 400, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.DRAFT)

    def test_bulk_template_downloads_csv(self):
        resp = self.client.get("/v1/user/users/bulk-template/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp["Content-Type"])
        header = resp.content.decode("utf-8-sig").splitlines()[0]
        self.assertIn("first_name", header)
        self.assertIn("email", header)

    def test_bulk_upload_creates_drafts_and_reports_row_errors(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        csv_body = (
            "first_name,last_name,email,role,phone,gender,job_title,employment_type,date_joined\n"
            "Bulk,One,bulk.one@codex.test,,08012345678,MALE,Analyst,FULL_TIME,\n"
            "Bulk,Two,not-an-email,,,,,,\n"  # invalid email → row-level error
        )
        upload = SimpleUploadedFile("staff.csv", csv_body.encode("utf-8"), content_type="text/csv")
        resp = self.client.post("/v1/user/users/bulk-upload/", {"file": upload}, format="multipart")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["summary"]["created"], 1)
        self.assertEqual(data["summary"]["failed"], 1)
        self.assertEqual(User.objects.get(email="bulk.one@codex.test").status, User.Status.DRAFT)
        self.assertEqual(data["errors"][0]["row"], 3)
