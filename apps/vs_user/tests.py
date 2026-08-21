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
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, tag
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
            PasswordService.request_reset(
                email=self.subject.email, tenant=self.subject.tenant.slug,
            )
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

    def test_cx_and_school_rows_are_told_apart_by_tenant_kind(self):
        """The list used to carry a ``user_type`` column, and a
        ``?user_type=CX_STAFF`` filter beside it. Both are gone. The split the
        console's two tabs actually draw is by tenant kind, which is what
        ``?scope=school`` filters on and what each row now reports."""
        from vs_user.serializers import UserListSerializer

        rows = {
            row["email"]: row["tenant_kind"]
            for row in UserListSerializer(self._queryset_for(""), many=True).data
        }
        self.assertEqual(rows["scope-cx@codex.test"], "PLATFORM")
        self.assertEqual(rows["scope-admin@school.test"], "SCHOOL")

    def test_school_scope_excludes_cx_staff(self):
        users = self._queryset_for("?scope=school")
        self.assertQuerySetEqual(users, [self.school_user], transform=lambda user: user)

    def test_platform_scope_returns_only_cx_staff(self):
        """The positive half of the same split.

        Only ``scope=school`` existed, so the console could exclude platform
        staff but never ask for them: its CX tabs fell back to an unfiltered
        list, which is every user on the platform. The pickers built on those
        tabs then offered school users - including the one that transfers
        platform super-admin.
        """
        users = self._queryset_for("?scope=platform")
        self.assertQuerySetEqual(users, [self.cx_user], transform=lambda user: user)

    def test_the_two_scopes_partition_the_list(self):
        """Neither half may drop a row or claim one twice."""
        everyone = set(self._queryset_for("").values_list("pk", flat=True))
        school = set(self._queryset_for("?scope=school").values_list("pk", flat=True))
        platform = set(self._queryset_for("?scope=platform").values_list("pk", flat=True))

        self.assertEqual(school | platform, everyone)
        self.assertEqual(school & platform, set())

    def test_an_unknown_scope_does_not_silently_filter(self):
        """``?user_type=CX_STAFF`` is now ignored rather than honoured, so a
        stale caller gets everything rather than nothing. Worth pinning: the
        dangerous failure would be an unrecognised value quietly narrowing the
        list and a reviewer trusting it."""
        everyone = set(self._queryset_for("").values_list("pk", flat=True))
        stale = set(self._queryset_for("?user_type=CX_STAFF").values_list("pk", flat=True))
        self.assertEqual(stale, everyone)

    def test_school_scope_serializes_placement_and_active_role(self):
        from vs_user.serializers import UserListSerializer

        user = self._queryset_for("?scope=school").get(pk=self.school_user.pk)
        data = UserListSerializer(user).data

        self.assertEqual(data["school_name"], "Scope School")
        self.assertEqual(data["role"], "School Administrator")


def platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002."""
    from vs_tenants.models import Tenant

    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


def make_cx_user(email="staff@codex.test", password="Str0ng!pass123"):
    # The tenant is named, not derived. Being CX staff IS being on the platform
    # tenant; there is no persona column left to stand in for it.
    return User.objects.create_user(
        email=email,
        password=password,
        tenant=platform_tenant(),
        status="ACTIVE",
        first_name="Code",
        last_name="Xer",
    )


def _run_user_create_serializer(*, email, actor=None, tenant=None, **extra):
    """Validate a CX-staff create the way the endpoint does, and return attrs.

    Exists because the per-tenant email check has to run where the target
    tenant is known - ``validate()`` - so a test that pokes ``validate_email``
    directly proves nothing about the rule any more.
    """
    from types import SimpleNamespace
    from vs_user.serializers import UserCreateSerializer

    actor = actor or make_cx_user(email="hiring.manager@codex.test")
    data = {
        "first_name": "Ada",
        "last_name": "Okoye",
        "email": email,
    }
    data.update(extra)
    serializer = UserCreateSerializer(
        data=data,
        context={
            "request": SimpleNamespace(user=actor, tenant=tenant or actor.tenant),
            "draft": True,
        },
    )
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _main_branch(school):
    """The main branch every school is created with."""
    from vs_tenants.models import Branch

    branch = Branch.all_objects.filter(tenant=school.tenant, is_main=True).first()
    if branch is None:
        branch = Branch.objects.create(
            tenant=school.tenant, name="Main Branch", is_main=True, status="ACTIVE",
        )
    return branch


def _seed_prebuilt_admin_roles():
    """Seed the two prebuilt role templates school creation provisions from.

    ``provision_admin_user`` refuses to mint an administrator without a role,
    so a school created in a test database that has never run
    ``seed_platform_permissions`` would silently produce no admin at all.
    """
    from vs_rbac.models import PrebuiltRoleTemplate

    for key, name, scope in (
        ("school_admin", "School Admin", "institution"),
        ("branch_admin", "Branch Admin", "branch"),
    ):
        PrebuiltRoleTemplate.objects.get_or_create(
            key=key, defaults={"name": name, "scope": scope},
        )


def make_school(name="Caleb International College", slug="caleb"):
    from schools.vs_schools.models import School
    return School.objects.create(name=name, slug=slug, status="ACTIVE")


def make_school_admin(school, email="admin@caleb.test", password="Str0ng!pass123"):
    return User.objects.create_user(
        email=email,
        password=password,
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
            LoginService.login(
                self.user.email, "wrong-password", tenant=self.user.tenant.slug,
            )
        self.assertEqual(ctx.exception.args[0]["code"], "INVALID_CREDENTIALS")

    def test_correct_password_on_locked_account_reveals_lock(self):
        self._lock()
        with self.assertRaises(ValueError) as ctx:
            LoginService.login(
                self.user.email, self.password, tenant=self.user.tenant.slug,
            )
        self.assertEqual(ctx.exception.args[0]["code"], "ACCOUNT_LOCKED")

    def test_successful_login_returns_tokens(self):
        result = LoginService.login(
            self.user.email, self.password, tenant=self.user.tenant.slug,
        )
        self.assertIn("access", result)
        self.assertIn("refresh", result)
        self.assertTrue(
            LoginSession.objects.filter(user=self.user, is_active=True).exists()
        )


class FailedAttemptAuditTests(TestCase):
    """B14 - the attempted email is recorded even when no account matches."""

    def test_unknown_email_is_recorded_as_entered(self):
        with self.assertRaises(ValueError):
            LoginService.login(
                "ghost@nowhere.test", "whatever", tenant=platform_tenant().slug,
            )
        attempt = AuthAttempt.objects.latest("id")
        self.assertEqual(attempt.email_entered, "ghost@nowhere.test")
        self.assertEqual(attempt.result, AuthAttempt.Result.FAIL)

    def test_known_email_wrong_password_recorded(self):
        user = make_cx_user()
        with self.assertRaises(ValueError):
            LoginService.login(
                user.email, "wrong-password", tenant=user.tenant.slug,
            )
        attempt = AuthAttempt.objects.latest("id")
        self.assertEqual(attempt.email_entered, user.email)


class SessionScopedLogoutTests(TestCase):
    """B10/B11 - multi-device session integrity."""

    def setUp(self):
        self.password = "Str0ng!pass123"
        self.user = make_cx_user(password=self.password)
        self.device_a = LoginService.login(
            self.user.email, self.password, tenant=self.user.tenant.slug,
        )
        self.device_b = LoginService.login(
            self.user.email, self.password, tenant=self.user.tenant.slug,
        )
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
        self.own_login = LoginService.login(
            self.user.email, self.password, tenant=self.user.tenant.slug,
        )
        self.other_login = LoginService.login(
            self.other.email, self.password, tenant=self.other.tenant.slug,
        )
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
        second_own_login = LoginService.login(
            self.user.email, self.password, tenant=self.user.tenant.slug,
        )

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
        return LoginService.login(
            user.email, password, tenant=user.tenant.slug, request=request,
        )

    def _me(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get("/v1/user/auth/me/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["data"]

    def _add_branding(self, logo=True):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from schools.vs_schools.models import SchoolBranding
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
        from vs_notifications.services.seed import seed_notification_templates

        # The event registry arrives with the database (vs_notifications 0008).
        # The DB templates do not, so the engine has nothing to render without this.
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
            resp = self.client.post(
                self.RESET_URL,
                {"email": user.email, "tenant": user.tenant.slug},
                format="json",
            )

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
            resp = self.client.post(
                self.RESET_URL,
                {"email": user.email, "tenant": user.tenant.slug},
                format="json",
            )

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
        # Event types come from vs_notifications 0008; only the templates need seeding.
        from vs_notifications.services.seed import seed_notification_templates

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


class UserBranchAssignmentTests(TestCase):
    """Creating a user against a branch - the path M9 school onboarding needs.

    Two differently shaped tenants on purpose. ``branched`` is a school with real
    branches; ``branchless`` is a branch-optional school with none. A fix that
    only works for the first shape is not a fix, so every rule below is asserted
    against whichever shape can actually express it.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school

        self.branched = make_school(slug="branched-academy", name="Branched Academy")
        self.lekki = make_branch(self.branched, name="Lekki Branch", is_main=True)
        self.yaba = make_branch(self.branched, name="Yaba Branch", is_main=False)

        # A second tenant that owns a branch of its own. Its branch is the
        # cross-tenant probe: it exists, so "not found" cannot be explained away
        # by the id simply being unused.
        self.rival = make_school(slug="rival-college", name="Rival College")
        self.rival_branch = make_branch(self.rival, name="Rival Main", is_main=True)

        # A branch-optional school: no branches at all, ever.
        self.branchless = make_school(slug="solo-centre", name="Solo Learning Centre")

        self.actor = User.objects.create_user(
            email="head@branched.test", password="Str0ng!pass123",
            status="ACTIVE",
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
        from vs_rbac.tests.helpers import scope_for_key
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
            defaults={
                "module": module, "resource": resource, "action": action,
                "scope": scope_for_key(permission_key),
            },
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

    def test_a_tenant_user_may_be_created_without_a_branch(self):
        """A null branch is a school-wide posting, and a legal one.

        Corona Secondary School has three branches and one bursar who works
        across all of them. She is STAFF with no branch, exactly like the
        principal, and the difference between them is their role.
        """
        resp = self._post(
            branch=None,
            email="second.head@branched.test",
        )

        self.assertEqual(resp.status_code, 201, resp.content)
        user = User.objects.get(email="second.head@branched.test")
        self.assertIsNone(user.branch_id)

    def test_a_school_wide_user_sees_every_branch_in_the_tenant(self):
        """"School-wide" has to mean something, not merely be permitted.

        The bursar with no branch must see Lekki and Yaba both; a bursar posted
        to Lekki must see only Lekki. If the null simply meant "unset" the two
        would be indistinguishable downstream.
        """
        from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

        school_wide = User.objects.create_user(
            email="bursar@branched.test", password="Str0ng!pass123",
            status="ACTIVE",
            first_name="Whole", last_name="School", tenant=self.branched.tenant,
        )
        pinned = User.objects.create_user(
            email="lekki.bursar@branched.test", password="Str0ng!pass123",
            status="ACTIVE",
            first_name="Just", last_name="Lekki", branch=self.lekki,
        )

        self.assertIs(
            visible_branch_ids(school_wide, self.branched.tenant), WHOLE_TENANT,
        )
        self.assertEqual(
            visible_branch_ids(pinned, self.branched.tenant),
            frozenset({self.lekki.pk}),
        )

    def test_a_user_is_created_in_a_school_that_has_no_branches_at_all(self):
        """The branch-optional shape: omitting the branch is the only option."""
        solo_admin = User.objects.create_user(
            email="head@solo.test", password="Str0ng!pass123",
            status="ACTIVE",
            first_name="Solo", last_name="Head", tenant=self.branchless.tenant,
        )
        self._grant(solo_admin, "platform.team.create", tenant=self.branchless.tenant)
        client = APIClient()
        client.force_authenticate(user=solo_admin)

        resp = client.post("/v1/user/users/", {
            "first_name": "Only", "last_name": "Admin",
            "email": "only.admin@solo.test", "gender": "MALE",
            "role": self.branchless_role.key,
        }, format="json")

        self.assertEqual(resp.status_code, 201, resp.content)
        user = User.objects.get(email="only.admin@solo.test")
        self.assertIsNone(user.branch_id)
        self.assertEqual(user.tenant_id, self.branchless.tenant_id)

    def test_a_student_may_also_be_created_without_a_branch(self):
        """No tenant persona is branch-compulsory any more.

        The rule that made SCHOOL_ADMIN the one branch-optional persona is gone
        from both the database and clean(), and this asserts they agree: if the
        constraint had been left in place while the Python check was removed,
        this would come back a 500 from the database rather than a 201.
        """
        resp = self._post(
            branch=None,
            email="new.pupil@branched.test",
        )

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(
            User.objects.get(email="new.pupil@branched.test").branch_id
        )

    def test_a_tenant_user_may_still_be_created_with_a_branch(self):
        """The branch did not become decorative: it is stored and it narrows."""
        resp = self._post(branch=self.yaba.pk, email="pinned@branched.test")

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(
            User.objects.get(email="pinned@branched.test").branch_id, self.yaba.pk,
        )

    def test_platform_staff_may_not_be_given_a_branch(self):
        """The reason must stay "platform staff take no branch", not "no such
        branch".

        A platform hire resolves against the platform tenant, which owns no
        branches, so resolving before this check would hide the real reason
        behind a lookup failure.

        The actor is what says a platform hire is being made. It used to be a
        ``user_type=CX_STAFF`` in the body, which a school administrator could
        type - and which is why this test used to be able to run its request
        through a school actor at all.
        """
        from vs_rbac.models import TenantRoleTemplate

        cx_actor = make_cx_user(email="cx.hiring@codex.test")
        self._grant(cx_actor, "platform.team.create", tenant=cx_actor.tenant)
        cx_role = TenantRoleTemplate.objects.create(
            tenant=cx_actor.tenant, key="cx-engineer", name="CX-Engineer",
        )
        client = APIClient()
        client.force_authenticate(user=cx_actor)

        resp = client.post("/v1/user/users/", {
            "first_name": "New", "last_name": "Person",
            "email": "cx.hire@codex.test", "gender": "FEMALE",
            "role": cx_role.key, "branch": self.lekki.pk,
        }, format="json")

        self.assertEqual(resp.status_code, 400, resp.content)
        # Reported on 'branch' now: that is the field the caller got wrong, and
        # there is no longer a 'user_type' field to hang it on.
        self.assertIn("branch", self._field_errors(resp))
        self.assertFalse(User.objects.filter(email="cx.hire@codex.test").exists())

    # -- authorization ----------------------------------------------------

    def test_creating_a_user_requires_the_create_permission(self):
        stranger = User.objects.create_user(
            email="nobody@branched.test", password="Str0ng!pass123",
            status="ACTIVE",
            first_name="No", last_name="Body", tenant=self.branched.tenant,
        )
        client = APIClient()
        client.force_authenticate(user=stranger)

        resp = client.post("/v1/user/users/", {
            "first_name": "Sneaky", "last_name": "Hire",
            "email": "sneaky@branched.test", "gender": "MALE",
            "role": self.role.key,
            "branch": self.lekki.pk,
        }, format="json")

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertFalse(User.objects.filter(email="sneaky@branched.test").exists())

    def test_branch_filter_rejects_a_non_numeric_id_instead_of_erroring(self):
        """The list filter addresses the same integer key as the create field."""
        resp = self.client.get("/v1/user/users/?branch_id=not-an-id")

        self.assertEqual(resp.status_code, 400, resp.content)


class UserBranchTenantGuardTests(TestCase):
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
            branch=self.lekki,
        )

        self.assertEqual(user.tenant_id, self.branched.tenant_id)
        self.assertEqual(user.tenant_id, self.lekki.tenant_id)

    def test_saving_a_user_onto_another_tenants_branch_is_refused(self):
        """The guard must still deny. Without the comparison this write lands
        and the account silently straddles two tenants."""
        user = User.objects.create_user(
            email="straddle@guard.test", password="Str0ng!pass123",
            first_name="Stra", last_name="Ddle", status="ACTIVE",
            branch=self.lekki,
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
            tenant=self.branchless.tenant,
        )
        user.branch = self.lekki

        with self.assertRaises(DjangoValidationError):
            user.save()

    def test_a_branch_in_the_users_own_tenant_still_saves(self):
        user = User.objects.create_user(
            email="ok@guard.test", password="Str0ng!pass123",
            first_name="O", last_name="Kay", status="ACTIVE",
            branch=self.lekki,
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


# =============================================================================
# Per-tenant email, Phase 1 - sign-in resolves the tenant instead of guessing it
# =============================================================================

class SignInTenantScopeTests(TestCase):
    """The tenant is resolved BEFORE the account is looked up.

    Ada Okoye has a child at Bright Star and another at Greenfield. Today her
    address is still globally unique, so these tests exercise the guard rather
    than the collision: an account reached from the wrong tenant's sign-in page
    is refused, and the refusal says nothing about where the account really is.
    """

    def setUp(self):
        from django.test import RequestFactory

        self.password = "Str0ng!pass123"
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.greenfield = make_school(name="Greenfield Academy", slug="greenfield")
        self.ada = make_school_admin(
            self.bright_star, email="ada.okoye@example.test", password=self.password,
        )
        self.cx = make_cx_user(email="ops@codex.test", password=self.password)
        self.factory = RequestFactory()

    def _login(self, email, password, tenant=None):
        request = self.factory.post("/v1/user/auth/login/")
        return LoginService.login(email, password, tenant=tenant, request=request)

    def _latest_attempt(self):
        return AuthAttempt.all_objects.latest("id")

    # ── A sign-in that names no tenant is refused ────────────────────────────
    #
    # These two used to assert the opposite: while the switch was off a
    # tenantless sign-in resolved by a global email lookup, and the comment
    # here said "the two live frontends send no tenant; neither may break".
    # Both now do send one, the switch is on, and the refusal is the point.

    def test_tenant_user_is_refused_when_no_tenant_is_supplied(self):
        with self.assertRaises(ValueError) as ctx:
            self._login(self.ada.email, self.password)

        self.assertEqual(ctx.exception.args[0]["code"], "INVALID_CREDENTIALS")

    def test_cx_staff_is_refused_when_no_tenant_is_supplied(self):
        with self.assertRaises(ValueError) as ctx:
            self._login(self.cx.email, self.password)

        self.assertEqual(ctx.exception.args[0]["code"], "INVALID_CREDENTIALS")

    def test_the_refusal_for_a_missing_tenant_is_audited_as_such(self):
        """Support has to tell "signed in at the wrong address" from "wrong
        password", and the caller must not be able to tell them apart."""
        with self.assertRaises(ValueError):
            self._login(self.ada.email, self.password)

        self.assertEqual(self._latest_attempt().failure_code, "TENANT_REQUIRED")

    # ── The scoped path ──────────────────────────────────────────────────────

    def test_correct_tenant_signs_in(self):
        result = self._login(self.ada.email, self.password, tenant="bright-star")

        self.assertIn("access", result)
        self.assertEqual(result["tenant"]["slug"], "bright-star")

    def test_cx_staff_may_assert_the_platform_tenant(self):
        """CX staff sign in at the console; codex is a tenant slug like any other."""
        result = self._login(self.cx.email, self.password, tenant="codex")

        self.assertIn("access", result)
        self.assertEqual(result["tenant"]["slug"], "codex")

    def test_tenant_slug_is_matched_case_insensitively(self):
        result = self._login(self.ada.email, self.password, tenant="  Bright-Star  ")

        self.assertIn("access", result)

    # ── Refusals are indistinguishable from a wrong password ─────────────────

    def _wrong_password_payload(self):
        with self.assertRaises(ValueError) as ctx:
            self._login(self.ada.email, "not-her-password", tenant="bright-star")
        return ctx.exception.args[0]

    def test_wrong_tenant_is_refused_with_the_wrong_password_message(self):
        with self.assertRaises(ValueError) as ctx:
            self._login(self.ada.email, self.password, tenant="greenfield")

        self.assertEqual(ctx.exception.args[0], self._wrong_password_payload())

    def test_unknown_tenant_is_refused_with_the_same_message(self):
        with self.assertRaises(ValueError) as ctx:
            self._login(self.ada.email, self.password, tenant="no-such-tenant")

        self.assertEqual(ctx.exception.args[0], self._wrong_password_payload())

    def test_suspended_tenant_is_refused_like_an_unknown_one(self):
        from vs_tenants.models import Tenant

        Tenant.objects.filter(pk=self.greenfield.tenant_id).update(
            status=Tenant.Status.SUSPENDED,
        )
        with self.assertRaises(ValueError) as ctx:
            self._login(self.ada.email, self.password, tenant="greenfield")

        self.assertEqual(ctx.exception.args[0]["code"], "INVALID_CREDENTIALS")

    def test_wrong_tenant_never_reaches_the_success_path(self):
        with self.assertRaises(ValueError):
            self._login(self.ada.email, self.password, tenant="greenfield")

        self.assertFalse(
            LoginSession.all_objects.filter(user=self.ada).exists(),
            "a wrong-tenant sign-in must not open a session",
        )

    # ── What the audit trail may and may not say ─────────────────────────────

    def test_tenant_mismatch_is_its_own_failure_code(self):
        with self.assertRaises(ValueError):
            self._login(self.ada.email, self.password, tenant="greenfield")
        attempt = self._latest_attempt()

        self.assertEqual(attempt.failure_code, "TENANT_MISMATCH")
        self.assertEqual(attempt.email_entered, self.ada.email)

    def test_wrong_password_keeps_its_own_failure_code(self):
        with self.assertRaises(ValueError):
            self._login(self.ada.email, "not-her-password", tenant="bright-star")

        self.assertEqual(self._latest_attempt().failure_code, "INVALID_CREDENTIALS")

    def test_mismatch_audit_row_names_only_the_asserted_tenant(self):
        """Greenfield's attempt log must not disclose that Ada is at Bright Star."""
        with self.assertRaises(ValueError):
            self._login(self.ada.email, self.password, tenant="greenfield")
        attempt = self._latest_attempt()

        self.assertEqual(attempt.tenant_id, self.greenfield.tenant_id)
        self.assertNotEqual(attempt.tenant_id, self.bright_star.tenant_id)
        self.assertIsNone(attempt.user_id)

    def test_unknown_tenant_audit_row_names_no_tenant_at_all(self):
        with self.assertRaises(ValueError):
            self._login(self.ada.email, self.password, tenant="no-such-tenant")
        attempt = self._latest_attempt()

        self.assertIsNone(attempt.tenant_id)
        self.assertIsNone(attempt.user_id)
        self.assertEqual(attempt.failure_code, "TENANT_MISMATCH")

    def test_unknown_email_in_a_real_tenant_looks_the_same_as_a_wrong_tenant(self):
        """Otherwise the failure code is an existence oracle over other tenants."""
        with self.assertRaises(ValueError):
            self._login("nobody@example.test", self.password, tenant="greenfield")
        stranger = self._latest_attempt().failure_code

        with self.assertRaises(ValueError):
            self._login(self.ada.email, self.password, tenant="greenfield")

        self.assertEqual(stranger, self._latest_attempt().failure_code)

    # ── The view passes it through ───────────────────────────────────────────

    def test_login_endpoint_accepts_and_enforces_the_tenant_field(self):
        from django.core.cache import cache

        cache.clear()
        client = APIClient()
        response = client.post(
            "/v1/user/auth/login/",
            {"email": self.ada.email, "password": self.password, "tenant": "greenfield"},
            format="json",
        )

        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.json()["error"]["code"], "INVALID_CREDENTIALS")


