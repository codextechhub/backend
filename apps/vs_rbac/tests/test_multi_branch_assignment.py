"""The same role, granted at two branches, is two grants - not a duplicate.

``TenantUserRoleAssignment`` was designed for exactly this: two partial unique
constraints allow at most one active whole-tenant grant of a role per person,
*and* at most one active grant of a role per person per branch. Mr Eze teaching
at Ikeja on Mondays and Lekki on Thursdays is one person holding one role twice,
and the schema records it.

An API write path that looks for a duplicate on ``(tenant, user, role, ACTIVE)``
without mentioning ``branch`` refuses that arrangement, so the schema permits
what the API forbids. These tests pin the
rule at both write paths, and pin the half that must not regress with it: two
whole-tenant grants of one role are still one too many, and getting the NULL
handling wrong is precisely how that guarantee would be lost.
"""
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from vs_rbac.evaluator import get_effective_permissions, has_permission
from vs_rbac.models import TenantUserRoleAssignment
from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids
from vs_user.tokens import CodeXRefreshToken

from .helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
    make_staff_user,
)
from .test_views import ROLE_KEYS, _grant, _q, _token_client

TEACHING_KEY = "school.students.view"


class _AssignmentAPIFixture(TestCase):
    """A multi-branch school and a single-branch one, side by side.

    Two shapes on purpose. The multi-branch school is where the defect bites,
    and the single-branch school is where the whole-tenant guarantee has to
    survive it: a school with one site issues whole-tenant grants, and it is the
    shape most likely to be broken by a careless ``filter(branch=value)``.
    """

    def setUp(self):
        self.school = make_school(slug="eze-multi", name="Multi Branch College")
        self.tenant = self.school.tenant
        self.hq = make_branch(self.tenant, name="Head Office", is_main=True)
        self.ikeja = make_branch(self.tenant, name="Ikeja", is_main=False)
        self.lekki = make_branch(self.tenant, name="Lekki", is_main=False)

        self.admin = make_school_admin(self.hq, email="registrar@eze-multi.test")
        _grant(self.admin, ROLE_KEYS)

        self.eze = make_staff_user(self.ikeja, email="eze@eze-multi.test")
        # A colleague with no home posting. ``User.branch`` is now consulted
        # only for somebody whose grants say nothing at all, so a whole-tenant
        # grant answers WHOLE_TENANT either way - but this person having no home
        # posting keeps every assertion below about the grants and nothing else,
        # which is what the assignment API tests are here to pin.
        self.roamer = make_staff_user(
            None, email="roamer@eze-multi.test", tenant=self.tenant,
        )
        self.permission = make_permission(TEACHING_KEY)
        self.role = make_role(self.tenant, name="Teacher")
        make_role_permission(self.role, self.permission, granted=True)

        # The single-branch shape, with its own administrator and its own role.
        self.solo_school = make_school(slug="eze-solo", name="Single Site Academy")
        self.solo_tenant = self.solo_school.tenant
        self.solo_branch = make_branch(self.solo_tenant, name="Main", is_main=True)
        self.solo_admin = make_school_admin(
            self.solo_branch, email="registrar@eze-solo.test",
        )
        _grant(self.solo_admin, ROLE_KEYS)
        self.solo_staff = make_staff_user(
            self.solo_branch, email="teacher@eze-solo.test",
        )
        self.solo_role = make_role(self.solo_tenant, name="Teacher")
        make_role_permission(self.solo_role, self.permission, granted=True)

    # -- URLs -------------------------------------------------------------- #
    def _list_url(self, slug=None, **params):
        slug = slug or self.school.slug
        return _q(
            reverse("rbac-assignment-list-create", kwargs={"tenant_slug": slug}),
            slug,
            **params,
        )

    def _detail_url(self, aid, slug=None):
        slug = slug or self.school.slug
        return _q(
            reverse("rbac-assignment-detail", kwargs={"tenant_slug": slug, "id": aid}),
            slug,
        )

    def _revoke_url(self, aid, slug=None):
        slug = slug or self.school.slug
        return _q(
            reverse("rbac-assignment-revoke", kwargs={"tenant_slug": slug, "id": aid}),
            slug,
        )

    def _replace_url(self, aid, slug=None):
        slug = slug or self.school.slug
        return _q(
            reverse("rbac-assignment-replace", kwargs={"tenant_slug": slug, "id": aid}),
            slug,
        )

    # -- Actions ----------------------------------------------------------- #
    def _post_grant(self, user, role, branch=None, *, actor=None, slug=None):
        body = {"user": user.id, "role": role.id, "reason_note": "Timetable"}
        if branch is not None:
            body["branch"] = branch.id
        return _token_client(actor or self.admin).post(
            self._list_url(slug=slug), body, format="json",
        )

    @staticmethod
    def _role_error(resp):
        """The message rendered under the role field on the assign-role form."""
        detail = (resp.data.get("error") or {}).get("detail") or {}
        messages = detail.get("role") if isinstance(detail, dict) else None
        if isinstance(messages, (list, tuple)):
            return str(messages[0])
        return str(messages or resp.data.get("message") or resp.data)


