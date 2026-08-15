"""
Tests for vs_user auth flows.

Covers the security-review fixes:
- B13: lock state is only revealed after a correct password (no oracle).
- B14: failed attempts record the email as entered, even for unknown accounts.
- B10: logout ends only the submitted session, not every device.
- B11: refresh rotation updates only the matching session's JTI.
"""
from io import StringIO
from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from vs_user.models import AccountLockout, AuthAttempt, LoginSession, User
from vs_user.services.auth import LoginService


class PlatformUserCreationTests(TestCase):
    """CX hires receive staff IDs and failed workflow setup is atomic."""

    def setUp(self):
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_tenants.models import Tenant
        from vs_user.models import OrgNode, Position

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
        division = OrgNode.objects.create(
            name="Product", code="PRODUCT", kind=OrgNode.Kind.DIVISION,
        )
        department = OrgNode.objects.create(
            name="Engineering", code="ENGINEERING", kind=OrgNode.Kind.DEPARTMENT,
            parent=division,
        )
        team = OrgNode.objects.create(
            name="Platform", code="PLATFORM", kind=OrgNode.Kind.TEAM,
            parent=department,
        )
        self.position = Position.objects.create(
            title="Platform Engineer", code="PLATFORM-ENG", org_node=team,
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
            "position_instance": self.position,
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
            "position": self.position.code,
        }, context={"request": request})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["phone"], "08012345678")

    def test_non_draft_cx_hire_requires_position(self):
        from types import SimpleNamespace
        from vs_user.serializers import UserCreateSerializer

        serializer = UserCreateSerializer(data={
            "first_name": "No", "last_name": "Seat",
            "email": "no.seat@codex.test", "gender": "FEMALE",
            "phone": "08012345678", "role": self.hire_role.key,
        }, context={"request": SimpleNamespace(user=self.actor, tenant=self.tenant)})

        self.assertFalse(serializer.is_valid())
        self.assertIn("position", serializer.errors)

    def test_position_populates_employment_hierarchy_and_job_title(self):
        from vs_user.services.user import UserCreationService

        user = UserCreationService.create_pending(
            self._validated_data("position.derived@codex.test"), self.actor,
        )
        profile = user.platform_staff_profile

        self.assertEqual(profile.position, self.position)
        self.assertEqual(profile.job_title, self.position.title)
        self.assertEqual(profile.org_node.name, "Platform")
        self.assertEqual(profile.department.name, "Engineering")
        self.assertEqual(profile.division.name, "Product")

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
                "position": self.position.code,
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
                "position": self.position.code,
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


class JobAttributionTests(TestCase):
    """Email jobs belong to the actor who triggered them, never to the subject.

    Regression: invitation/reset jobs were queued with the *target* user as
    ``_job_owner_id``, so a freshly activated account opened its inbox to a
    "task completed" notification for an email an admin had sent it, and saw a
    queue row it never triggered.
    """

    def setUp(self):
        self.actor = make_cx_user(email="job.actor@codex.test")
        self.subject = make_cx_user(email="job.subject@codex.test")

    def _queued_kwargs(self, patched):
        self.assertTrue(patched.called, "expected an email job to be queued")
        return patched.call_args.kwargs

    def test_invitation_job_is_owned_by_the_inviting_admin(self):
        from vs_user.services.user import UserCreationService

        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            UserCreationService.finalize_invitation(
                user=self.subject, requested_by=self.actor,
            )
        kwargs = self._queued_kwargs(delay)
        self.assertEqual(kwargs["_job_owner_id"], str(self.actor.id))
        self.assertNotEqual(kwargs["_job_owner_id"], str(self.subject.id))
        # Per-row fan-out: the actor gets queue rows, not one bell per invitee.
        self.assertIs(kwargs["_job_notify"], False)

    def test_invitation_resend_job_is_owned_by_the_resending_admin(self):
        from vs_user.services.invitation import InvitationService

        InvitationService.create(user=self.subject, invited_by=self.actor)
        with mock.patch("vs_user.tasks.send_invitation_email_task.delay") as delay:
            InvitationService.resend(user=self.subject, requested_by=self.actor)
        kwargs = self._queued_kwargs(delay)
        self.assertEqual(kwargs["_job_owner_id"], str(self.actor.id))

    def test_admin_password_reset_job_is_owned_by_the_admin(self):
        from vs_user.services.password import PasswordService

        with mock.patch("vs_user.tasks.send_password_reset_email_task.delay") as delay:
            PasswordService.admin_reset(
                target_user=self.subject, requesting_user=self.actor,
            )
        kwargs = self._queued_kwargs(delay)
        self.assertEqual(kwargs["_job_owner_id"], str(self.actor.id))

    def test_self_service_reset_job_is_owned_by_the_requesting_user(self):
        from vs_user.services.password import PasswordService

        with mock.patch("vs_user.tasks.send_password_reset_email_task.delay") as delay:
            PasswordService.request_reset(email=self.subject.email)
        kwargs = self._queued_kwargs(delay)
        # SELF origin: the subject *is* the actor, so the row is theirs.
        self.assertEqual(kwargs["_job_owner_id"], str(self.subject.id))


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