class SignInTenantRequiredSwitchTests(TestCase):
    """Phase 3 flips one constant; nothing else about the services changes."""

    def setUp(self):
        from django.test import RequestFactory

        self.password = "Str0ng!pass123"
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.ada = make_school_admin(
            self.bright_star, email="ada.okoye@example.test", password=self.password,
        )
        self.factory = RequestFactory()

    def _login(self, tenant=None):
        request = self.factory.post("/v1/user/auth/login/")
        return LoginService.login(
            self.ada.email, self.password, tenant=tenant, request=request,
        )

    def test_omitting_the_tenant_is_refused_once_the_switch_is_on(self):
        with mock.patch(
            "vs_user.services.sign_in_scope.REQUIRE_TENANT_ON_SIGN_IN", True,
        ):
            with self.assertRaises(ValueError) as ctx:
                self._login()

        self.assertEqual(ctx.exception.args[0]["code"], "INVALID_CREDENTIALS")
        self.assertEqual(AuthAttempt.all_objects.latest("id").failure_code,
                         "TENANT_REQUIRED")

    def test_supplying_the_tenant_still_works_once_the_switch_is_on(self):
        with mock.patch(
            "vs_user.services.sign_in_scope.REQUIRE_TENANT_ON_SIGN_IN", True,
        ):
            result = self._login(tenant="bright-star")

        self.assertIn("access", result)

    def test_reset_without_a_tenant_does_nothing_once_the_switch_is_on(self):
        from vs_user.models import PasswordResetRequest
        from vs_user.services.password import PasswordService

        with mock.patch("vs_user.tasks.send_password_reset_email_task"):
            with mock.patch(
                "vs_user.services.sign_in_scope.REQUIRE_TENANT_ON_SIGN_IN", True,
            ):
                PasswordService.request_reset(email=self.ada.email)

        self.assertFalse(PasswordResetRequest.objects.filter(user=self.ada).exists())