class SameRoleAtTwoBranchesTests(_AssignmentAPIFixture):
    """The defect: Mr Eze teaches at Ikeja and at Lekki."""

    def test_same_role_at_two_branches_both_succeed_and_are_active(self):
        first = self._post_grant(self.eze, self.role, self.ikeja)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.content)

        second = self._post_grant(self.eze, self.role, self.lekki)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED, second.content)

        grants = TenantUserRoleAssignment.objects.filter(
            tenant=self.tenant,
            user=self.eze,
            role=self.role,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        )
        self.assertEqual(grants.count(), 2)
        self.assertEqual(
            {g.branch_id for g in grants}, {self.ikeja.pk, self.lekki.pk},
        )

    def test_both_branches_are_reachable_from_the_two_grants(self):
        self._post_grant(self.eze, self.role, self.ikeja)
        self._post_grant(self.eze, self.role, self.lekki)

        self.assertTrue(has_permission(self.eze, TEACHING_KEY, tenant=self.tenant))
        self.assertEqual(
            visible_branch_ids(self.eze, self.tenant),
            frozenset({self.ikeja.pk, self.lekki.pk}),
        )

    def test_second_grant_at_the_same_branch_is_refused(self):
        self._post_grant(self.eze, self.role, self.ikeja)
        again = self._post_grant(self.eze, self.role, self.ikeja)

        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST, again.content)
        self.assertEqual(
            TenantUserRoleAssignment.objects.filter(
                tenant=self.tenant,
                user=self.eze,
                role=self.role,
                branch=self.ikeja,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).count(),
            1,
        )

    def test_refusal_at_the_same_branch_names_the_branch(self):
        self._post_grant(self.eze, self.role, self.ikeja)
        again = self._post_grant(self.eze, self.role, self.ikeja)

        message = self._role_error(again)
        self.assertIn("Ikeja", message)
        self.assertIn("Teacher", message)

    def test_second_whole_tenant_grant_is_still_refused(self):
        """The NULL trap. Two unpinned grants of one role remain one too many."""
        first = self._post_grant(self.eze, self.role)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.content)

        again = self._post_grant(self.eze, self.role)
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST, again.content)
        self.assertEqual(
            TenantUserRoleAssignment.objects.filter(
                tenant=self.tenant,
                user=self.eze,
                role=self.role,
                branch__isnull=True,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).count(),
            1,
        )

    def test_whole_tenant_refusal_says_so_without_naming_a_branch(self):
        self._post_grant(self.eze, self.role)
        again = self._post_grant(self.eze, self.role)

        message = self._role_error(again)
        self.assertIn("Teacher", message)
        self.assertIn("across the whole organisation", message)

    def test_second_whole_tenant_grant_refused_in_a_single_branch_school(self):
        """The same guarantee, in the shape that never pins a branch at all."""
        first = self._post_grant(
            self.solo_staff, self.solo_role,
            actor=self.solo_admin, slug=self.solo_school.slug,
        )
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.content)

        again = self._post_grant(
            self.solo_staff, self.solo_role,
            actor=self.solo_admin, slug=self.solo_school.slug,
        )
        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST, again.content)
        self.assertEqual(
            TenantUserRoleAssignment.objects.filter(
                tenant=self.solo_tenant,
                user=self.solo_staff,
                role=self.solo_role,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).count(),
            1,
        )

    def test_whole_tenant_and_branch_pinned_grants_of_one_role_coexist(self):
        """Deliberate: the constraints allow it, so the API must not invent a bar.

        The pair is redundant rather than contradictory - the whole-tenant grant
        already reaches Ikeja - and it is how an administrator records "Mr Eze
        teaches everywhere, and is formally posted to Ikeja". Refusing it would
        be the API forbidding what the schema stores, which is the defect these
        tests exist to close.
        """
        wide = self._post_grant(self.roamer, self.role)
        self.assertEqual(wide.status_code, status.HTTP_201_CREATED, wide.content)

        pinned = self._post_grant(self.roamer, self.role, self.ikeja)
        self.assertEqual(pinned.status_code, status.HTTP_201_CREATED, pinned.content)

        self.assertEqual(
            TenantUserRoleAssignment.objects.filter(
                tenant=self.tenant,
                user=self.roamer,
                role=self.role,
                assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
            ).count(),
            2,
        )
        # The whole-tenant grant still dominates the branch-pinned one, exactly
        # as ``vs_rbac.scoping`` documents.
        self.assertIs(visible_branch_ids(self.roamer, self.tenant), WHOLE_TENANT)

    def test_a_different_role_at_the_same_branch_is_untouched(self):
        other_role = make_role(self.tenant, name="Form Master")
        make_role_permission(other_role, self.permission, granted=True)

        self.assertEqual(
            self._post_grant(self.eze, self.role, self.ikeja).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self._post_grant(self.eze, other_role, self.ikeja).status_code,
            status.HTTP_201_CREATED,
        )

    def test_another_person_at_the_same_branch_is_untouched(self):
        colleague = make_staff_user(self.ikeja, email="ada@eze-multi.test")

        self.assertEqual(
            self._post_grant(self.eze, self.role, self.ikeja).status_code,
            status.HTTP_201_CREATED,
        )
        self.assertEqual(
            self._post_grant(colleague, self.role, self.ikeja).status_code,
            status.HTTP_201_CREATED,
        )


