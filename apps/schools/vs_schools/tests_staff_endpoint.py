"""``/v1/i/me/staff/`` - the school's own people, and inviting one.

The same gap as ``/v1/i/me/profile/``, one step further along the checklist.
"Add Staff & Invitations" is a step CodeX asks a school for during onboarding,
and until this endpoint existed the only way to create a school user was
``/v1/user/users/``, gated on ``platform.team.*`` - a key no school
administrator holds. So the step could be asked for and never done. The first
test below is that gap, stated as a passing assertion.

The rest are the two questions this surface has to answer correctly every time:
who may write here, and whose people they can see.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)
from vs_tenants.models import BranchStatus, Tenant
from vs_user.models import User
from vs_user.tokens import CodeXRefreshToken

from .models import SchoolStatus


class SchoolStaffEndpointTests(TestCase):
    """A pending school running its own staff list."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(
            slug="bright-star", name="Bright Star", status=SchoolStatus.PENDING,
        )
        cls.branch = make_branch(
            cls.school, name="Main Branch", status=BranchStatus.PENDING,
        )
        cls.tenant = cls.school.tenant

        cls.view_perm = make_permission("school.administrators.view")
        cls.create_perm = make_permission("school.administrators.create")

        cls.admin_role = make_role(cls.school, name="School Admin", key="school_admin")
        make_role_permission(cls.admin_role, cls.view_perm)
        make_role_permission(cls.admin_role, cls.create_perm)
        cls.admin = make_school_admin(
            None, email="admin@bright-star.example.com", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.admin, cls.admin_role, branch=None)

        # A branch admin reads the list and may not add to it, which is the
        # same read-only shape the onboarding control room gives them.
        cls.branch_role = make_role(cls.school, name="Branch Admin", key="branch_admin")
        make_role_permission(cls.branch_role, cls.view_perm)
        cls.branch_admin = make_school_admin(
            cls.branch, email="branch@bright-star.example.com",
        )
        make_assignment(
            cls.school, cls.branch_admin, cls.branch_role, branch=cls.branch,
        )

        # A whole second school, to ask the isolation question honestly.
        cls.other = make_school(slug="green-field", name="Green Field")
        cls.other_branch = make_branch(cls.other, name="Main Branch")
        cls.other_role = make_role(
            cls.other, name="School Admin", key="school_admin",
        )
        make_role_permission(cls.other_role, cls.view_perm)
        make_role_permission(cls.other_role, cls.create_perm)
        cls.other_admin = make_school_admin(
            None, email="admin@green-field.example.com", tenant=cls.other.tenant,
        )
        make_assignment(cls.other, cls.other_admin, cls.other_role, branch=None)

    def _client(self, user):
        """A real bearer token, not ``force_authenticate``.

        The view reads ``request.tenant``, and only ``TenantJWTAuthentication``
        binds it. A test that took the shortcut would be testing a request
        shape this endpoint never sees.
        """
        token = str(CodeXRefreshToken.for_user(user).access_token)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    @property
    def url(self):
        return reverse("school-staff")

    def _get(self, user, tenant=None):
        return self._client(user).get(
            self.url, {"tenant": (tenant or self.tenant).slug},
        )

    def _post(self, user, payload, tenant=None):
        return self._client(user).post(
            f"{self.url}?tenant={(tenant or self.tenant).slug}",
            payload, format="json",
        )

    def _invite(self, **overrides):
        payload = {
            "first_name": "Ngozi",
            "last_name": "Umeh",
            "email": "ngozi@bright-star.example.com",
            "role": "branch_admin",
        }
        payload.update(overrides)
        return payload

    # ── The gap this endpoint closes ─────────────────────────────────────────

    def test_a_pending_school_can_list_and_invite_its_own_staff(self):
        """The whole point: the step is doable by the school it is asked of."""
        self.assertEqual(self.tenant.status, Tenant.Status.PENDING)

        listed = self._get(self.admin)
        self.assertEqual(listed.status_code, 200, listed.data)

        created = self._post(self.admin, self._invite())
        self.assertEqual(created.status_code, 201, created.data)

        invited = User.objects.get(email="ngozi@bright-star.example.com")
        self.assertEqual(invited.tenant, self.tenant)
        self.assertEqual(invited.status, User.Status.PENDING)

    def test_an_invitation_record_is_created_with_a_key(self):
        """An account created and never invited is a row nobody can sign in to.

        The invitation RECORD is the assertion rather than ``mail.outbox``: the
        message itself is rendered from a seeded notification template, and a
        test database has none, so the channel is skipped and the outbox is
        empty for a reason that has nothing to do with this endpoint. The
        digest below proves the one-time activation credential was issued
        without storing that raw credential on the account.
        """
        response = self._post(self.admin, self._invite())
        self.assertEqual(response.status_code, 201, response.data)

        invited = User.objects.get(email="ngozi@bright-star.example.com")
        self.assertTrue(hasattr(invited, "invitation"))
        self.assertTrue(invited.invitation.token_hash)

    # ── Who may do what ──────────────────────────────────────────────────────

    def test_a_branch_admin_may_read_the_staff_list(self):
        response = self._get(self.branch_admin)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_branch_admin_may_not_invite(self):
        """Read-only, matching the control room they see the same step from."""
        response = self._post(self.branch_admin, self._invite())
        self.assertEqual(response.status_code, 403, response.data)
        self.assertFalse(
            User.objects.filter(email="ngozi@bright-star.example.com").exists(),
        )

    # ── Whose people ─────────────────────────────────────────────────────────

    def test_the_list_is_only_this_school(self):
        """There is no identifier to tamper with; prove the scope holds anyway."""
        response = self._get(self.admin)
        emails = {row["email"] for row in response.data["data"]}
        self.assertIn("admin@bright-star.example.com", emails)
        self.assertNotIn("admin@green-field.example.com", emails)

    def test_asserting_another_schools_tenant_does_not_reach_it(self):
        """Bright Star's admin naming Green Field gets nothing of Green Field's.

        404 rather than 403 on the assertion, so slugs cannot be enumerated.
        """
        response = self._get(self.admin, tenant=self.other.tenant)
        self.assertEqual(response.status_code, 404, getattr(response, "data", None))

    def test_an_invitation_lands_in_the_callers_school_not_a_named_one(self):
        """A `tenant` in the body is not a target. The session decides."""
        response = self._post(
            self.admin, self._invite(tenant=self.other.tenant.pk),
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            User.objects.get(email="ngozi@bright-star.example.com").tenant,
            self.tenant,
        )

    # ── Resending ────────────────────────────────────────────────────────────

    def _resend_url(self, pk):
        return reverse("school-staff-resend", args=[pk])

    def test_resending_reuses_the_account_rather_than_making_a_second(self):
        self._post(self.admin, self._invite())
        invited = User.objects.get(email="ngozi@bright-star.example.com")

        response = self._client(self.admin).post(
            f"{self._resend_url(invited.pk)}?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            User.objects.filter(email="ngozi@bright-star.example.com").count(), 1,
        )

    def test_resending_to_somebody_already_active_is_refused(self):
        response = self._client(self.admin).post(
            f"{self._resend_url(self.admin.pk)}?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 422, response.data)

    def test_another_schools_user_is_not_found_rather_than_forbidden(self):
        """A 403 here would confirm the id exists somewhere on the platform."""
        response = self._client(self.admin).post(
            f"{self._resend_url(self.other_admin.pk)}?tenant={self.tenant.slug}",
        )
        self.assertEqual(response.status_code, 404, response.data)

    # ── The shape the client reads ───────────────────────────────────────────

    def test_the_list_ships_the_roles_this_school_can_hand_out(self):
        """The invite form's options, so it cannot drift from the real roles."""
        response = self._get(self.admin)
        values = {row["value"] for row in response.data["role_options"]}
        self.assertIn("school_admin", values)
        self.assertIn("branch_admin", values)

    def test_the_role_options_are_this_schools_own(self):
        make_role(self.other, name="Green Field Only", key="green_only")
        response = self._get(self.admin)
        values = {row["value"] for row in response.data["role_options"]}
        self.assertNotIn("green_only", values)

    # ── Only admins, while the school is onboarding ──────────────────────────

    def test_a_pending_school_is_only_offered_the_admin_roles(self):
        """The bursar case: Payout Approver is not on offer before go-live."""
        make_role(self.school, name="Payout Approver", key="payout-approver")

        values = {
            row["value"] for row in self._get(self.admin).data["role_options"]
        }
        self.assertEqual(values, {"school_admin", "branch_admin"})

    def test_a_pending_school_is_refused_a_non_admin_role_on_the_post(self):
        """The narrowed dropdown is a courtesy; this is the rule.

        Without this, a crafted request assigns the school's bursar Payout
        Approver during onboarding - a grant that becomes live the moment CodeX
        activates the school, and that no second administrator ever reviewed,
        because during onboarding there is no second administrator.
        """
        make_role(self.school, name="Payout Approver", key="payout-approver")

        response = self._post(self.admin, self._invite(role="payout-approver"))
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("role", response.data["error"]["detail"])
        self.assertFalse(
            User.objects.filter(email="ngozi@bright-star.example.com").exists(),
        )

    def test_a_live_school_may_invite_into_any_of_its_roles(self):
        """The rule is onboarding's, not a permanent restriction.

        Once the school is live, role assignment is reviewable by the
        administrators it now has, so the narrowing lifts.
        """
        make_role(self.school, name="Payout Approver", key="payout-approver")
        Tenant.objects.filter(pk=self.tenant.pk).update(
            status=Tenant.Status.ACTIVE,
        )

        values = {
            row["value"] for row in self._get(self.admin).data["role_options"]
        }
        self.assertIn("payout-approver", values)

        response = self._post(self.admin, self._invite(role="payout-approver"))
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_row_says_whether_its_invitation_can_be_resent(self):
        self._post(self.admin, self._invite())
        rows = {row["email"]: row for row in self._get(self.admin).data["data"]}
        self.assertTrue(rows["ngozi@bright-star.example.com"]["can_resend"])
        self.assertFalse(rows["admin@bright-star.example.com"]["can_resend"])

    def test_a_draft_account_is_not_listed_as_an_invitation(self):
        """Nobody was written to, so nothing should look chased.

        A DRAFT is a record parked before an invitation was sent. Showing it
        under "Invitations sent" tells a school it has already contacted
        somebody it has not.
        """
        drafted = make_school_admin(
            None, email="draft@bright-star.example.com", tenant=self.tenant,
        )
        User.objects.filter(pk=drafted.pk).update(status=User.Status.DRAFT)

        emails = {row["email"] for row in self._get(self.admin).data["data"]}
        self.assertNotIn("draft@bright-star.example.com", emails)