class PasswordResetTenantScopeTests(TestCase):
    """A reset asked for at one tenant must never rewrite another tenant's account."""

    def setUp(self):
        self.password = "Str0ng!pass123"
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.greenfield = make_school(name="Greenfield Academy", slug="greenfield")
        self.ada = make_school_admin(
            self.bright_star, email="ada.okoye@example.test", password=self.password,
        )

    def _request(self, tenant=None):
        from vs_user.services.password import PasswordService

        with mock.patch("vs_user.tasks.send_password_reset_email_task"):
            PasswordService.request_reset(email=self.ada.email, tenant=tenant)

    def _resets(self):
        from vs_user.models import PasswordResetRequest

        return PasswordResetRequest.objects.filter(user=self.ada)

    def test_reset_in_the_right_tenant_creates_the_request(self):
        self._request(tenant="bright-star")

        self.assertEqual(self._resets().count(), 1)

    def test_reset_in_the_wrong_tenant_touches_nothing(self):
        self._request(tenant="greenfield")

        self.assertFalse(self._resets().exists())

    def test_reset_in_an_unknown_tenant_touches_nothing(self):
        self._request(tenant="no-such-tenant")

        self.assertFalse(self._resets().exists())

    def test_reset_with_no_tenant_is_refused(self):
        """It used to fall back to a global email lookup, which is how a reset
        asked for at Greenfield could rewrite the Bright Star password."""
        self._request()

        self.assertFalse(self._resets().exists())

    def test_reset_with_no_tenant_still_resolved_globally_while_the_switch_is_off(self):
        with _tenant_required(False):
            self._request()

        self.assertEqual(self._resets().count(), 1)

    def test_reset_endpoint_stays_silent_about_the_wrong_tenant(self):
        from django.core.cache import cache

        cache.clear()
        client = APIClient()
        with mock.patch("vs_user.tasks.send_password_reset_email_task"):
            response = client.post(
                "/v1/user/auth/password/reset/request/",
                {"email": self.ada.email, "tenant": "greenfield"},
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(self._resets().exists())


class BarcodePreviewIsPlatformOnlyTests(TestCase):
    """The unauthenticated barcode preview answers only for the CodeX tenant.

    Unscoped it was a name-and-existence oracle over every parent, student and
    teacher on the platform: anyone who could reach the URL could learn whether
    ada.okoye@example.test held an account, what her account's state was, and -
    if it was active - her full name.
    """

    URL = "/v1/user/auth/special_login/preview/"

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.ada = make_school_admin(self.bright_star, email="ada.okoye@example.test")
        self.cx = make_cx_user(email="ops@codex.test")
        self.client = APIClient()

    def _get(self, email):
        return self.client.get(self.URL, {"email": email})

    def test_cx_staff_address_still_previews(self):
        response = self._get(self.cx.email)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["full_name"], self.cx.full_name)

    def test_customer_tenant_address_is_a_404(self):
        response = self._get(self.ada.email)

        self.assertEqual(response.status_code, 404, response.content)

    def test_customer_404_is_identical_to_an_unknown_address(self):
        known = self._get(self.ada.email)
        unknown = self._get("nobody@example.test")

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(
            known.json()["message"].replace(self.ada.email, "EMAIL"),
            unknown.json()["message"].replace("nobody@example.test", "EMAIL"),
        )

    def test_suspended_customer_user_gets_404_not_403(self):
        """403 would confirm the account exists and disclose its state."""
        self.ada.status = User.Status.SUSPENDED
        self.ada.save(update_fields=["status", "updated_at"])

        response = self._get(self.ada.email)

        self.assertEqual(response.status_code, 404, response.content)

    def test_suspended_cx_staff_still_gets_the_status_message(self):
        self.cx.status = User.Status.SUSPENDED
        self.cx.save(update_fields=["status", "updated_at"])

        response = self._get(self.cx.email)

        self.assertEqual(response.status_code, 403, response.content)
        self.assertIn("suspended", response.json()["message"].lower())


# ═════════════════════════════════════════════════════════════════════════════
# Phase 2: email case
# ═════════════════════════════════════════════════════════════════════════════

class EmailCaseNormalizationTests(TestCase):
    """No address reaches the users table with an uppercase character in it.

    Django's ``normalize_email`` folds only the domain, so ``Ada@gmail.com``
    used to survive creation with its capital. PostgreSQL unique indexes are
    case sensitive, so a second row spelled ``ada@gmail.com`` could then sit
    beside it while every lookup asked for ``iexact`` and took ``.first()``.
    """

    def setUp(self):
        from vs_tenants.models import Tenant

        self.platform = Tenant.objects.get(slug="codex", kind="PLATFORM")
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")

    def _stored(self, pk):
        """The value the database holds, never the one the instance remembers."""
        return User.objects.values_list("email", flat=True).get(pk=pk)

    # ── the write paths ──────────────────────────────────────────────────────

    def test_manager_create_user_stores_the_address_lowercase(self):
        user = User.objects.create_user(
            email="  Ada.Okoye@Example.TEST  ", password="Str0ng!pass123",
            status="ACTIVE", first_name="Ada", last_name="Okoye",
            tenant=self.platform,
        )

        self.assertEqual(self._stored(user.pk), "ada.okoye@example.test")

    def test_save_stores_the_address_lowercase(self):
        """The path create_user does not cover: User(...) then .save()."""
        user = User(
            email="Tunde.Bello@Example.TEST", first_name="Tunde", last_name="Bello",
            status="ACTIVE", tenant=self.platform,
        )
        user.save()

        self.assertEqual(self._stored(user.pk), "tunde.bello@example.test")

    def test_a_later_save_folds_a_new_address_too(self):
        user = make_cx_user(email="ops@codex.test")
        user.email = "OPS.Renamed@Codex.TEST"
        user.save(update_fields=["email", "updated_at"])

        self.assertEqual(self._stored(user.pk), "ops.renamed@codex.test")

    def test_full_clean_folds_before_the_instance_is_ever_saved(self):
        """A validated-but-unsaved instance must already carry the stored form."""
        user = User(
            email="  Ngozi@Example.TEST  ", first_name="Ngozi", last_name="Eze",
            status="ACTIVE", tenant=self.platform,
        )
        user.set_unusable_password()
        user.full_clean()

        self.assertEqual(user.email, "ngozi@example.test")

    def test_full_clean_catches_a_case_variant_of_an_existing_address(self):
        """validate_unique compares with '='; folding first is what makes it see."""
        make_cx_user(email="ada@example.test")
        clash = User(
            email="ADA@Example.TEST", first_name="Ada", last_name="Twin",
            status="ACTIVE", tenant=self.platform,
        )
        clash.set_unusable_password()

        with self.assertRaises(DjangoValidationError) as ctx:
            clash.full_clean()

        self.assertIn("email", ctx.exception.error_dict)

    def test_bulk_create_folds_the_addresses_it_never_calls_save_for(self):
        User.objects.bulk_create([
            User(
                email="  BULK@Example.TEST ", first_name="Bulk", last_name="One",
                status="PENDING", tenant=self.platform,
            ),
        ])

        self.assertTrue(User.objects.filter(email="bulk@example.test").exists())
        self.assertFalse(User.objects.filter(email__contains="BULK").exists())

    def test_bulk_update_folds_the_addresses_it_never_calls_save_for(self):
        user = make_cx_user(email="before@codex.test")
        user.email = "AFTER@Codex.TEST"
        User.objects.bulk_update([user], ["email"])

        self.assertEqual(self._stored(user.pk), "after@codex.test")

    def test_the_database_refuses_a_mixed_case_write_that_skips_every_hook(self):
        """QuerySet.update() and psql go round save(); the constraint does not."""
        from django.db import IntegrityError, transaction

        user = make_cx_user(email="constrained@codex.test")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.filter(pk=user.pk).update(email="Constrained@Codex.TEST")

    def test_the_manager_normalizer_folds_the_local_part_not_just_the_domain(self):
        """Django's own normalize_email leaves 'Ada@' alone. Ours must not."""
        self.assertEqual(
            User.objects.normalize_email("  Ada@GMAIL.com "), "ada@gmail.com",
        )

    # ── the read paths still find the account ────────────────────────────────

    def test_login_works_when_the_address_is_typed_in_a_different_case(self):
        from django.test import RequestFactory

        password = "Str0ng!pass123"
        ada = make_school_admin(
            self.bright_star, email="ada.okoye@example.test", password=password,
        )
        request = RequestFactory().post("/v1/user/auth/login/")

        result = LoginService.login(
            "  ADA.Okoye@Example.TEST ", password,
            tenant=self.bright_star.tenant.slug, request=request,
        )

        self.assertIn("access", result)
        self.assertEqual(result["user"]["email"], ada.email)

    def test_password_reset_works_when_the_address_is_typed_in_a_different_case(self):
        from vs_user.models import PasswordResetRequest
        from vs_user.services.password import PasswordService

        ada = make_school_admin(self.bright_star, email="ada.okoye@example.test")

        with mock.patch("vs_user.tasks.send_password_reset_email_task"):
            PasswordService.request_reset(
                email="ADA.Okoye@Example.TEST", tenant=self.bright_star.tenant.slug,
            )

        self.assertEqual(PasswordResetRequest.objects.filter(user=ada).count(), 1)

    def test_change_email_stores_the_new_address_lowercase(self):
        from vs_user.services.user import EmailChangeService

        user = make_cx_user(email="old@codex.test")
        EmailChangeService.change_email(user, "  New.Address@Codex.TEST  ", user)

        self.assertEqual(self._stored(user.pk), "new.address@codex.test")

    def test_change_email_refuses_a_case_variant_of_someone_elses_address(self):
        from vs_user.services.user import EmailChangeService

        make_cx_user(email="taken@codex.test")
        mover = make_cx_user(email="mover@codex.test")

        with self.assertRaises(ValueError) as ctx:
            EmailChangeService.change_email(mover, "TAKEN@Codex.TEST", mover)

        self.assertEqual(ctx.exception.args[0]["error_code"], "DUPLICATE_EMAIL")


class EmailCaseCreationChecksAgreeTests(TestCase):
    """The two creation paths must mean the same thing by "already exists".

    ``vs_user.serializers`` checked with ``iexact`` and the school-create
    serializer checked with ``=``, so the school path created exactly the
    duplicate the other path refused.
    """

    def setUp(self):
        make_cx_user(email="ada.okoye@example.test")

    def test_platform_user_create_refuses_a_case_variant(self):
        """Driven through the whole serializer, not ``validate_email`` alone.

        The uniqueness check moved to ``validate()`` in Phase 4, because the
        tenant that will own the account is not resolved until then and a
        field-level validator can only ask about the whole platform.
        """
        from rest_framework.exceptions import ValidationError as DRFValidationError

        with self.assertRaises(DRFValidationError) as ctx:
            _run_user_create_serializer(email="  ADA.Okoye@Example.TEST ")

        self.assertIn("email", ctx.exception.detail)

    def test_platform_user_create_returns_the_folded_address(self):
        from vs_user.serializers import UserCreateSerializer

        self.assertEqual(
            UserCreateSerializer().validate_email("  Fresh.Hire@Codex.TEST "),
            "fresh.hire@codex.test",
        )

    def test_admin_provisioning_treats_a_case_variant_as_the_same_account(self):
        """The idempotency check that used to miss and blow its own savepoint."""
        from types import SimpleNamespace

        from schools.vs_schools.models import InviteStatus
        from schools.vs_schools.services.admin_provisioning import provision_admin_user

        school = make_school(name="Bright Star School", slug="bright-star")
        existing = make_school_admin(school, email="head@bright-star.test")
        link = SimpleNamespace(
            invite_status=InviteStatus.QUEUED, invite_sent_at=None,
            save=lambda **kwargs: None,
        )

        returned = provision_admin_user(
            contact=SimpleNamespace(email="HEAD@Bright-Star.TEST", full_name="Head Teacher"),
            admin_link=link, school=school, branch=None,
            role="", actor=None,
        )

        self.assertEqual(returned, existing)
        self.assertEqual(link.invite_status, InviteStatus.SENT)

    def test_the_bulk_importer_sees_a_case_variant_as_taken(self):
        """vs_import_data validated with a bare .lower() against unfolded rows."""
        from vs_import_data.models import ImportRowActionChoices
        from vs_import_data.services.import_executor import import_cx_users_row

        result = import_cx_users_row(
            import_batch=None,
            payload={"email": "ADA.Okoye@Example.TEST", "first_name": "Ada",
                     "last_name": "Okoye"},
            queued_by=None,
        )

        self.assertEqual(result.action, ImportRowActionChoices.SKIP)