class RevokingOneOfTwoGrantsTests(_AssignmentAPIFixture):
    """Mr Eze stops teaching at Lekki. He still teaches at Ikeja."""

    def setUp(self):
        super().setUp()
        self.at_ikeja = make_assignment(
            self.tenant, self.eze, self.role, branch=self.ikeja,
        )
        self.at_lekki = make_assignment(
            self.tenant, self.eze, self.role, branch=self.lekki,
        )

    def test_revoking_lekki_leaves_ikeja_in_force(self):
        resp = _token_client(self.admin).post(
            self._revoke_url(self.at_lekki.id),
            {"reason_note": "Timetable change"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.at_lekki.refresh_from_db()
        self.at_ikeja.refresh_from_db()
        self.assertEqual(self.at_lekki.assignment_status, "REVOKED")
        self.assertEqual(self.at_ikeja.assignment_status, "ACTIVE")

    def test_after_revoking_lekki_he_still_reaches_ikeja(self):
        _token_client(self.admin).post(
            self._revoke_url(self.at_lekki.id),
            {"reason_note": "Timetable change"},
            format="json",
        )

        self.eze.refresh_from_db()
        self.assertTrue(has_permission(self.eze, TEACHING_KEY, tenant=self.tenant))
        self.assertIn(
            TEACHING_KEY, get_effective_permissions(self.eze, tenant=self.tenant),
        )
        self.assertEqual(
            visible_branch_ids(self.eze, self.tenant), frozenset({self.ikeja.pk}),
        )

    def test_the_revoked_branch_can_be_granted_again(self):
        """Re-hiring at Lekki must not collide with the revoked row."""
        _token_client(self.admin).post(
            self._revoke_url(self.at_lekki.id),
            {"reason_note": "Timetable change"},
            format="json",
        )

        again = self._post_grant(self.eze, self.role, self.lekki)
        self.assertEqual(again.status_code, status.HTTP_201_CREATED, again.content)

    def test_revoking_both_leaves_him_reaching_nothing(self):
        for grant in (self.at_ikeja, self.at_lekki):
            _token_client(self.admin).post(
                self._revoke_url(grant.id), {"reason_note": "Left"}, format="json",
            )

        self.eze.refresh_from_db()
        self.assertFalse(has_permission(self.eze, TEACHING_KEY, tenant=self.tenant))


class MovingAGrantBetweenBranchesTests(_AssignmentAPIFixture):
    """PATCH moves a grant from one site to another - unless the target is taken."""

    def setUp(self):
        super().setUp()
        self.at_ikeja = make_assignment(
            self.tenant, self.eze, self.role, branch=self.ikeja,
        )

    def test_a_grant_moves_to_a_free_branch(self):
        resp = _token_client(self.admin).patch(
            self._detail_url(self.at_ikeja.id),
            {"branch": str(self.lekki.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        self.at_ikeja.refresh_from_db()
        self.assertEqual(self.at_ikeja.branch_id, self.lekki.pk)
        self.assertEqual(
            visible_branch_ids(self.eze, self.tenant), frozenset({self.lekki.pk}),
        )

    def test_a_grant_cannot_move_onto_a_branch_that_already_has_one(self):
        make_assignment(self.tenant, self.eze, self.role, branch=self.lekki)

        resp = _token_client(self.admin).patch(
            self._detail_url(self.at_ikeja.id),
            {"branch": str(self.lekki.id)},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn("Lekki", self._role_error(resp))

        self.at_ikeja.refresh_from_db()
        self.assertEqual(self.at_ikeja.branch_id, self.ikeja.pk)

    def test_a_grant_cannot_widen_onto_an_existing_whole_tenant_grant(self):
        make_assignment(self.tenant, self.eze, self.role, branch=None)

        resp = _token_client(self.admin).patch(
            self._detail_url(self.at_ikeja.id),
            {"branch": None},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertIn("across the whole organisation", self._role_error(resp))

    def test_a_grant_may_widen_when_no_whole_tenant_grant_exists(self):
        roamer_grant = make_assignment(
            self.tenant, self.roamer, self.role, branch=self.ikeja,
        )

        resp = _token_client(self.admin).patch(
            self._detail_url(roamer_grant.id),
            {"branch": None},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)

        roamer_grant.refresh_from_db()
        self.assertIsNone(roamer_grant.branch_id)
        self.assertIs(visible_branch_ids(self.roamer, self.tenant), WHOLE_TENANT)

    def test_patching_a_grant_without_touching_branch_does_not_self_collide(self):
        """The exclude(pk=...) half: a row must not be its own duplicate."""
        resp = _token_client(self.admin).patch(
            self._detail_url(self.at_ikeja.id),
            {"reason_note": "Confirmed by the head teacher"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)


class ReplacingARoleAtOneBranchTests(_AssignmentAPIFixture):
    """The replace endpoint swaps a role and keeps the grant's branch."""

    def setUp(self):
        super().setUp()
        self.senior = make_role(self.tenant, name="Senior Teacher")
        make_role_permission(self.senior, self.permission, granted=True)
        self.at_ikeja = make_assignment(
            self.tenant, self.eze, self.role, branch=self.ikeja,
        )

    def _replace(self, assignment, role):
        return _token_client(self.admin).post(
            self._replace_url(assignment.id),
            {"role": str(role.id), "reason_note": "Promotion"},
            format="json",
        )

    def test_replace_succeeds_when_the_target_role_is_held_at_another_branch(self):
        """Senior Teacher at Lekki must not block Senior Teacher at Ikeja."""
        make_assignment(self.tenant, self.eze, self.senior, branch=self.lekki)

        resp = self._replace(self.at_ikeja, self.senior)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

        replacement = TenantUserRoleAssignment.objects.get(
            tenant=self.tenant,
            user=self.eze,
            role=self.senior,
            branch=self.ikeja,
            assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
        )
        self.assertEqual(replacement.branch_id, self.ikeja.pk)
        self.at_ikeja.refresh_from_db()
        self.assertEqual(self.at_ikeja.assignment_status, "REVOKED")

    def test_replace_is_refused_when_the_target_role_is_held_at_this_branch(self):
        make_assignment(self.tenant, self.eze, self.senior, branch=self.ikeja)

        resp = self._replace(self.at_ikeja, self.senior)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT, resp.content)
        self.assertIn("Ikeja", str(resp.data.get("message")))

        self.at_ikeja.refresh_from_db()
        self.assertEqual(self.at_ikeja.assignment_status, "ACTIVE")

    def test_replacing_a_whole_tenant_grant_collides_only_with_a_whole_tenant_one(self):
        wide = make_assignment(self.tenant, self.eze, self.role, branch=None)
        make_assignment(self.tenant, self.eze, self.senior, branch=self.ikeja)

        resp = self._replace(wide, self.senior)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.content)

    def test_replacing_a_whole_tenant_grant_is_refused_by_a_whole_tenant_holder(self):
        # ``self.at_ikeja`` occupies Ikeja; give him the wide Teacher grant too.
        wide = make_assignment(self.tenant, self.eze, self.role, branch=None)
        make_assignment(self.tenant, self.eze, self.senior, branch=None)

        resp = self._replace(wide, self.senior)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT, resp.content)
        self.assertIn("across the whole organisation", str(resp.data.get("message")))


class MultiBranchGrantsDoNotDoubleCountTests(_AssignmentAPIFixture):
    """One person holding one role twice is still one person."""

    def test_the_assignment_list_shows_both_grants_distinctly(self):
        make_assignment(self.tenant, self.eze, self.role, branch=self.ikeja)
        make_assignment(self.tenant, self.eze, self.role, branch=self.lekki)

        resp = _token_client(self.admin).get(
            self._list_url(assignment_status="ACTIVE"),
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        rows = [r for r in resp.data["data"] if r["user_id"] == str(self.eze.id)]
        self.assertEqual(len(rows), 2)

    def test_the_roles_list_counts_holders_not_grants(self):
        """One teacher at two branches must not read as two teachers."""
        make_assignment(self.tenant, self.eze, self.role, branch=self.ikeja)
        make_assignment(self.tenant, self.eze, self.role, branch=self.lekki)

        resp = _token_client(self.admin).get(
            _q(
                reverse(
                    "rbac-role-list-create",
                    kwargs={"tenant_slug": self.school.slug},
                ),
                self.school.slug,
            )
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.content)
        row = next(r for r in resp.data["data"] if r["key"] == self.role.key)
        self.assertEqual(row["assigned_users_count"], 1)

    def test_the_roles_list_still_counts_two_people_as_two(self):
        colleague = make_staff_user(self.lekki, email="ada-count@eze-multi.test")
        make_assignment(self.tenant, self.eze, self.role, branch=self.ikeja)
        make_assignment(self.tenant, colleague, self.role, branch=self.lekki)

        resp = _token_client(self.admin).get(
            _q(
                reverse(
                    "rbac-role-list-create",
                    kwargs={"tenant_slug": self.school.slug},
                ),
                self.school.slug,
            )
        )
        row = next(r for r in resp.data["data"] if r["key"] == self.role.key)
        self.assertEqual(row["assigned_users_count"], 2)

    def test_effective_permissions_are_a_set_not_a_tally(self):
        make_assignment(self.tenant, self.eze, self.role, branch=self.ikeja)
        make_assignment(self.tenant, self.eze, self.role, branch=self.lekki)

        keys = get_effective_permissions(self.eze, tenant=self.tenant)
        self.assertIn(TEACHING_KEY, keys)
        self.assertEqual(len([k for k in keys if k == TEACHING_KEY]), 1)


class AnonymousAndCrossTenantStillRefusedTests(_AssignmentAPIFixture):
    """The branch-aware check must not have loosened anything else."""

    def test_anon_cannot_create_a_branch_pinned_grant(self):
        body = {"user": self.eze.id, "role": self.role.id, "branch": self.ikeja.id}
        resp = APIClient().post(self._list_url(), body, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_branch_from_another_tenant_is_refused(self):
        resp = self._post_grant(self.eze, self.role, self.solo_branch)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.content)
        self.assertFalse(
            TenantUserRoleAssignment.objects.filter(
                user=self.eze, branch=self.solo_branch,
            ).exists()
        )

    def test_a_plain_staff_member_cannot_grant_at_a_branch(self):
        plain = make_staff_user(self.ikeja, email="plain@eze-multi.test")
        resp = self._post_grant(self.eze, self.role, self.ikeja, actor=plain)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