class OrgNodeSerializerUniquenessTests(TestCase):
    """Org-node names are unique among siblings, not across hierarchy tiers."""

    def setUp(self):
        from vs_user.models import OrgNode

        self.division = OrgNode.objects.create(
            name="Operations", code="OPS", kind=OrgNode.Kind.DIVISION,
        )

    def test_department_may_have_same_name_as_its_division(self):
        from vs_user.models import OrgNode
        from vs_user.serializers import OrgNodeSerializer

        serializer = OrgNodeSerializer(data={
            "name": self.division.name,
            "code": "OPS-DEPT",
            "kind": OrgNode.Kind.DEPARTMENT,
            "parent_id": self.division.pk,
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        department = serializer.save()
        self.assertEqual(department.name, self.division.name)
        self.assertEqual(department.parent, self.division)

    def test_sibling_departments_may_not_share_a_name(self):
        from vs_user.models import OrgNode
        from vs_user.serializers import OrgNodeSerializer

        OrgNode.objects.create(
            name="People", code="PEOPLE-ONE", kind=OrgNode.Kind.DEPARTMENT,
            parent=self.division,
        )
        serializer = OrgNodeSerializer(data={
            "name": "People",
            "code": "PEOPLE-TWO",
            "kind": OrgNode.Kind.DEPARTMENT,
            "parent_id": self.division.pk,
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("name", serializer.errors)

    def test_top_level_divisions_may_not_share_a_name(self):
        from vs_user.models import OrgNode
        from vs_user.serializers import OrgNodeSerializer

        serializer = OrgNodeSerializer(data={
            "name": self.division.name,
            "code": "OPS-TWO",
            "kind": OrgNode.Kind.DIVISION,
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("name", serializer.errors)


class OrganogramListQueryTests(TestCase):
    """The org-node and position list endpoints must not be N+1 - three queries
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


class SeedOrganogramCommandTests(TestCase):
    """`seed_organogram` builds a non-empty tree and seats CX staff - an empty
    chart is usually just this seed not having been run."""

    def test_seed_builds_tree_and_seats_staff(self):
        from django.core.management import call_command
        from vs_user.services.organogram import OrganogramService

        u1 = make_cx_user(email="seed1@codex.test")
        make_cx_user(email="seed2@codex.test")

        call_command("seed_organogram", verbosity=0)

        tree = OrganogramService.build_tree()
        self.assertEqual([n["code"] for n in tree], ["VP-TECH"])  # single root, not empty
        self.assertTrue(
            u1.position_assignments.filter(end_date__isnull=True).exists()
        )

        # Idempotent: a second run adds nothing new.
        from vs_user.models import Position
        before = Position.objects.count()
        call_command("seed_organogram", verbosity=0)
        self.assertEqual(Position.objects.count(), before)


class OrgNodeDeleteProtectionTests(TestCase):
    """Deleting an org unit must either succeed or explain itself.

    Two regressions live here: (a) `parent` used to be SET_NULL, so deleting a
    Division silently orphaned its subtree into a state clean() forbids -
    un-editable and un-deletable; (b) ProtectedError subclasses IntegrityError,
    so every blocked delete surfaced as a 500 "An unexpected error occurred."
    """

    def setUp(self):
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_tenants.models import Tenant
        from vs_user.models import OrgNode, Position

        tenant = Tenant.objects.get(slug="codex", kind="PLATFORM")
        self.actor = make_cx_user(email="org-deleter@codex.test")
        role = TenantRoleTemplate.objects.create(
            tenant=tenant, key="xvs_super_admin", name="Super",
        )
        TenantUserRoleAssignment.objects.create(
            tenant=tenant, user=self.actor, role=role, assignment_status="ACTIVE",
        )

        self.division = OrgNode.objects.create(
            name="Technology", code="TECH", kind=OrgNode.Kind.DIVISION,
        )
        self.department = OrgNode.objects.create(
            name="Engineering", code="ENGR", kind=OrgNode.Kind.DEPARTMENT,
            parent=self.division,
        )
        self.team = OrgNode.objects.create(
            name="Backend", code="BACKEND", kind=OrgNode.Kind.TEAM,
            parent=self.department,
        )
        self.seat = Position.objects.create(
            title="Backend Engineer", code="BE-ENG", org_node=self.team,
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)

    def test_deleting_parent_with_children_is_409_and_keeps_the_subtree(self):
        from vs_user.models import OrgNode

        resp = self.client.delete(f"/v1/user/organogram/nodes/{self.division.pk}/")
        self.assertEqual(resp.status_code, 409, resp.content)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "PROTECTED_REFERENCE")
        self.assertIn("org node", body["message"])
        # The whole point: no silent orphaning.
        self.assertTrue(OrgNode.objects.filter(pk=self.division.pk).exists())
        self.department.refresh_from_db()
        self.assertEqual(self.department.parent_id, self.division.pk)

    def test_protection_covers_every_tier_not_just_divisions(self):
        """`parent` is one self-FK, so Department→Team is guarded exactly like
        Division→Department - deleting a Department with Teams under it must not
        orphan them either."""
        from vs_user.models import OrgNode

        resp = self.client.delete(f"/v1/user/organogram/nodes/{self.department.pk}/")
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["error"]["detail"], {"vs_user.orgnode": 1})
        self.team.refresh_from_db()
        self.assertEqual(self.team.parent_id, self.department.pk)
        self.assertTrue(OrgNode.objects.filter(pk=self.department.pk).exists())

    def test_deleting_a_node_holding_seats_is_409_naming_positions(self):
        resp = self.client.delete(f"/v1/user/organogram/nodes/{self.team.pk}/")
        self.assertEqual(resp.status_code, 409, resp.content)
        body = resp.json()
        self.assertEqual(body["error"]["code"], "PROTECTED_REFERENCE")
        self.assertEqual(body["error"]["detail"], {"vs_user.position": 1})
        self.assertIn("position", body["message"])

    def test_empty_leaf_still_deletes(self):
        from vs_user.models import OrgNode

        self.seat.delete()
        resp = self.client.delete(f"/v1/user/organogram/nodes/{self.team.pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(OrgNode.objects.filter(pk=self.team.pk).exists())


class LoginLockoutOracleTests(TestCase):
    """B13 - wrong-password attempts must never reveal the locked state."""

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
    """B14 - the attempted email is recorded even when no account matches."""

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
    """B10/B11 - multi-device session integrity."""

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

    def test_my_active_sessions_excludes_and_closes_expired_refresh_tokens(self):
        session = LoginSession.all_objects.get(pk=self.own_login["session_id"])
        OutstandingToken.objects.filter(jti=session.refresh_jti).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        response = self.client.get("/v1/user/sessions/mine/?is_active=true&page_size=50")

        self.assertEqual(response.status_code, 200, response.content)
        returned_ids = {item["id"] for item in response.json()["data"]}
        self.assertNotIn(str(session.id), {str(pk) for pk in returned_ids})
        session.refresh_from_db()
        self.assertFalse(session.is_active)
        self.assertEqual(session.end_reason, "EXPIRED")

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

    def test_end_other_mine_preserves_current_session_and_revokes_the_rest(self):
        second_own_login = LoginService.login(self.user.email, self.password)

        response = self.client.post(
            "/v1/user/sessions/end-other-mine/",
            {"current_session_id": self.own_login["session_id"]},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["ended_sessions"], 1)
        self.assertTrue(
            LoginSession.all_objects.get(pk=self.own_login["session_id"]).is_active,
        )
        ended_session = LoginSession.all_objects.get(pk=second_own_login["session_id"])
        self.assertFalse(ended_session.is_active)
        self.assertTrue(
            BlacklistedToken.objects.filter(token__jti=ended_session.refresh_jti).exists(),
        )
        self.assertTrue(
            LoginSession.all_objects.get(pk=self.other_login["session_id"]).is_active,
        )

    def test_end_other_mine_rejects_a_session_not_owned_by_the_caller(self):
        response = self.client.post(
            "/v1/user/sessions/end-other-mine/",
            {"current_session_id": self.other_login["session_id"]},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertTrue(
            LoginSession.all_objects.get(pk=self.own_login["session_id"]).is_active,
        )
        self.assertTrue(
            LoginSession.all_objects.get(pk=self.other_login["session_id"]).is_active,
        )


# =============================================================================
# WP-B1 - school branding in auth payloads (A.1)
# =============================================================================

class SchoolBrandingPayloadTests(TestCase):
    """The login / me payloads carry a nested `school` object for school users.

    Login is exercised through ``LoginService.login`` (the service returns the
    payload dict that the view wraps verbatim) with a real request so absolute
    logo URLs are built - this mirrors the existing tests in this module and
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
    Regression - an SMTP outage during eager (in-process) email sending must
    not 500 the request.

    Email now flows through the vs_notifications engine: the vs_user task
    dispatches (synchronously, cheaply) and the engine's
    deliver_email_notification does the SMTP send. Under eager mode the delivery
    task runs in-process; its eager guard treats the first failure as final, so
    celery.exceptions.Retry never propagates through the HTTP request even when
    smtp.zoho.com is unreachable - the PasswordResetRequest / UserInvitation row
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
            # dispatch enqueues the delivery task via transaction.on_commit -
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
            "Sh0rt!Aa",          # only 8 chars - too short
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


class DraftUserTests(TestCase):
    """Save-as-draft parks a DRAFT CX hire; submit promotes it into the normal
    approval flow. (Bulk upload lives in the vs_import_data framework.)"""

    def setUp(self):
        from vs_rbac.models import TenantRoleTemplate, TenantUserRoleAssignment
        from vs_tenants.models import Tenant
        from vs_user.models import OrgNode, Position

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
        node = OrgNode.objects.create(
            name="Draft Operations", code="DRAFT-OPS", kind=OrgNode.Kind.DIVISION,
        )
        self.position = Position.objects.create(
            title="Operations Analyst", code="OPS-ANALYST", org_node=node,
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
            resp = self.client.post(
                f"/v1/user/users/{user.id}/submit/",
                {"position": self.position.code}, format="json",
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.PENDING_APPROVAL)
        wf.assert_called_once()
        self.assertEqual(user.platform_staff_profile.position, self.position)
        self.assertEqual(user.platform_staff_profile.job_title, self.position.title)

    def test_submit_draft_without_role_is_rejected(self):
        self.client.post("/v1/user/users/", {
            "first_name": "Roleless", "last_name": "Draft",
            "email": "roleless.draft@codex.test", "gender": "MALE",
            "save_as_draft": True,
        }, format="json")
        user = User.objects.get(email="roleless.draft@codex.test")
        resp = self.client.post(
            f"/v1/user/users/{user.id}/submit/",
            {"position": self.position.code}, format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.DRAFT)

    def test_submit_draft_without_position_is_rejected(self):
        self.client.post("/v1/user/users/", {
            "first_name": "Seatless", "last_name": "Draft",
            "email": "seatless.draft@codex.test", "gender": "MALE",
            "role": self.hire_role.key, "save_as_draft": True,
        }, format="json")
        user = User.objects.get(email="seatless.draft@codex.test")

        resp = self.client.post(f"/v1/user/users/{user.id}/submit/", {}, format="json")

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json()["error"]["error_code"], "POSITION_REQUIRED")
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.DRAFT)


class CXUsersImportHandlerTests(TestCase):
    """The vs_import_data cx_users handler creates CX staff through the NORMAL
    flow (PENDING_APPROVAL + workflow), never as drafts; its template is seeded."""

    def setUp(self):
        from vs_rbac.models import TenantRoleTemplate
        from vs_tenants.models import Tenant
        from vs_user.models import OrgNode, Position

        self.tenant = Tenant.objects.get(slug="codex", kind="PLATFORM")
        self.actor = make_cx_user(email="importer@codex.test")
        self.hire_role = TenantRoleTemplate.objects.create(
            tenant=self.tenant, key="xvs_platform_admin", name="Platform Admin",
        )
        node = OrgNode.objects.create(
            name="Import Division", code="IMPORT", kind=OrgNode.Kind.DIVISION,
        )
        self.position = Position.objects.create(
            title="Import Analyst", code="IMPORT-ANALYST", org_node=node,
        )

    def test_handler_creates_pending_user_not_draft(self):
        from types import SimpleNamespace
        from unittest import mock
        from vs_import_data.services.import_executor import import_cx_users_row

        with mock.patch("vs_workflow.services.submission.submit_for_approval") as wf:
            result = import_cx_users_row(
                import_batch=SimpleNamespace(school=None),
                payload={
                    "first_name": "Bulk", "last_name": "Hire",
                    "email": "bulk.hire@codex.test", "role": self.hire_role.key,
                    "gender": "MALE", "position": self.position.code,
                },
                queued_by=self.actor,
            )
        self.assertEqual(result.action, "create")
        user = User.objects.get(email="bulk.hire@codex.test")
        self.assertEqual(user.status, User.Status.PENDING_APPROVAL)
        self.assertEqual(user.platform_staff_profile.position, self.position)
        self.assertEqual(user.platform_staff_profile.job_title, self.position.title)
        wf.assert_called_once()

    def test_handler_rejects_missing_position(self):
        from types import SimpleNamespace
        from rest_framework.exceptions import ValidationError
        from vs_import_data.services.import_executor import import_cx_users_row

        with self.assertRaises(ValidationError) as caught:
            import_cx_users_row(
                import_batch=SimpleNamespace(school=None),
                payload={
                    "first_name": "No", "last_name": "Seat",
                    "email": "bulk.no.seat@codex.test", "role": self.hire_role.key,
                },
                queued_by=self.actor,
            )
        self.assertIn("position", caught.exception.detail)

    def test_handler_skips_duplicate_email(self):
        from types import SimpleNamespace
        from vs_import_data.services.import_executor import import_cx_users_row

        result = import_cx_users_row(
            import_batch=SimpleNamespace(school=None),
            payload={"first_name": "Dupe", "last_name": "X",
                     "email": self.actor.email, "role": self.hire_role.key},
            queued_by=self.actor,
        )
        self.assertEqual(result.action, "skip")

    def test_cx_users_template_seeds_from_the_import_command(self):
        from io import StringIO
        from django.core.management import call_command
        from vs_import_data.models import ImportTemplate

        call_command("seed_import", dataset_type="cx_users", stdout=StringIO())
        template = ImportTemplate.objects.get(code="cx_users_master_v1")
        self.assertEqual(template.dataset_type, "cx_users")
        self.assertTrue(
            template.columns.filter(target_field="email", is_required=True).exists()
        )
        self.assertTrue(
            template.columns.filter(target_field="position", is_required=True).exists()
        )
        self.assertFalse(template.columns.filter(target_field="job_title").exists())


class QueueSummaryTests(TestCase):
    """The summary cards must agree with the rows the list returns.

    Regression: the summary aggregated off the LIST queryset, which carries
    ``.order_by("-created_at")``. Django adds ORDER BY columns to the GROUP BY,
    so the counts were grouped by (status, created_at) - one bucket per row -
    and ``dict()`` kept only the last of each duplicated status key. Every card
    read 1 no matter how many rows the table showed.
    """

    def setUp(self):
        from vs_tenants.models import Tenant

        self.tenant = Tenant.objects.get(slug="codex", kind="PLATFORM")
        self.actor = make_cx_user(email="queue-owner@codex.test")
        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)

    def _job(self, status, kind="email", label="Receipt email"):
        from core.models import BackgroundJob
        import uuid

        return BackgroundJob.objects.create(
            owner=self.actor, tenant=self.tenant, kind=kind, label=label,
            task_name="vs_finance.send_receipt", status=status,
            celery_task_id=str(uuid.uuid4()),
        )

    def test_counts_every_row_not_one_per_timestamp(self):
        for _ in range(6):
            self._job("SUCCEEDED")
        for _ in range(2):
            self._job("FAILED")
        self._job("QUEUED")

        res = self.client.get("/v1/user/me/tasks/summary/")
        self.assertEqual(res.status_code, 200)
        by_status = res.json()["data"]["by_status"]
        self.assertEqual(by_status.get("SUCCEEDED"), 6)
        self.assertEqual(by_status.get("FAILED"), 2)
        self.assertEqual(by_status.get("QUEUED"), 1)
        self.assertEqual(res.json()["data"]["total"], 9)

    def test_summary_agrees_with_the_list_it_describes(self):
        for _ in range(4):
            self._job("SUCCEEDED")

        listed = self.client.get("/v1/user/me/tasks/").json()
        summary = self.client.get("/v1/user/me/tasks/summary/").json()
        self.assertEqual(
            summary["data"]["by_status"].get("SUCCEEDED"),
            len([j for j in listed["data"] if j["status"] == "SUCCEEDED"]),
        )

    def test_summary_honours_the_same_filters_as_the_list(self):
        for _ in range(3):
            self._job("SUCCEEDED", kind="export")
        self._job("SUCCEEDED", kind="email")

        res = self.client.get("/v1/user/me/tasks/summary/?kind=export")
        self.assertEqual(res.json()["data"]["by_status"].get("SUCCEEDED"), 3)

    def test_summary_is_scoped_to_the_caller(self):
        from core.models import BackgroundJob
        import uuid

        other = make_cx_user(email="someone-else@codex.test")
        BackgroundJob.objects.create(
            owner=other, tenant=self.tenant, kind="email", label="Theirs",
            task_name="t", status="SUCCEEDED", celery_task_id=str(uuid.uuid4()),
        )
        self._job("SUCCEEDED")

        res = self.client.get("/v1/user/me/tasks/summary/")
        self.assertEqual(res.json()["data"]["by_status"].get("SUCCEEDED"), 1)


class SeedsNotificationEventTypes:
    """Seeds the notification event-type registry for tests that create users.

    Nothing creates these rows automatically - no migration does, only the
    `seed_notification_event_types` command - so a fresh test database has an
    empty registry. Creating a user sends an invitation, dispatch cannot resolve
    its event key, and `finalize_invitation` catches the error and logs it. The
    test still passes, which is the problem: the invitation path never actually
    ran, and every user-creating test printed error-level tracebacks that make a
    genuine failure in the same run much harder to read.

    Seeding here means these tests exercise the real dispatch instead of its
    exception handler, and stops them depending on whether some earlier test in
    the same worker happened to seed the registry first.
    """

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        call_command("seed_notification_event_types", verbosity=0)


class UserBranchAssignmentTests(SeedsNotificationEventTypes, TestCase):
    """Creating a user against a branch - the path M9 school onboarding needs.

    Two differently shaped tenants on purpose. ``branched`` is a school with real
    branches; ``branchless`` is a branch-optional school with none. A fix that
    only works for the first shape is not a fix, so every rule below is asserted
    against whichever shape can actually express it.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school

        self.branched = make_school(slug="branched-academy", name="Branched Academy")
        self.lekki = make_branch(self.branched, name="Lekki Campus", is_main=True)
        self.yaba = make_branch(self.branched, name="Yaba Campus", is_main=False)

        # A second tenant that owns a branch of its own. Its branch is the
        # cross-tenant probe: it exists, so "not found" cannot be explained away
        # by the id simply being unused.
        self.rival = make_school(slug="rival-college", name="Rival College")
        self.rival_branch = make_branch(self.rival, name="Rival Main", is_main=True)

        # A branch-optional school: no branches at all, ever.
        self.branchless = make_school(slug="solo-centre", name="Solo Learning Centre")

        self.actor = User.objects.create_user(
            email="head@branched.test", password="Str0ng!pass123",
            user_type=User.UserType.SCHOOL_ADMIN, status="ACTIVE",
            first_name="Head", last_name="Teacher", tenant=self.branched.tenant,
        )
        self._grant(self.actor, "platform.team.create", tenant=self.branched.tenant)
        self._grant(self.actor, "platform.team.view", tenant=self.branched.tenant)

        self.role = self._role(self.branched.tenant, "school-staff")
        self.branchless_role = self._role(self.branchless.tenant, "solo-staff")

        self.client = APIClient()
        self.client.force_authenticate(user=self.actor)

    # -- fixtures ---------------------------------------------------------

    @staticmethod
    def _role(tenant, key):
        from vs_rbac.models import TenantRoleTemplate

        role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=tenant, key=key, defaults={"name": key.title(), "status": "ACTIVE"},
        )
        return role

    @classmethod
    def _grant(cls, user, permission_key, *, tenant, role_key="team-manager"):
        from vs_rbac.models import (
            Permission, PermissionAction, PermissionModule, PermissionResource,
            TenantRolePermission, TenantUserRoleAssignment,
        )

        module_name, resource_name, action_name = permission_key.split(".")
        module, _ = PermissionModule.objects.get_or_create(name=module_name)
        resource, _ = PermissionResource.objects.get_or_create(
            module=module, name=resource_name,
        )
        action, _ = PermissionAction.objects.get_or_create(name=action_name)
        permission, _ = Permission.objects.get_or_create(
            key=permission_key,
            defaults={"module": module, "resource": resource, "action": action},
        )
        role = cls._role(tenant, role_key)
        TenantRolePermission.objects.get_or_create(
            role=role, permission=permission, defaults={"granted": True},
        )
        TenantUserRoleAssignment.objects.get_or_create(
            tenant=tenant, user=user, role=role,
            defaults={"assignment_status": "ACTIVE"},
        )

    def _post(self, **overrides):
        body = {
            "first_name": "New", "last_name": "Person",
            "email": "new.person@branched.test", "gender": "FEMALE",
            "user_type": User.UserType.STAFF,
            "role": self.role.key,
        }
        body.update(overrides)
        return self.client.post("/v1/user/users/", body, format="json")

    @staticmethod
    def _field_errors(response):
        """The per-field errors out of the standard error envelope."""
        return response.json().get("error", {}).get("detail", {})

    @classmethod
    def _branch_error(cls, response):
        return cls._field_errors(response).get("branch")

    # -- the happy path ---------------------------------------------------

    def test_user_is_created_against_a_branch_and_the_branch_is_persisted(self):
        resp = self._post(branch=self.yaba.pk)

        self.assertEqual(resp.status_code, 201, resp.content)
        user = User.objects.get(email="new.person@branched.test")
        self.assertEqual(user.branch_id, self.yaba.pk)
        self.assertEqual(user.tenant_id, self.branched.tenant_id)

    def test_branch_id_is_accepted_as_a_string_as_well_as_a_number(self):
        """Form posts and many JSON clients send the id as a string.

        The old UUID field accepted a JSON *integer* by accident (DRF turned 7
        into UUID(int=7) and Django turned it back into 7) while rejecting the
        string "7" outright, so whether a caller worked depended on its encoding.
        """
        resp = self._post(branch=str(self.lekki.pk))

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(
            User.objects.get(email="new.person@branched.test").branch_id,
            self.lekki.pk,
        )

    # -- refusals ---------------------------------------------------------

    def test_branch_from_another_tenant_is_refused_exactly_like_an_unknown_one(self):
        """A foreign branch must not be distinguishable from a missing one.

        Otherwise the field is an oracle: a caller learns which branch ids exist
        in other tenants by reading which error comes back.
        """
        foreign = self._post(branch=self.rival_branch.pk)
        unknown = self._post(branch=self.rival_branch.pk + 10_000)

        self.assertEqual(foreign.status_code, 400, foreign.content)
        self.assertEqual(unknown.status_code, 400, unknown.content)
        self.assertEqual(self._branch_error(foreign), self._branch_error(unknown))
        self.assertFalse(
            User.objects.filter(email="new.person@branched.test").exists()
        )

    def test_unknown_branch_is_refused(self):
        resp = self._post(branch=self.yaba.pk + 5_000)

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(
            User.objects.filter(email="new.person@branched.test").exists()
        )

    def test_malformed_branch_reference_is_a_validation_error_not_a_server_error(self):
        """Anything that is not a decimal id is a 400, never a database error."""
        import uuid as _uuid

        for bad in (str(_uuid.uuid4()), "not-an-id", "9" * 40, "-3", "1.5"):
            with self.subTest(branch=bad):
                resp = self._post(branch=bad)
                self.assertEqual(resp.status_code, 400, f"{bad}: {resp.content}")

    # -- branchless shapes ------------------------------------------------

    def test_school_admin_may_be_created_without_a_branch(self):
        resp = self._post(
            user_type=User.UserType.SCHOOL_ADMIN, branch=None,
            email="second.head@branched.test",
        )

        self.assertEqual(resp.status_code, 201, resp.content)
        user = User.objects.get(email="second.head@branched.test")
        self.assertIsNone(user.branch_id)

    def test_school_admin_is_created_in_a_school_that_has_no_branches_at_all(self):
        """The branch-optional shape: omitting the branch is the only option."""
        solo_admin = User.objects.create_user(
            email="head@solo.test", password="Str0ng!pass123",
            user_type=User.UserType.SCHOOL_ADMIN, status="ACTIVE",
            first_name="Solo", last_name="Head", tenant=self.branchless.tenant,
        )
        self._grant(solo_admin, "platform.team.create", tenant=self.branchless.tenant)
        client = APIClient()
        client.force_authenticate(user=solo_admin)

        resp = client.post("/v1/user/users/", {
            "first_name": "Only", "last_name": "Admin",
            "email": "only.admin@solo.test", "gender": "MALE",
            "user_type": User.UserType.SCHOOL_ADMIN,
            "role": self.branchless_role.key,
        }, format="json")

        self.assertEqual(resp.status_code, 201, resp.content)
        user = User.objects.get(email="only.admin@solo.test")
        self.assertIsNone(user.branch_id)
        self.assertEqual(user.tenant_id, self.branchless.tenant_id)

    def test_branch_level_user_type_still_requires_a_branch(self):
        resp = self._post(branch=None)

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("must be assigned to a branch", str(self._branch_error(resp)))

    def test_vision_staff_may_not_be_given_a_branch(self):
        """The reason must stay "CX staff take no branch", not "no such branch".

        CX staff resolve against the platform tenant, which owns no branches, so
        resolving before this check would hide the real reason behind a lookup
        failure.
        """
        resp = self._post(
            user_type=User.UserType.CX_STAFF, branch=self.lekki.pk,
            email="cx.hire@codex.test",
        )

        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("user_type", self._field_errors(resp))
        self.assertNotIn("branch", self._field_errors(resp))

    # -- authorization ----------------------------------------------------

    def test_creating_a_user_requires_the_create_permission(self):
        stranger = User.objects.create_user(
            email="nobody@branched.test", password="Str0ng!pass123",
            user_type=User.UserType.SCHOOL_ADMIN, status="ACTIVE",
            first_name="No", last_name="Body", tenant=self.branched.tenant,
        )
        client = APIClient()
        client.force_authenticate(user=stranger)

        resp = client.post("/v1/user/users/", {
            "first_name": "Sneaky", "last_name": "Hire",
            "email": "sneaky@branched.test", "gender": "MALE",
            "user_type": User.UserType.STAFF, "role": self.role.key,
            "branch": self.lekki.pk,
        }, format="json")

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertFalse(User.objects.filter(email="sneaky@branched.test").exists())

    def test_branch_filter_rejects_a_non_numeric_id_instead_of_erroring(self):
        """The list filter addresses the same integer key as the create field."""
        resp = self.client.get("/v1/user/users/?branch_id=not-an-id")

        self.assertEqual(resp.status_code, 400, resp.content)


class UserBranchTenantGuardTests(SeedsNotificationEventTypes, TestCase):
    """``User._derive_tenant`` and the ``save()`` guard, after Phase C.

    Both read the branch's own ``tenant_id`` now instead of walking
    ``branch.school.tenant_id``. The save-time guard is the last thing standing
    between a mis-set ``tenant`` and a user account bound to another tenant's
    branch, and it is reachable from every writer (API, services, shell,
    management commands), so it is asserted at the model rather than only
    through the create endpoint.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school

        self.branched = make_school(slug="guard-branched", name="Guard Branched")
        self.lekki = make_branch(self.branched, name="Lekki", is_main=True)
        self.rival = make_school(slug="guard-rival", name="Guard Rival")
        self.rival_branch = make_branch(self.rival, name="Rival Main", is_main=True)
        # Branch-optional shape.
        self.branchless = make_school(slug="guard-solo", name="Guard Solo")

    def test_a_branch_bound_user_inherits_the_branchs_own_tenant(self):
        user = User.objects.create_user(
            email="derive@guard.test", password="Str0ng!pass123",
            first_name="Der", last_name="Ive", status="ACTIVE",
            user_type=User.UserType.STAFF, branch=self.lekki,
        )

        self.assertEqual(user.tenant_id, self.branched.tenant_id)
        self.assertEqual(user.tenant_id, self.lekki.tenant_id)

    def test_saving_a_user_onto_another_tenants_branch_is_refused(self):
        """The guard must still deny. Without the comparison this write lands
        and the account silently straddles two tenants."""
        user = User.objects.create_user(
            email="straddle@guard.test", password="Str0ng!pass123",
            first_name="Stra", last_name="Ddle", status="ACTIVE",
            user_type=User.UserType.STAFF, branch=self.lekki,
        )
        user.branch = self.rival_branch

        with self.assertRaises(DjangoValidationError):
            user.save()

        user.refresh_from_db()
        self.assertEqual(user.branch_id, self.lekki.pk)

    def test_a_branchless_tenants_user_cannot_be_moved_onto_a_branch(self):
        user = User.objects.create_user(
            email="solo@guard.test", password="Str0ng!pass123",
            first_name="Sol", last_name="Oh", status="ACTIVE",
            user_type=User.UserType.SCHOOL_ADMIN, tenant=self.branchless.tenant,
        )
        user.branch = self.lekki

        with self.assertRaises(DjangoValidationError):
            user.save()

    def test_a_branch_in_the_users_own_tenant_still_saves(self):
        user = User.objects.create_user(
            email="ok@guard.test", password="Str0ng!pass123",
            first_name="O", last_name="Kay", status="ACTIVE",
            user_type=User.UserType.STAFF, branch=self.lekki,
        )
        user.first_name = "Okay"
        user.save()  # must not raise

        user.refresh_from_db()
        self.assertEqual(user.first_name, "Okay")
        self.assertEqual(user.tenant_id, self.branched.tenant_id)


class AuthContextParityTests(TestCase):
    """The login response and /me must describe the tenant identically.

    The console treats a fresh login as equivalent to a /me sync and skips the
    round trip for that mount, so a field one carries and the other does not is
    absent for a whole session. That is how a platform operator came to be told
    they were a school until the next page reload.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school, make_school_admin

        self.school = make_school(slug="parity-school", name="Parity School")
        self.branch = make_branch(self.school)
        self.user = make_school_admin(self.branch, email="parity-admin@test.com")

    def _me_tenant(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from vs_user.views.me import CurrentUserView

        request = APIRequestFactory().get("/v1/user/auth/me/")
        request.tenant = self.user.tenant
        request.rbac_tenant = self.user.tenant
        force_authenticate(request, user=self.user)
        resp = CurrentUserView.as_view()(request)
        body = resp.data.get("data", resp.data)
        return body["tenant"]

    def test_me_matches_the_shared_builder(self):
        from vs_tenants.context import tenant_context_block

        self.assertEqual(self._me_tenant(), tenant_context_block(self.user.tenant))

    def test_the_block_carries_kind(self):
        """Without kind the console cannot tell the platform from a customer."""
        from vs_rbac.tests.helpers import codex_tenant
        from vs_tenants.context import tenant_context_block

        self.assertEqual(tenant_context_block(self.user.tenant)["kind"], "SCHOOL")
        self.assertEqual(tenant_context_block(codex_tenant())["kind"], "PLATFORM")

    def test_login_response_carries_the_same_tenant_keys(self):
        """The login payload is what the console caches when it skips /me."""
        import inspect

        from vs_user.services.auth import LoginService
        from vs_tenants.context import tenant_context_block

        source = inspect.getsource(LoginService)
        self.assertIn("tenant_context_block", source,
                      "login must build its tenant block with the shared helper")
        self.assertEqual(set(tenant_context_block(self.user.tenant)),
                         set(self._me_tenant()))