class EmailCaseRepairMigrationTests(TestCase):
    """``vs_user.0006`` folds historical rows - or names the ones it will not.

    The repair step is exercised directly rather than through the migration
    executor: the constraint the same migration adds makes it impossible to
    write a mixed-case row through the ORM, so each test drops it, plants the
    row with raw SQL and calls the function. The drop is rolled back with the
    rest of the test transaction.
    """

    MIGRATION = "vs_user.migrations.0006_normalize_user_email_case"

    def setUp(self):
        from django.db import connection
        from importlib import import_module

        self.connection = connection
        self.migration = import_module(self.MIGRATION)
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.greenfield = make_school(name="Greenfield Academy", slug="greenfield")

        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE vs_users_user DROP CONSTRAINT ck_user_email_lowercase"
            )

    def _plant(self, user, raw_email):
        """Put a mixed-case address on an existing row, behind the ORM's back."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE vs_users_user SET email = %s WHERE id = %s",
                [raw_email, user.pk],
            )
        return raw_email

    def _run(self):
        class _Apps:
            @staticmethod
            def get_model(app_label, model_name):
                return User

        class _SchemaEditor:
            connection = self.connection

        self.migration.normalize_email_case(_Apps(), _SchemaEditor())

    def _stored(self, pk):
        return User.objects.values_list("email", flat=True).get(pk=pk)

    # ── it repairs ───────────────────────────────────────────────────────────

    def test_a_mixed_case_row_is_folded(self):
        ada = make_school_admin(self.bright_star, email="ada.okoye@example.test")
        self._plant(ada, "Ada.Okoye@Example.TEST")

        self._run()

        self.assertEqual(self._stored(ada.pk), "ada.okoye@example.test")

    def test_it_is_a_no_op_on_data_that_is_already_lowercase(self):
        ada = make_school_admin(self.bright_star, email="ada.okoye@example.test")
        before = {u.pk: u.email for u in User.objects.all()}

        self._run()

        self.assertEqual({u.pk: u.email for u in User.objects.all()}, before)
        self.assertEqual(self._stored(ada.pk), "ada.okoye@example.test")

    def test_it_is_safe_to_run_again(self):
        """Irreversible, but idempotent - a re-run finds nothing left to do."""
        ada = make_school_admin(self.bright_star, email="ada.okoye@example.test")
        self._plant(ada, "ADA.OKOYE@EXAMPLE.TEST")

        self._run()
        self._run()

        self.assertEqual(self._stored(ada.pk), "ada.okoye@example.test")

    # ── it refuses ───────────────────────────────────────────────────────────

    def test_it_refuses_two_rows_in_one_tenant_that_differ_only_in_case(self):
        """Which of the two accounts is real is a human decision, not this one."""
        ada = make_school_admin(self.bright_star, email="ada.okoye@example.test")
        twin = make_school_admin(self.bright_star, email="ada.okoye+2@example.test")
        planted = self._plant(twin, "Ada.Okoye@Example.TEST")

        with self.assertRaises(RuntimeError) as ctx:
            self._run()

        report = str(ctx.exception)
        self.assertIn("ada.okoye@example.test", report)
        self.assertIn("same tenant", report)
        self.assertIn(f"pk={ada.pk}", report)
        self.assertIn(f"pk={twin.pk}", report)
        self.assertIn(planted, report)

    def test_it_writes_nothing_at_all_when_it_refuses(self):
        ada = make_school_admin(self.bright_star, email="ada.okoye@example.test")
        twin = make_school_admin(self.bright_star, email="ada.okoye+2@example.test")
        self._plant(twin, "Ada.Okoye@Example.TEST")
        stray = make_school_admin(self.greenfield, email="other@example.test")
        self._plant(stray, "Other@Example.TEST")

        with self.assertRaises(RuntimeError):
            self._run()

        self.assertEqual(self._stored(ada.pk), "ada.okoye@example.test")
        self.assertEqual(self._stored(twin.pk), "Ada.Okoye@Example.TEST")
        self.assertEqual(self._stored(stray.pk), "Other@Example.TEST")

    def test_it_refuses_a_pair_that_spans_two_tenants(self):
        """Legal after Phase 3, but User.email is still globally unique today."""
        make_school_admin(self.bright_star, email="ada.okoye@example.test")
        greenfield_ada = make_school_admin(
            self.greenfield, email="ada.okoye+gf@example.test",
        )
        self._plant(greenfield_ada, "Ada.Okoye@Example.TEST")

        with self.assertRaises(RuntimeError) as ctx:
            self._run()

        self.assertIn("different tenants", str(ctx.exception))

    def test_the_refusal_names_every_colliding_address_not_just_the_first(self):
        first = make_school_admin(self.bright_star, email="ada.okoye@example.test")
        first_twin = make_school_admin(self.bright_star, email="ada.okoye+2@example.test")
        second = make_school_admin(self.bright_star, email="tunde.bello@example.test")
        second_twin = make_school_admin(self.bright_star, email="tunde.bello+2@example.test")
        self._plant(first_twin, "Ada.Okoye@Example.TEST")
        self._plant(second_twin, "Tunde.Bello@Example.TEST")

        with self.assertRaises(RuntimeError) as ctx:
            self._run()

        report = str(ctx.exception)
        self.assertIn("2 address(es)", report)
        self.assertIn("ada.okoye@example.test", report)
        self.assertIn("tunde.bello@example.test", report)
        self.assertIn(f"pk={first.pk}", report)
        self.assertIn(f"pk={second.pk}", report)


# =============================================================================
# Per-tenant email, Phase 3 - uniqueness narrows from the platform to a tenant
# =============================================================================

def _tenant_required(value=True):
    """Flip sign_in_scope.REQUIRE_TENANT_ON_SIGN_IN for the block."""
    return mock.patch(
        "vs_user.services.sign_in_scope.REQUIRE_TENANT_ON_SIGN_IN", value,
    )


class EmailUniquePerTenantTests(TestCase):
    """``uq_user_email_per_tenant`` replaces platform-wide uniqueness.

    Ada Okoye has a child at Bright Star and another at Greenfield and uses
    ada.okoye@example.test at both. Before this, Greenfield's database refused
    to create her an account at all - and the refusal itself told Greenfield
    that somebody, somewhere on the platform, held that address.
    """

    def setUp(self):
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.greenfield = make_school(name="Greenfield Academy", slug="greenfield")

    def _plant(self, school, email, **extra):
        """Create straight through save(), skipping full_clean()."""
        return User.objects.create(
            email=email, first_name="Ada", last_name="Okoye",
            status="ACTIVE", tenant=school.tenant,
            **extra,
        )

    # ── what is now allowed ──────────────────────────────────────────────────

    def test_two_tenants_may_hold_the_same_address(self):
        with _tenant_required():
            at_bright_star = make_school_admin(
                self.bright_star, email="ada.okoye@example.test",
            )
            at_greenfield = make_school_admin(
                self.greenfield, email="ada.okoye@example.test",
            )

        self.assertNotEqual(at_bright_star.pk, at_greenfield.pk)
        self.assertNotEqual(at_bright_star.tenant_id, at_greenfield.tenant_id)
        self.assertEqual(
            User.objects.filter(email="ada.okoye@example.test").count(), 2,
        )

    def test_the_email_column_carries_no_unique_index_of_its_own(self):
        """The model must not quietly keep both kinds of uniqueness."""
        self.assertFalse(User._meta.get_field("email").unique)

    # ── what is still refused, by the database ───────────────────────────────

    def test_one_tenant_may_not_hold_the_same_address_twice(self):
        from django.db import IntegrityError, transaction

        self._plant(self.bright_star, "ada.okoye@example.test")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._plant(self.bright_star, "ada.okoye@example.test")

    def test_a_case_variant_in_one_tenant_is_still_refused(self):
        """The constraint is on the raw columns; ck_user_email_lowercase is
        what makes that case-insensitive, by folding the address first."""
        from django.db import IntegrityError, transaction

        self._plant(self.bright_star, "ada.okoye@example.test")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._plant(self.bright_star, "ADA.Okoye@Example.TEST")

    def test_a_case_variant_at_another_tenant_is_the_same_account_key(self):
        """Folding happens before the tenant scope is applied, not after."""
        with _tenant_required():
            at_greenfield = make_school_admin(
                self.greenfield, email="ADA.Okoye@Example.TEST",
            )

        self.assertEqual(at_greenfield.email, "ada.okoye@example.test")

    # ── and by validation, on the field, as unique=True used to ──────────────

    def test_full_clean_reports_a_same_tenant_duplicate_on_the_email_field(self):
        """A two-column constraint reports under NON_FIELD_ERRORS by default.

        Every creation path that relies on full_clean() would have had its
        error shape change silently; User.validate_unique keeps it on email.
        """
        make_school_admin(self.bright_star, email="ada.okoye@example.test")
        clash = User(
            email="ada.okoye@example.test", first_name="Ada", last_name="Twin",
            status="ACTIVE", tenant=self.bright_star.tenant,
        )
        clash.set_unusable_password()

        with self.assertRaises(DjangoValidationError) as ctx:
            clash.full_clean()

        self.assertIn("email", ctx.exception.error_dict)
        self.assertNotIn("__all__", ctx.exception.error_dict)

    def test_full_clean_reports_it_once_not_twice(self):
        make_school_admin(self.bright_star, email="ada.okoye@example.test")
        clash = User(
            email="ada.okoye@example.test", first_name="Ada", last_name="Twin",
            status="ACTIVE", tenant=self.bright_star.tenant,
        )
        clash.set_unusable_password()

        with self.assertRaises(DjangoValidationError) as ctx:
            clash.full_clean()

        self.assertEqual(len(ctx.exception.error_dict["email"]), 1)

    def test_full_clean_allows_the_same_address_at_another_tenant(self):
        make_school_admin(self.bright_star, email="ada.okoye@example.test")
        other = User(
            email="ada.okoye@example.test", first_name="Ada", last_name="Okoye",
            status="ACTIVE", tenant=self.greenfield.tenant,
        )
        other.set_unusable_password()

        with _tenant_required():
            other.full_clean()  # must not raise

    def test_saving_an_existing_user_is_not_a_clash_with_itself(self):
        ada = make_school_admin(self.bright_star, email="ada.okoye@example.test")
        ada.first_name = "Adaeze"

        ada.full_clean()
        ada.save()

        self.assertEqual(User.objects.get(pk=ada.pk).first_name, "Adaeze")


class CrossTenantEmailGuardTests(TestCase):
    """While sign-in does not name its tenant, a second tenant's copy is refused.

    Uniqueness is per tenant now, so ada.okoye@example.test may legally exist
    at Bright Star AND at Greenfield. Sign-in can only tell those two apart
    when the request names the tenant. Without this guard the ordering that
    keeps Ada safe would live only in a plan document: create the pair while
    sign-in is unscoped and she is signed in to whichever school the lookup
    returns first, with her own password, and nothing in any log looks wrong.

    REQUIRE_TENANT_ON_SIGN_IN is now on, so the guard stands down in
    production and the refusal cases below force it off for their block. Each
    test therefore pins the mode it is about, rather than inheriting whatever
    the module constant currently says - which is what let this whole class go
    red the moment the constant moved.
    """

    def setUp(self):
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.greenfield = make_school(name="Greenfield Academy", slug="greenfield")
        self.ada = make_school_admin(
            self.bright_star, email="ada.okoye@example.test",
        )

    def _second_copy(self):
        return make_school_admin(self.greenfield, email="ada.okoye@example.test")

    # ── refused while the switch is off ──────────────────────────────────────

    def test_a_second_tenants_copy_is_refused(self):
        with _tenant_required(False), self.assertRaises(DjangoValidationError) as ctx:
            self._second_copy()

        self.assertIn("email", ctx.exception.error_dict)

    def test_it_is_refused_on_the_path_that_never_calls_full_clean(self):
        """objects.create() goes straight to save(); the guard sits there too."""
        with _tenant_required(False), self.assertRaises(DjangoValidationError):
            User.objects.create(
                email="ada.okoye@example.test", first_name="Ada", last_name="Okoye",
                status="ACTIVE",
                tenant=self.greenfield.tenant,
            )

    def test_a_case_variant_at_another_tenant_is_refused_too(self):
        with _tenant_required(False), self.assertRaises(DjangoValidationError):
            make_school_admin(self.greenfield, email="ADA.Okoye@Example.TEST")

    def test_moving_an_existing_account_onto_the_address_is_refused(self):
        mover = make_school_admin(self.greenfield, email="tunde@example.test")
        mover.email = "ada.okoye@example.test"

        with _tenant_required(False), self.assertRaises(DjangoValidationError):
            mover.save()

    def test_nothing_is_written_when_it_refuses(self):
        with _tenant_required(False), self.assertRaises(DjangoValidationError):
            self._second_copy()

        self.assertEqual(
            User.objects.filter(email="ada.okoye@example.test").count(), 1,
        )

    # ── the refusal says nothing about where the other account lives ─────────

    def test_the_refusal_does_not_name_the_other_tenant(self):
        """Greenfield's admin must not learn that Bright Star has this parent."""
        with _tenant_required(False), self.assertRaises(DjangoValidationError) as ctx:
            self._second_copy()

        message = " ".join(ctx.exception.messages).lower()
        for leak in ("bright", "bright-star", "bright star", str(self.bright_star.tenant_id),
                     str(self.ada.pk), self.ada.first_name.lower(), self.ada.last_name.lower()):
            self.assertNotIn(leak.lower(), message, f"refusal leaked {leak!r}")

    def test_the_refusal_is_the_shared_message(self):
        from vs_user.models import CROSS_TENANT_EMAIL_REFUSAL

        with _tenant_required(False), self.assertRaises(DjangoValidationError) as ctx:
            self._second_copy()

        self.assertEqual(ctx.exception.messages, [CROSS_TENANT_EMAIL_REFUSAL])

    def test_a_same_tenant_duplicate_gets_the_duplicate_message_not_this_one(self):
        from vs_user.models import CROSS_TENANT_EMAIL_REFUSAL

        with _tenant_required(False), self.assertRaises(DjangoValidationError) as ctx:
            make_school_admin(self.bright_star, email="ada.okoye@example.test")

        self.assertNotIn(CROSS_TENANT_EMAIL_REFUSAL, ctx.exception.messages)

    # ── permitted once the switch is on ──────────────────────────────────────

    def test_a_second_tenants_copy_is_allowed_once_the_switch_is_on(self):
        with _tenant_required():
            second = self._second_copy()

        self.assertEqual(second.email, self.ada.email)
        self.assertEqual(second.tenant_id, self.greenfield.tenant_id)

    # ── an account that already exists stays writable ────────────────────────

    def test_a_legal_pair_can_still_be_saved_after_the_switch_goes_back_off(self):
        """Otherwise a status change, or a login's last_login_at write, would
        start failing for both accounts the moment the switch was rolled back."""
        with _tenant_required():
            second = self._second_copy()

        second.status = "SUSPENDED"
        second.save()

        self.assertEqual(User.objects.get(pk=second.pk).status, "SUSPENDED")

    def test_an_update_fields_save_that_excludes_email_never_checks(self):
        with _tenant_required():
            second = self._second_copy()

        second.last_login_at = timezone.now()
        second.save(update_fields=["last_login_at", "updated_at"])

        self.assertIsNotNone(User.objects.get(pk=second.pk).last_login_at)

    def test_an_ordinary_new_address_is_untouched_by_the_guard(self):
        fresh = make_school_admin(self.greenfield, email="tunde.bello@example.test")

        self.assertEqual(fresh.email, "tunde.bello@example.test")


class PerTenantEmailSignInTests(TestCase):
    """Two Adas, one address: sign-in and reset must land on the right one.

    This is the failure the whole plan exists to prevent, and it is only
    reproducible now that the pair can exist. Both accounts are created with
    the switch on, which is the state the platform reaches when both frontends
    send their tenant.
    """

    def setUp(self):
        from django.test import RequestFactory

        self.bright_star_password = "Br1ghtStar!pass"
        self.greenfield_password = "Gr33nfield!pass"
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.greenfield = make_school(name="Greenfield Academy", slug="greenfield")
        with _tenant_required():
            self.at_bright_star = make_school_admin(
                self.bright_star, email="ada.okoye@example.test",
                password=self.bright_star_password,
            )
            self.at_greenfield = make_school_admin(
                self.greenfield, email="ada.okoye@example.test",
                password=self.greenfield_password,
            )
        self.factory = RequestFactory()

    def _login(self, password, tenant=None):
        request = self.factory.post("/v1/user/auth/login/")
        return LoginService.login(
            "ada.okoye@example.test", password, tenant=tenant, request=request,
        )

    def test_each_tenants_sign_in_reaches_its_own_account(self):
        at_bright_star = self._login(self.bright_star_password, tenant="bright-star")
        at_greenfield = self._login(self.greenfield_password, tenant="greenfield")

        self.assertEqual(at_bright_star["user"]["id"], self.at_bright_star.pk)
        self.assertEqual(at_greenfield["user"]["id"], self.at_greenfield.pk)
        self.assertEqual(at_bright_star["tenant"]["slug"], "bright-star")
        self.assertEqual(at_greenfield["tenant"]["slug"], "greenfield")

    def test_one_tenants_password_does_not_open_the_other_tenants_account(self):
        with self.assertRaises(ValueError) as ctx:
            self._login(self.greenfield_password, tenant="bright-star")

        self.assertEqual(ctx.exception.args[0]["code"], "INVALID_CREDENTIALS")

    def test_omitting_the_tenant_is_refused_once_the_switch_is_on(self):
        with _tenant_required():
            with self.assertRaises(ValueError) as ctx:
                self._login(self.bright_star_password)

        self.assertEqual(ctx.exception.args[0]["code"], "INVALID_CREDENTIALS")
        self.assertEqual(
            AuthAttempt.all_objects.latest("id").failure_code, "TENANT_REQUIRED",
        )

    def test_a_reset_at_one_tenant_never_touches_the_other_account(self):
        from vs_user.models import PasswordResetRequest
        from vs_user.services.password import PasswordService

        with mock.patch("vs_user.tasks.send_password_reset_email_task"):
            PasswordService.request_reset(
                email="ada.okoye@example.test", tenant="greenfield",
            )

        self.assertEqual(
            PasswordResetRequest.objects.filter(user=self.at_greenfield).count(), 1,
        )
        self.assertFalse(
            PasswordResetRequest.objects.filter(user=self.at_bright_star).exists(),
        )


class EmailPerTenantMigrationTests(TestCase):
    """``vs_user.0007`` verifies before it writes, in both directions.

    The two check functions are exercised directly rather than through the
    migration executor: the constraint the same migration adds makes the
    forward collision impossible to plant through the ORM, so that test drops
    it first. The drop rolls back with the test transaction.
    """

    MIGRATION = "vs_user.migrations.0007_user_email_unique_per_tenant"

    def setUp(self):
        from django.db import connection
        from importlib import import_module

        self.connection = connection
        self.migration = import_module(self.MIGRATION)
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.greenfield = make_school(name="Greenfield Academy", slug="greenfield")

    def _drop(self, name):
        with self.connection.cursor() as cursor:
            cursor.execute(f"ALTER TABLE vs_users_user DROP CONSTRAINT {name}")

    def _plant(self, school, email):
        return User.objects.create(
            email=email, first_name="Ada", last_name="Okoye",
            status="ACTIVE", tenant=school.tenant,
        )

    def _raw_email(self, user, raw):
        """Put a mixed-case address on a row, behind save() and the constraint."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE vs_users_user SET email = %s WHERE id = %s", [raw, user.pk],
            )
        return raw

    def _shims(self):
        class _Apps:
            @staticmethod
            def get_model(app_label, model_name):
                return User

        class _SchemaEditor:
            connection = self.connection

        return _Apps(), _SchemaEditor()

    def _forward(self):
        self.migration.refuse_same_tenant_duplicates(*self._shims())

    def _reverse(self):
        self.migration.refuse_cross_tenant_duplicates(*self._shims())

    # ── forward ──────────────────────────────────────────────────────────────

    def test_forward_passes_on_clean_data(self):
        make_school_admin(self.bright_star, email="ada.okoye@example.test")

        self._forward()  # must not raise

    def test_forward_passes_when_two_tenants_share_an_address(self):
        """That pair is the point of the migration, not an obstacle to it."""
        with _tenant_required():
            make_school_admin(self.bright_star, email="ada.okoye@example.test")
            make_school_admin(self.greenfield, email="ada.okoye@example.test")

        self._forward()  # must not raise

    def test_forward_refuses_two_rows_in_one_tenant_and_names_them(self):
        self._drop("uq_user_email_per_tenant")
        first = self._plant(self.bright_star, "ada.okoye@example.test")
        second = self._plant(self.bright_star, "ada.okoye@example.test")

        with self.assertRaises(RuntimeError) as ctx:
            self._forward()

        report = str(ctx.exception)
        self.assertIn("1 address(es)", report)
        self.assertIn("ada.okoye@example.test", report)
        self.assertIn(f"pk={first.pk}", report)
        self.assertIn(f"pk={second.pk}", report)
        self.assertIn(f"tenant={self.bright_star.tenant_id}", report)

    def test_forward_refuses_a_case_variant_even_with_no_lowercase_constraint(self):
        """It must not assume 0006 ran: the check folds the address itself."""
        self._drop("uq_user_email_per_tenant")
        self._drop("ck_user_email_lowercase")
        first = self._plant(self.bright_star, "ada.okoye@example.test")
        second = self._plant(self.bright_star, "ada.okoye+2@example.test")
        self._raw_email(second, "ADA.Okoye@Example.TEST")

        with self.assertRaises(RuntimeError) as ctx:
            self._forward()

        report = str(ctx.exception)
        self.assertIn(f"pk={first.pk}", report)
        self.assertIn(f"pk={second.pk}", report)

    def test_forward_ignores_a_lone_row_at_a_third_tenant(self):
        """A tenant that merely shares the address is not part of the problem."""
        self._drop("uq_user_email_per_tenant")
        first = self._plant(self.bright_star, "ada.okoye@example.test")
        second = self._plant(self.bright_star, "ada.okoye@example.test")
        with _tenant_required():
            innocent = self._plant(self.greenfield, "ada.okoye@example.test")

        with self.assertRaises(RuntimeError) as ctx:
            self._forward()

        report = str(ctx.exception)
        self.assertIn(f"pk={first.pk}", report)
        self.assertIn(f"pk={second.pk}", report)
        self.assertNotIn(f"pk={innocent.pk}", report)

    # ── reverse ──────────────────────────────────────────────────────────────

    def test_reverse_passes_when_every_address_is_still_unique_platform_wide(self):
        make_school_admin(self.bright_star, email="ada.okoye@example.test")
        make_school_admin(self.greenfield, email="tunde.bello@example.test")

        self._reverse()  # must not raise

    def test_reverse_refuses_when_two_tenants_share_an_address(self):
        """Restoring unique=True would otherwise die as an opaque IntegrityError."""
        with _tenant_required():
            at_bright_star = make_school_admin(
                self.bright_star, email="ada.okoye@example.test",
            )
            at_greenfield = make_school_admin(
                self.greenfield, email="ada.okoye@example.test",
            )

        with self.assertRaises(RuntimeError) as ctx:
            self._reverse()

        report = str(ctx.exception)
        self.assertIn("1 address(es)", report)
        self.assertIn("ada.okoye@example.test", report)
        self.assertIn(f"pk={at_bright_star.pk}", report)
        self.assertIn(f"pk={at_greenfield.pk}", report)
        self.assertIn(f"tenant={self.bright_star.tenant_id}", report)
        self.assertIn(f"tenant={self.greenfield.tenant_id}", report)

    def test_reverse_names_every_shared_address_not_just_the_first(self):
        with _tenant_required():
            make_school_admin(self.bright_star, email="ada.okoye@example.test")
            make_school_admin(self.greenfield, email="ada.okoye@example.test")
            make_school_admin(self.bright_star, email="tunde.bello@example.test")
            make_school_admin(self.greenfield, email="tunde.bello@example.test")

        with self.assertRaises(RuntimeError) as ctx:
            self._reverse()

        report = str(ctx.exception)
        self.assertIn("2 address(es)", report)
        self.assertIn("ada.okoye@example.test", report)
        self.assertIn("tunde.bello@example.test", report)

    def test_reverse_writes_nothing_when_it_refuses(self):
        with _tenant_required():
            at_bright_star = make_school_admin(
                self.bright_star, email="ada.okoye@example.test",
            )
            at_greenfield = make_school_admin(
                self.greenfield, email="ada.okoye@example.test",
            )

        with self.assertRaises(RuntimeError):
            self._reverse()

        self.assertEqual(
            set(User.objects.filter(email="ada.okoye@example.test")
                .values_list("pk", flat=True)),
            {at_bright_star.pk, at_greenfield.pk},
        )


# =============================================================================
# Phase 4 - every remaining email lookup is scoped, or says why it is not
# =============================================================================

class ScopedEmailLookupTests(TestCase):
    """An unscoped email lookup on ``User`` is a defect, in both directions.

    Unscoped, an ``.exists()`` refuses something legal (Greenfield cannot give
    Ada an account because Bright Star already has one) and a ``.first()``
    accepts something wrong (Greenfield's new admin link is handed Ada's Bright
    Star account). These tests hold both halves for every production path.
    """

    def setUp(self):
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.greenfield = make_school(name="Greenfield Academy", slug="greenfield")
        self.ada = make_school_admin(
            self.bright_star, email="ada.okoye@example.test",
        )

    # ── the helper every path shares ─────────────────────────────────────────

    def test_the_helper_refuses_an_address_held_in_the_same_tenant(self):
        from vs_user.services.email_availability import (
            SAME_TENANT_REFUSAL, email_refusal,
        )

        self.assertEqual(
            email_refusal("ada.okoye@example.test", tenant=self.bright_star.tenant),
            SAME_TENANT_REFUSAL,
        )

    def test_the_helper_allows_another_tenants_address_once_the_switch_is_on(self):
        from vs_user.services.email_availability import email_refusal

        with _tenant_required():
            self.assertEqual(
                email_refusal(
                    "ada.okoye@example.test", tenant=self.greenfield.tenant,
                ),
                "",
            )

    def test_the_helper_mirrors_the_transitional_guard_while_the_switch_is_off(self):
        """It must not be laxer than ``User.save``, or the pre-check would pass
        a create the model then refuses with an unhandled ValidationError."""
        from vs_user.models import CROSS_TENANT_EMAIL_REFUSAL
        from vs_user.services.email_availability import email_refusal

        with _tenant_required(False):
            self.assertEqual(
                email_refusal("ada.okoye@example.test", tenant=self.greenfield.tenant),
                CROSS_TENANT_EMAIL_REFUSAL,
            )

    def test_the_helper_permits_the_second_copy_once_the_switch_is_on(self):
        """The mirror has to follow the model in BOTH directions, or Greenfield
        is refused Ada's second account by a stale pre-check."""
        from vs_user.services.email_availability import email_refusal

        self.assertEqual(
            email_refusal("ada.okoye@example.test", tenant=self.greenfield.tenant),
            "",
        )

    def test_the_helper_ignores_the_account_being_renamed(self):
        from vs_user.services.email_availability import email_refusal

        self.assertEqual(
            email_refusal(
                "ada.okoye@example.test",
                tenant=self.bright_star.tenant,
                exclude_pk=self.ada.pk,
            ),
            "",
        )

    def test_the_bulk_helper_answers_for_several_addresses_at_once(self):
        from vs_user.services.email_availability import (
            SAME_TENANT_REFUSAL, email_refusals,
        )

        refused = email_refusals(
            ["ADA.Okoye@Example.TEST", "nobody@example.test"],
            tenant=self.bright_star.tenant,
        )

        self.assertEqual(refused, {"ada.okoye@example.test": SAME_TENANT_REFUSAL})

    # ── user create ──────────────────────────────────────────────────────────

    def test_user_create_accepts_an_address_held_by_another_tenant(self):
        with _tenant_required():
            attrs = _run_user_create_serializer(
                email="ada.okoye@example.test",
                actor=make_school_admin(self.greenfield, email="head@greenfield.test"),
            )

        self.assertEqual(attrs["email"], "ada.okoye@example.test")
        self.assertEqual(attrs["tenant"], self.greenfield.tenant)

    def test_user_create_refuses_the_same_address_twice_in_one_tenant(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        with _tenant_required():
            with self.assertRaises(DRFValidationError) as ctx:
                _run_user_create_serializer(
                    email="ada.okoye@example.test",
                    actor=make_school_admin(
                        self.bright_star, email="head@bright-star.test",
                    ),
                )

        self.assertIn("email", ctx.exception.detail)

    # ── email change ─────────────────────────────────────────────────────────

    def test_email_change_to_another_tenants_address_succeeds(self):
        from vs_user.services.user import EmailChangeService

        mover = make_school_admin(self.greenfield, email="tunde@greenfield.test")
        with _tenant_required():
            EmailChangeService.change_email(
                mover, "ada.okoye@example.test", mover,
            )

        mover.refresh_from_db()
        self.assertEqual(mover.email, "ada.okoye@example.test")
        self.assertEqual(mover.tenant_id, self.greenfield.tenant_id)
        self.assertEqual(User.objects.get(pk=self.ada.pk).tenant_id,
                         self.bright_star.tenant_id)

    def test_email_change_within_one_tenant_is_still_refused(self):
        from vs_user.services.user import EmailChangeService

        mover = make_school_admin(self.bright_star, email="tunde@bright-star.test")
        with _tenant_required():
            with self.assertRaises(ValueError) as ctx:
                EmailChangeService.change_email(
                    mover, "ada.okoye@example.test", mover,
                )

        self.assertEqual(ctx.exception.args[0]["error_code"], "DUPLICATE_EMAIL")

    # ── admin provisioning: the worst one ────────────────────────────────────

    def _provision(self, school, email):
        from types import SimpleNamespace

        from schools.vs_schools.models import InviteStatus
        from schools.vs_schools.services.admin_provisioning import provision_admin_user
        from vs_rbac.models import TenantRoleTemplate

        role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=school.tenant, key="school_admin",
            defaults={"name": "School Admin", "status": "ACTIVE"},
        )
        link = SimpleNamespace(
            invite_status=InviteStatus.QUEUED, invite_sent_at=None,
            save=lambda **kwargs: None,
        )
        return provision_admin_user(
            contact=SimpleNamespace(email=email, full_name="Ada Okoye", phone=""),
            admin_link=link, school=school, branch=None,
            role=role.key if role else "", actor=None,
        )

    def test_provisioning_never_hands_back_another_schools_account(self):
        """The reported defect, in the shape it would actually happen.

        CodeX creates Greenfield with ada.okoye@example.test as its primary
        admin. Ada already administers Bright Star. Greenfield must get its
        own new account, not a link to hers.
        """
        with _tenant_required():
            provisioned = self._provision(self.greenfield, "ada.okoye@example.test")

        self.assertIsNotNone(provisioned)
        self.assertNotEqual(provisioned.pk, self.ada.pk)
        self.assertEqual(provisioned.tenant_id, self.greenfield.tenant_id)
        self.assertEqual(
            User.objects.get(pk=self.ada.pk).tenant_id, self.bright_star.tenant_id,
        )

    def test_provisioning_is_still_idempotent_within_one_school(self):
        """The behaviour the unscoped lookup was there for, kept."""
        from schools.vs_schools.models import InviteStatus

        before = User.objects.count()
        returned = self._provision(self.bright_star, "ADA.Okoye@Example.TEST")

        self.assertEqual(returned.pk, self.ada.pk)
        self.assertEqual(User.objects.count(), before)
        self.assertEqual(InviteStatus.SENT, "SENT")

    # ── the CX bulk importer ─────────────────────────────────────────────────

    def test_the_cx_importer_ignores_a_school_account_on_the_same_address(self):
        """Phase 0: a CX staff member may also be a parent at a school.

        Ada administers Bright Star and CodeX later hires her. The import must
        not skip her row because "the address already exists" - it exists at a
        customer, not on the platform tenant her CX account will belong to.
        """
        from vs_import_data.models import ImportRowActionChoices
        from vs_import_data.services.import_executor import import_cx_users_row
        from vs_rbac.models import TenantRoleTemplate
        from vs_user.models import OrgNode, Position

        hiring_manager = make_cx_user(email="hiring@codex.test")
        TenantRoleTemplate.objects.get_or_create(
            tenant=hiring_manager.tenant, key="cx_analyst",
            defaults={"name": "CX Analyst", "status": "ACTIVE"},
        )
        node = OrgNode.objects.create(
            name="Operations", code="OPS", kind=OrgNode.Kind.DIVISION,
        )
        Position.objects.create(title="Analyst", code="ANALYST", org_node=node)

        with _tenant_required():
            result = import_cx_users_row(
                import_batch=None,
                payload={"email": "ada.okoye@example.test", "first_name": "Ada",
                         "last_name": "Okoye", "role": "cx_analyst",
                         "position": "ANALYST"},
                queued_by=hiring_manager,
            )

        self.assertEqual(result.action, ImportRowActionChoices.CREATE)
        self.assertEqual(result.instance.tenant_id, hiring_manager.tenant_id)
        self.assertNotEqual(result.instance.pk, self.ada.pk)

    # ── the barcode preview's single-platform-tenant assumption ──────────────

    def test_the_barcode_preview_refuses_rather_than_choosing_a_platform_row(self):
        """Its ``.first()`` is only correct while ONE tenant has kind=PLATFORM.

        That is an assumption, not a constraint, so it is asserted: a second
        platform tenant must make the endpoint answer 404 rather than hand a
        scanner whichever of two people's names came back first.
        """
        from django.core.cache import cache
        from django.test import Client

        from vs_tenants.models import Tenant

        cache.clear()
        second_platform = Tenant.objects.create(
            slug="codex-two", kind=Tenant.Kind.PLATFORM, name="CodeX Two",
            status=Tenant.Status.ACTIVE,
        )
        with _tenant_required():
            User.objects.create_user(
                email="scanner@codex.test", password="Str0ng!pass123",
                status="ACTIVE", first_name="Chidi",
                last_name="One", tenant=Tenant.objects.get(slug="codex"),
            )
            User.objects.create_user(
                email="scanner@codex.test", password="Str0ng!pass123",
                status="ACTIVE", first_name="Nkechi",
                last_name="Two", tenant=second_platform,
            )

        response = Client().get(
            "/v1/user/auth/special_login/preview/",
            {"email": "scanner@codex.test"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Chidi", response.content.decode())
        self.assertNotIn("Nkechi", response.content.decode())


class ScopedEmailLookupCommandTests(TestCase):
    """``delete_user`` and ``create_superuser`` must refuse, not pick a row."""

    def setUp(self):
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.greenfield = make_school(name="Greenfield Academy", slug="greenfield")
        with _tenant_required():
            self.at_bright_star = make_school_admin(
                self.bright_star, email="ada.okoye@example.test",
            )
            self.at_greenfield = make_school_admin(
                self.greenfield, email="ada.okoye@example.test",
            )

    def test_delete_user_refuses_an_address_held_at_two_tenants(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "delete_user", "--email", "ada.okoye@example.test", "--force",
                stdout=StringIO(),
            )

        self.assertIn("more than one tenant", str(ctx.exception))
        self.assertEqual(
            User.objects.filter(email="ada.okoye@example.test").count(), 2,
        )

    def test_delete_user_deletes_exactly_the_named_tenants_account(self):
        from io import StringIO

        from django.core.management import call_command

        call_command(
            "delete_user", "--email", "ada.okoye@example.test",
            "--tenant_id", "greenfield", "--force", stdout=StringIO(),
        )

        self.assertFalse(User.objects.filter(pk=self.at_greenfield.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.at_bright_star.pk).exists())

    def test_delete_user_rejects_an_unknown_tenant_reference(self):
        from io import StringIO

        from django.core.management import call_command
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "delete_user", "--email", "ada.okoye@example.test",
                "--tenant_id", "no-such-school", "--force", stdout=StringIO(),
            )

        self.assertIn("No tenant found", str(ctx.exception))

    def test_create_superuser_assign_role_refuses_an_ambiguous_address(self):
        from io import StringIO

        from django.core.management import call_command
        from vs_rbac.models import TenantUserRoleAssignment

        out = StringIO()
        # --tenant_id is not supplied and the default codex scope holds no such
        # account, so nothing may be promoted by accident either.
        call_command(
            "create_superuser", "--assign-role",
            "--email", "ada.okoye@example.test", stdout=out, stderr=StringIO(),
        )

        self.assertIn("No user found", out.getvalue())
        self.assertFalse(
            TenantUserRoleAssignment.objects.filter(
                user__email="ada.okoye@example.test",
            ).exists()
        )

    def test_create_superuser_assign_role_names_the_tenants_when_scope_is_wide(self):
        from io import StringIO

        from django.core.management import call_command
        from vs_rbac.models import TenantUserRoleAssignment

        out = StringIO()
        with mock.patch(
            "core.management.commands.create_superuser._codex_tenant",
            return_value=None,
        ):
            call_command(
                "create_superuser", "--assign-role",
                "--email", "ada.okoye@example.test", stdout=out, stderr=StringIO(),
            )

        self.assertIn("more than one tenant", out.getvalue())
        self.assertFalse(
            TenantUserRoleAssignment.objects.filter(
                user__email="ada.okoye@example.test",
            ).exists()
        )

    def test_create_superuser_ignores_a_school_account_on_the_same_address(self):
        """Phase 0: a CX staff member may also be a parent at a school.

        admin@codexng.com existing at Bright Star must not block the bootstrap
        superuser, which lives on the codex platform tenant.
        """
        from io import StringIO

        from django.core.management import call_command
        from vs_tenants.models import Tenant

        with _tenant_required():
            make_school_admin(self.bright_star, email="admin@codexng.com")

        with _tenant_required():
            call_command(
                "create_superuser", "--force", "--password", "Str0ng!pass123",
                stdout=StringIO(), stderr=StringIO(),
            )

        self.assertTrue(
            User.objects.filter(
                email="admin@codexng.com", tenant__kind=Tenant.Kind.PLATFORM,
            ).exists()
        )


class ScopedEmailLookupSchoolCreateTests(TestCase):
    """A new school's admin address may already be in use at another school."""

    def setUp(self):
        _seed_prebuilt_admin_roles()
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.ada = make_school_admin(
            self.bright_star, email="ada.okoye@example.test",
        )
        self.actor = make_cx_user(email="onboarding@codex.test")

    def _payload(self, slug="greenfield"):
        return {
            "name": "Greenfield Academy",
            "slug": slug,
            "primary_admin_data": {
                "full_name": "Ada Okoye",
                "email": "ada.okoye@example.test",
            },
            "branches": [{
                "name": "Main Branch",
                "is_main": True,
                "primary_admin_data": {
                    "full_name": "Bola Adeniyi",
                    "email": "bola@greenfield.test",
                },
            }],
        }

    def _create(self):
        from types import SimpleNamespace

        from schools.vs_schools.serializers import SchoolCreateSerializer

        serializer = SchoolCreateSerializer(
            data=self._payload(),
            context={"request": SimpleNamespace(user=self.actor, tenant=self.actor.tenant)},
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def test_the_new_school_gets_its_own_admin_account(self):
        with _tenant_required():
            school = self._create()

        admins = User.objects.filter(
            email="ada.okoye@example.test", tenant=school.tenant,
        )
        self.assertEqual(admins.count(), 1)
        self.assertNotEqual(admins.first().pk, self.ada.pk)
        self.assertEqual(
            User.objects.filter(email="ada.okoye@example.test").count(), 2,
        )

    def test_the_existing_account_is_untouched(self):
        with _tenant_required():
            self._create()

        self.ada.refresh_from_db()
        self.assertEqual(self.ada.tenant_id, self.bright_star.tenant_id)
        self.assertIsNone(self.ada.branch_id)

    def test_it_is_still_refused_while_the_switch_is_off(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        with _tenant_required(False), self.assertRaises(DRFValidationError) as ctx:
            self._create()

        self.assertIn("primary_admin_data", ctx.exception.detail)


class SchoolAuditEventsCarryTheTenantTests(TestCase):
    """An investigator must be able to filter "everything at Bright Star".

    ``AuditEvent.tenant`` is nullable, so a missing argument is silent: the row
    is written, looks complete, and simply cannot be filtered by customer.
    """

    def setUp(self):
        from types import SimpleNamespace

        _seed_prebuilt_admin_roles()
        self.actor = make_cx_user(email="onboarding@codex.test")
        self.request = SimpleNamespace(user=self.actor, tenant=self.actor.tenant)

    def _events(self, module_key):
        from vs_audit.models import AuditEvent
        return AuditEvent.objects.filter(module_key=module_key)

    def _make_school(self):
        from types import SimpleNamespace

        from schools.vs_schools.serializers import SchoolCreateSerializer

        serializer = SchoolCreateSerializer(
            data={
                "name": "Bright Star School",
                "slug": "bright-star",
                "branches": [{"name": "Main Branch", "is_main": True,
                              "primary_admin_data": {
                                  "full_name": "Bola Adeniyi",
                                  "email": "bola@bright-star.test"}}],
            },
            context={"request": self.request},
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def test_school_and_branch_creation_events_carry_the_tenant(self):
        from vs_audit.models import AuditModuleKey

        school = self._make_school()

        school_event = self._events(AuditModuleKey.SCHOOL).get(entity_type="School")
        branch_event = self._events(AuditModuleKey.BRANCH).get(entity_type="Branch")
        self.assertEqual(school_event.tenant_id, school.tenant_id)
        self.assertEqual(branch_event.tenant_id, school.tenant_id)

    def test_a_branch_added_later_carries_the_tenant(self):
        from vs_audit.models import AuditActionType, AuditModuleKey
        from schools.vs_schools.serializers import BranchCreateSerializer

        school = self._make_school()
        serializer = BranchCreateSerializer(
            data={"name": "Annexe", "primary_admin_data": {
                "full_name": "Tunde Bello", "email": "tunde@bright-star.test"}},
            context={"school": school, "actor_id": self.actor, "request": self.request},
        )
        serializer.is_valid(raise_exception=True)
        branch = serializer.save()

        event = self._events(AuditModuleKey.BRANCH).get(
            entity_id=str(branch.pk), action_type=AuditActionType.CREATE,
        )
        self.assertEqual(event.tenant_id, school.tenant_id)
        # branch.tenant IS the school's tenant - Branch is owned by Tenant
        # directly, and the create passes school.tenant. Asserted so the two
        # sources cannot silently drift apart.
        self.assertEqual(branch.tenant_id, school.tenant_id)

    def test_branch_and_school_updates_carry_the_tenant(self):
        from vs_audit.models import AuditActionType, AuditModuleKey
        from schools.vs_schools.serializers import (
            BranchUpdateSerializer, SchoolUpdateSerializer,
        )

        school = self._make_school()
        branch = school.tenant.branches.get(is_main=True)

        branch_ser = BranchUpdateSerializer(
            branch, data={"name": "Main Branch Renamed"}, partial=True,
            context={"actor_id": self.actor, "request": self.request},
        )
        branch_ser.is_valid(raise_exception=True)
        branch_ser.save()

        school_ser = SchoolUpdateSerializer(
            school, data={"motto": "Ad astra"}, partial=True,
            context={"actor_id": self.actor, "request": self.request},
        )
        school_ser.is_valid(raise_exception=True)
        school_ser.save()

        self.assertEqual(
            self._events(AuditModuleKey.BRANCH)
            .get(action_type=AuditActionType.UPDATE).tenant_id,
            school.tenant_id,
        )
        self.assertEqual(
            self._events(AuditModuleKey.SCHOOL)
            .get(action_type=AuditActionType.UPDATE).tenant_id,
            school.tenant_id,
        )

    def test_a_configuration_reset_carries_the_tenant(self):
        from vs_audit.models import AuditActionType, AuditModuleKey
        from schools.vs_schools.serializers import SchoolResetConfigSerializer

        school = self._make_school()
        serializer = SchoolResetConfigSerializer(
            data={"confirmation_token": school.slug},
            context={"school": school, "actor_id": self.actor, "request": self.request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()

        event = self._events(AuditModuleKey.SCHOOL).get(
            action_type=AuditActionType.CONFIG_CHANGED,
        )
        self.assertEqual(event.tenant_id, school.tenant_id)


class IdentityAuditEventsCarryTheTenantTests(TestCase):
    """Sign-in events name the customer, and nothing ambient can supply it.

    Every identity action in the platform funnels through ``log_auth_event``,
    and the ones investigators actually chase - a failed sign-in, a lockout, a
    password reset - happen on unauthenticated endpoints. Authentication has
    not run, so there is no request tenant to inherit: if this helper does not
    pass the subject's own tenant, Bright Star's sign-in history is
    unattributable to Bright Star no matter what the Explorer offers.
    """

    def setUp(self):
        from vs_tenants.context import clear_request_context
        from vs_tenants.models import Tenant

        clear_request_context()
        self.bright_star = Tenant.objects.create(
            name="Bright Star School", slug="bright-star",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        self.bola = User.objects.create_user(
            email="bola@bright-star.test", password="Str0ng!pass123",
            first_name="Bola", last_name="Adeniyi", status="ACTIVE", tenant=self.bright_star,
        )

    def test_a_failed_sign_in_is_filed_under_the_tenant_it_happened_at(self):
        from vs_audit.models import AuditActionType, AuditEvent
        from vs_user.models import AuthEventLog
        from vs_user.services.audit import log_auth_event

        log_auth_event(
            actor=None, subject=self.bola, tenant=self.bola.tenant,
            event=AuthEventLog.Event.LOGIN_FAILURE,
        )

        event = AuditEvent.objects.get(action_type=AuditActionType.LOGIN_FAILED)
        self.assertEqual(event.tenant_id, self.bright_star.pk)

    def test_a_platform_wide_identity_event_with_no_tenant_stays_null(self):
        from vs_audit.models import AuditActionType, AuditEvent
        from vs_user.models import AuthEventLog
        from vs_user.services.audit import log_auth_event

        log_auth_event(
            actor=None, subject=self.bola, tenant=None,
            event=AuthEventLog.Event.PASSWORD_RESET_REQUESTED,
        )

        event = AuditEvent.objects.get(
            action_type=AuditActionType.PASSWORD_RESET_REQUESTED,
        )
        self.assertIsNone(event.tenant_id)


class ParentAtTwoSchoolsEndToEndTests(TestCase):
    """The whole point, proved end to end with the switch on.

    Ada Okoye has Tunde at Bright Star and Zainab at Greenfield and uses
    ada.okoye@example.test at both. Greenfield's admin creates her an account
    through the ordinary user-create endpoint's serializer, and she signs in at
    both schools with two different passwords, reaching a different account
    each time. Neither school can see the other.
    """

    def setUp(self):
        from django.test import RequestFactory
        from vs_rbac.models import TenantRoleTemplate

        _seed_prebuilt_admin_roles()
        self.factory = RequestFactory()
        self.bright_star = make_school(name="Bright Star School", slug="bright-star")
        self.greenfield = make_school(name="Greenfield Academy", slug="greenfield")
        self.bright_star_branch = _main_branch(self.bright_star)
        self.greenfield_branch = _main_branch(self.greenfield)
        for tenant in (self.bright_star.tenant, self.greenfield.tenant):
            TenantRoleTemplate.objects.get_or_create(
                tenant=tenant, key="parent",
                defaults={"name": "Parent", "status": "ACTIVE"},
            )
        self.bright_star_head = make_school_admin(
            self.bright_star, email="head@bright-star.test",
        )
        self.greenfield_head = make_school_admin(
            self.greenfield, email="head@greenfield.test",
        )

    def _create_parent(self, actor, branch, password):
        """Create Ada through UserCreateSerializer + UserCreationService."""
        from types import SimpleNamespace

        from vs_user.serializers import UserCreateSerializer
        from vs_user.services.user import UserCreationService

        serializer = UserCreateSerializer(
            data={
                "first_name": "Ada", "last_name": "Okoye",
                "email": "ada.okoye@example.test",
                "role": "parent",
                "branch": str(branch.pk),
            },
            context={"request": SimpleNamespace(user=actor, tenant=actor.tenant)},
        )
        serializer.is_valid(raise_exception=True)
        user = UserCreationService.create_pending(
            validated_data=serializer.validated_data, requesting_user=actor,
        )
        user.set_password(password)
        user.status = User.Status.ACTIVE
        user.is_active = True
        user.save(update_fields=["password", "status", "is_active", "updated_at"])
        return user

    def test_a_parent_can_be_created_at_a_second_school_and_sign_in_at_both(self):
        with _tenant_required():
            at_bright_star = self._create_parent(
                self.bright_star_head, self.bright_star_branch, "Br1ghtStar!pass",
            )
            at_greenfield = self._create_parent(
                self.greenfield_head, self.greenfield_branch, "Gr33nfield!pass",
            )

        self.assertNotEqual(at_bright_star.pk, at_greenfield.pk)
        self.assertEqual(at_bright_star.tenant_id, self.bright_star.tenant_id)
        self.assertEqual(at_greenfield.tenant_id, self.greenfield.tenant_id)

        def _login(password, tenant):
            return LoginService.login(
                "ada.okoye@example.test", password, tenant=tenant,
                request=self.factory.post("/v1/user/auth/login/"),
            )

        bright_star_session = _login("Br1ghtStar!pass", "bright-star")
        greenfield_session = _login("Gr33nfield!pass", "greenfield")

        self.assertEqual(bright_star_session["user"]["id"], at_bright_star.pk)
        self.assertEqual(greenfield_session["user"]["id"], at_greenfield.pk)
        self.assertEqual(bright_star_session["tenant"]["slug"], "bright-star")
        self.assertEqual(greenfield_session["tenant"]["slug"], "greenfield")

    def test_the_same_school_still_refuses_the_address_twice(self):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        with _tenant_required():
            self._create_parent(
                self.greenfield_head, self.greenfield_branch, "Gr33nfield!pass",
            )
            with self.assertRaises(DRFValidationError) as ctx:
                self._create_parent(
                    self.greenfield_head, self.greenfield_branch, "Another!pass1",
                )

        self.assertIn("email", ctx.exception.detail)

    def test_the_second_account_is_refused_while_the_switch_is_off(self):
        """Phase 3's guard is what makes the legacy unscoped sign-in lookup
        safe, so it must still hold until the switch flips."""
        from rest_framework.exceptions import ValidationError as DRFValidationError

        self._create_parent(
            self.bright_star_head, self.bright_star_branch, "Br1ghtStar!pass",
        )
        with _tenant_required(False), self.assertRaises(DRFValidationError) as ctx:
            self._create_parent(
                self.greenfield_head, self.greenfield_branch, "Gr33nfield!pass",
            )

        self.assertIn("email", ctx.exception.detail)


@tag("slow")
class UserTypeMigrationTests(TransactionTestCase):
    """0009_drop_user_type, and the 0008 conversion that still runs before it.

    Driven by rewinding the real migration graph one step, which is affordable
    here for a reason worth writing down: nothing outside ``vs_user`` depends
    on 0008 or later, so unapplying 0009 unapplies 0009 and nothing else. The
    predecessor of this class drove 0008's data functions against the LIVE
    model registry instead, which worked only while ``user_type`` still existed
    on it. It does not, so the column has to come from the schema the database
    actually had at that point - which is what a historical registry is.

    Rows are built through the historical model and stamped with
    ``QuerySet.update()`` where a value is illegal, because that is exactly how
    such a row would have arrived: past the choices list, past ``clean()``,
    straight into the column.
    """

    serialized_rollback = True

    APP = "vs_user"
    BEFORE = "0008_drop_admin_user_types"

    def setUp(self):
        self._migrate(self.BEFORE)
        self.historical = self._historical_apps(self.BEFORE)
        self.User = self.historical.get_model("vs_user", "User")
        Tenant = self.historical.get_model("vs_tenants", "Tenant")
        Branch = self.historical.get_model("vs_tenants", "Branch")

        self.codex = Tenant.objects.get(slug="codex", kind="PLATFORM")
        self.school = Tenant.objects.create(
            name="Retire School", slug="retire-school", kind="SCHOOL", status="ACTIVE",
        )
        self.lekki = Branch.objects.create(
            tenant=self.school, name="Lekki", code=1, is_main=True, status="ACTIVE",
        )

    def tearDown(self):
        # Clear this class's rows FIRST. Several tests manufacture exactly the
        # shapes 0009 refuses to migrate, and the forward run below is the real
        # migration - it would refuse them again, correctly, and fail the test
        # in tearDown for doing its job. Raw SQL because the live model and the
        # historical one disagree about which columns exist here.
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM vs_users_user WHERE email LIKE '%%@retire.test'"
            )
            cursor.execute(
                "DELETE FROM vs_schools_branch WHERE tenant_id = %s", [self.school.pk],
            )
            cursor.execute(
                "DELETE FROM vs_tenants_tenant WHERE id = %s", [self.school.pk],
            )

        # Always leave the database at the latest state for the rest of the
        # run. Every leaf, not just this app's - see the same rule, and the
        # same reasoning, in schools.vs_schools._MigrationHarness.
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        executor.loader.build_graph()
        super().tearDown()

    # -- harness ----------------------------------------------------------

    def _migrate(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([(self.APP, target)])
        executor.loader.build_graph()
        return executor

    def _historical_apps(self, target):
        """One state registry, so every historical model shares an identity."""
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        return executor.loader.project_state((self.APP, target)).apps

    @staticmethod
    def _module(name):
        """The real migration module. Its name starts with a digit."""
        from importlib import import_module

        return import_module(f"vs_user.migrations.{name}")

    def _user(self, email, *, tenant=None, branch=None, user_type="STAFF", uid=None):
        return self.User.objects.create(
            email=email, first_name="Test", last_name="Person", gender="",
            phone="", uid=uid, user_type=user_type, role="", status="ACTIVE",
            is_staff=False, is_active=True, is_superuser=False,
            tenant=tenant or self.school, branch=branch,
        )

    def _stamp(self, user, **values):
        """Write a value no live writer could produce."""
        self.User.objects.filter(pk=user.pk).update(**values)
        user.refresh_from_db()
        return user

    def _type_of(self, user):
        return self.User.objects.values_list("user_type", flat=True).get(pk=user.pk)

    # -- 0008: the conversion that still runs on a fresh database ---------

    def test_the_retired_admin_personas_still_convert(self):
        """0009 supersedes 0008, but 0007 -> HEAD still passes through it."""
        head = self._stamp(self._user("head@retire.test"), user_type="SCHOOL_ADMIN")
        pinned = self._stamp(
            self._user("lekki.head@retire.test", branch=self.lekki),
            user_type="BRANCH_ADMIN",
        )

        self._module(self.BEFORE).retire_admin_personas(self.historical, None)

        self.assertEqual(self._type_of(head), "STAFF")
        self.assertEqual(self._type_of(pinned), "STAFF")
        # Each kept the branch it had: NULL still means school-wide.
        self.assertIsNone(self.User.objects.get(pk=head.pk).branch_id)
        self.assertEqual(self.User.objects.get(pk=pinned.pk).branch_id, self.lekki.pk)

    def test_a_branch_admin_with_no_branch_is_refused_not_guessed(self):
        """It claims one branch and names none. Whole school, or a lost id?"""
        orphan = self._stamp(self._user("orphan@retire.test"), user_type="BRANCH_ADMIN")
        alongside = self._stamp(
            self._user("also@retire.test", branch=self.lekki), user_type="SCHOOL_ADMIN",
        )

        with self.assertRaises(RuntimeError) as ctx:
            self._module(self.BEFORE).retire_admin_personas(self.historical, None)

        self.assertIn("orphan@retire.test", str(ctx.exception))
        # Refused before it wrote anything: the convertible row alongside it is
        # untouched, so re-running after a fix is not a partial re-run.
        self.assertEqual(self._type_of(orphan), "BRANCH_ADMIN")
        self.assertEqual(self._type_of(alongside), "SCHOOL_ADMIN")

    # -- 0009 forward: it refuses rather than deciding for you ------------

    def _verify_drop(self):
        self._module("0009_drop_user_type").verify_before_drop(self.historical, None)

    def test_the_drop_is_allowed_when_the_two_facts_agree(self):
        self._user("cx@retire.test", tenant=self.codex, user_type="CX_STAFF", uid=10)
        self._user("teacher@retire.test", branch=self.lekki, uid=10)

        self._verify_drop()  # must not raise

    def test_the_drop_refuses_cx_staff_sitting_outside_the_platform_tenant(self):
        """The whole migration rests on those two being the same set.

        If they have already drifted, dropping the column decides which of them
        was telling the truth - and does it silently, which is the one thing it
        must not do.
        """
        # Branchless, because ck_vision_staff_no_branch is still in force at
        # this migration state - it is 0009 that replaces it. The drift being
        # manufactured is the tenant one, which nothing guarded at all.
        stray = self._user("stray.cx@retire.test")
        self._stamp(stray, user_type="CX_STAFF")

        with self.assertRaises(RuntimeError) as ctx:
            self._verify_drop()

        self.assertIn("stray.cx@retire.test", str(ctx.exception))

    def test_the_drop_refuses_a_platform_row_that_is_not_cx_staff(self):
        """After the drop it becomes platform staff by definition. That is a
        promotion, and this migration will not perform one silently."""
        self._user("interloper@retire.test", tenant=self.codex, user_type="STUDENT")

        with self.assertRaises(RuntimeError) as ctx:
            self._verify_drop()

        self.assertIn("interloper@retire.test", str(ctx.exception))

    def test_the_drop_refuses_a_uid_clash_the_merged_constraint_would_reject(self):
        """Two uid constraints become one, and the merge is stricter.

        Worth being straight about how a clash gets there. While both old
        constraints are in force it cannot: a same-tenant pair needs one CX row
        inside a school tenant, and that is the drift the check above already
        refuses. This check earns its keep on a database whose constraints are
        NOT intact - a restored dump, or a table somebody altered by hand - so
        the constraint is dropped here to put the database in exactly that
        state. Without the check the merge fails as an IntegrityError naming an
        index; with it, the two accounts are named.
        """
        with connection.cursor() as cursor:
            # A conditional UniqueConstraint is a partial unique INDEX on
            # PostgreSQL, not a table constraint, so this is the drop that
            # matches how Django created it.
            cursor.execute("DROP INDEX unique_uid_per_tenant")

        self._user("clash.one@retire.test", branch=self.lekki, uid=77)
        self._user("clash.two@retire.test", branch=self.lekki, uid=77)

        with self.assertRaises(RuntimeError) as ctx:
            self._verify_drop()

        self.assertIn("clash.one@retire.test", str(ctx.exception))

    # -- 0009 reverse: what comes back, and what does not -----------------

    def _restore(self):
        self._module("0009_drop_user_type").restore_user_type(self.historical, None)

    def test_the_reverse_recovers_cx_staff_from_the_tenant(self):
        """The one value a reverse can fill in honestly."""
        cx = self._user("rev.cx@retire.test", tenant=self.codex, user_type="")
        teacher = self._user("rev.teacher@retire.test", branch=self.lekki, user_type="")

        self._restore()

        self.assertEqual(self._type_of(cx), "CX_STAFF")
        self.assertEqual(self._type_of(teacher), "STAFF")

    def test_the_reverse_cannot_bring_a_pupil_back(self):
        """The honest loss, asserted so nobody discovers it during a restore.

        After the forward drop a pupil, a guardian and a bursar are the same
        row in every column that remains. The reverse leaves all three as STAFF
        rather than guess which was which.
        """
        pupil = self._user("rev.pupil@retire.test", branch=self.lekki, user_type="STUDENT")

        self._restore()

        self.assertEqual(self._type_of(pupil), "STAFF")

    def test_the_reverse_refuses_a_platform_user_holding_a_branch(self):
        """It would become a CX_STAFF row that violates the re-added
        constraint, and finding that out from a half-applied AddConstraint is
        the worst place to find it out."""
        self._stamp(
            self._user("rev.branched@retire.test", tenant=self.codex, user_type=""),
            branch=self.lekki,
        )

        with self.assertRaises(RuntimeError) as ctx:
            self._restore()

        self.assertIn("rev.branched@retire.test", str(ctx.exception))

    # -- the guarantee survives the migration ----------------------------

    def test_applying_the_migration_installs_the_branch_rule_as_triggers(self):
        """The point of the whole exercise: the database still refuses it.

        A CheckConstraint cannot reach the tenant's kind, so if the rule had
        been allowed to become a Python-only check this would insert happily.
        """
        from django.db import IntegrityError

        probe = self._user(
            "trigger.probe@retire.test", tenant=self.codex, user_type="CX_STAFF",
        )

        self._migrate("0009_drop_user_type")

        # Straight SQL, so nothing but the database is consulted - no
        # serializer, no clean(), no save().
        with self.assertRaises(IntegrityError) as ctx, transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE vs_users_user SET branch_id = %s WHERE id = %s",
                    [self.lekki.pk, probe.pk],
                )
        self.assertIn("must not be assigned to a branch", str(ctx.exception))

    def test_applying_the_migration_leaves_a_tenant_user_free_to_hold_a_branch(self):
        """The other half of the rule, which is just as easy to over-enforce."""
        pinned = self._user("trigger.pinned@retire.test")

        self._migrate("0009_drop_user_type")

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE vs_users_user SET branch_id = %s WHERE id = %s",
                [self.lekki.pk, pinned.pk],
            )
            cursor.execute(
                "SELECT branch_id FROM vs_users_user WHERE id = %s", [pinned.pk],
            )
            self.assertEqual(cursor.fetchone()[0], self.lekki.pk)


class BranchRuleAgreementTests(TestCase):
    """The database and ``clean()`` must forbid the same set, exactly.

    The rule used to be written four times and the copies were free to drift.
    These assert the two that are load-bearing - the check constraint and the
    model's own validation - still say the same thing, from both sides.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school

        self.school = make_school(slug="agree-school", name="Agree School")
        self.branch = make_branch(self.school, name="Main", is_main=True)

    def test_a_tenant_user_with_no_branch_passes_full_clean(self):
        user = User(
            email="wide@agree.test", first_name="Wide", last_name="Reach",
            status="ACTIVE",
            tenant=self.school.tenant, branch=None,
        )
        user.full_clean(exclude=["password"])  # must not raise

    def test_a_student_with_no_branch_passes_full_clean(self):
        user = User(
            email="pupil@agree.test", first_name="Pu", last_name="Pil",
            status="ACTIVE",
            tenant=self.school.tenant, branch=None,
        )
        user.full_clean(exclude=["password"])  # must not raise

    def test_cx_staff_with_a_branch_is_refused_by_clean(self):
        from vs_tenants.models import Tenant

        user = User(
            email="cx@agree.test", first_name="C", last_name="X",
            status="ACTIVE",
            tenant=Tenant.objects.get(slug="codex", kind="PLATFORM"),
            branch=self.branch,
        )
        with self.assertRaises(DjangoValidationError):
            user.full_clean(exclude=["password"])

    def test_cx_staff_with_a_branch_is_refused_by_the_database_too(self):
        """The half that survives a writer who skips validation entirely."""
        from django.db import IntegrityError, transaction

        cx = make_cx_user(email="cx.direct@agree.test")

        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=cx.pk).update(branch=self.branch)

    def test_the_database_no_longer_forbids_a_branchless_tenant_user(self):
        """The inverse: a rule dropped in Python must be dropped in the schema.

        Written with ``update()`` so nothing but the constraint is consulted.
        """
        user = User.objects.create_user(
            email="pinned@agree.test", password="Str0ng!pass123",
            first_name="Pin", last_name="Ned", status="ACTIVE",
            branch=self.branch,
        )

        User.objects.filter(pk=user.pk).update(branch=None)  # must not raise

        user.refresh_from_db()
        self.assertIsNone(user.branch_id)


class PersonaConfersNoAuthorityTests(TestCase):
    """An account's shape confers nothing. Only its role does.

    The personas were retired first and the column dropped after, and both
    steps had the same failure mode: something starts reading what is LEFT of
    an account as though it were a grant. "A tenant user with no branch" is
    the shape the old school-wide SCHOOL_ADMIN had; "a user with no student
    record" is the shape everybody has now. If either is being read as
    authority anywhere, it shows up here.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school

        self.school = make_school(slug="inert-school", name="Inert School")
        self.branch = make_branch(self.school, name="Main", is_main=True)

    def _user(self, email, **kwargs):
        return User.objects.create_user(
            email=email, password="Str0ng!pass123", first_name="In",
            last_name="Ert", status="ACTIVE", tenant=self.school.tenant, **kwargs,
        )

    def test_a_branchless_staff_account_may_not_create_users(self):
        """The shape a SCHOOL_ADMIN row now has, holding no grants at all."""
        actor = self._user("wide@inert.test")
        client = APIClient()
        client.force_authenticate(user=actor)

        resp = client.post("/v1/user/users/", {
            "first_name": "New", "last_name": "Hire",
            "email": "new.hire@inert.test", "gender": "MALE",
            "role": "anything",
        }, format="json")

        self.assertEqual(resp.status_code, 403, resp.content)
        self.assertFalse(User.objects.filter(email="new.hire@inert.test").exists())

    def test_a_school_actor_cannot_mint_a_platform_account(self):
        """The body may not choose which side of the boundary a hire lands on.

        This is the reason the create serializer has no ``user_type`` input,
        and it is worth stating as a security property rather than a tidying.

        The old field let the request body pin the target tenant to codex, and
        ``platform.team.create`` is a TENANT-holdable key - a school
        administrator legitimately holds it to create their own school's staff.
        ``HasRBACPermission`` evaluates it against the tenant the CALLER
        asserted, never against the tenant the new row lands in. So Amaka,
        administrator at Bright Star, could post ``user_type: "CX_STAFF"`` with
        a codex role key and an active position id, pass a permission check
        scoped to Bright Star, and end up with an account inside CodeX carrying
        ``is_staff=True`` and a platform staff profile.

        Which tenant a new account belongs to now follows from the actor's own
        tenant, and a school actor cannot fake that.
        """
        actor = self._user("amaka@inert.test")
        self._grant_create(actor)
        client = APIClient()
        client.force_authenticate(user=actor)

        resp = client.post("/v1/user/users/", {
            "first_name": "Sneaky", "last_name": "Insider",
            "email": "insider@inert.test", "gender": "FEMALE",
            # Ignored: there is no such input field any more. Sent exactly as
            # the old exploit sent it, so this test fails loudly if one is
            # ever reintroduced.
            "user_type": "CX_STAFF",
            "role": "xvs_platform_admin",
        }, format="json")

        # Refused, because the role key is looked up in the actor's OWN tenant
        # now - a codex role is simply not found there.
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(User.objects.filter(email="insider@inert.test").exists())

    def test_a_school_actors_hire_lands_in_their_own_tenant(self):
        """The other half: the legitimate case still works, and stays put."""
        from vs_rbac.models import TenantRoleTemplate

        actor = self._user("amaka2@inert.test")
        self._grant_create(actor)
        TenantRoleTemplate.objects.create(
            tenant=self.school.tenant, key="bursar", name="Bursar",
        )
        client = APIClient()
        client.force_authenticate(user=actor)

        resp = client.post("/v1/user/users/", {
            "first_name": "Ngozi", "last_name": "Bursar",
            "email": "ngozi@inert.test", "gender": "FEMALE",
            "role": "bursar",
        }, format="json")

        self.assertEqual(resp.status_code, 201, resp.content)
        hire = User.objects.get(email="ngozi@inert.test")
        self.assertEqual(hire.tenant_id, self.school.tenant_id)
        self.assertFalse(hire.is_platform_user)
        # And no platform staff profile came with them.
        self.assertFalse(hasattr(hire, "platform_staff_profile"))

    def _grant_create(self, user):
        """Give *user* platform.team.create inside their own tenant."""
        from vs_rbac.tests.helpers import (
            make_assignment, make_permission, make_role, make_role_permission,
        )

        role = make_role(self.school.tenant, name=f"Creator {user.email}")
        make_role_permission(role, make_permission("platform.team.create"))
        make_assignment(self.school.tenant, user, role)
        return role

    def test_the_permission_gate_reads_the_same_for_both_branch_shapes(self):
        """Two STAFF accounts, one branchless, neither granted anything."""
        from vs_rbac.evaluator import has_permission

        wide = self._user("wide2@inert.test")
        pinned = self._user("pinned2@inert.test", branch=self.branch)

        for user in (wide, pinned):
            with self.subTest(branch=user.branch_id):
                self.assertFalse(
                    has_permission(user, "platform.team.create", tenant=self.school.tenant)
                )
