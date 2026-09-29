"""An administrator pinned to a school's only branch manages the whole school.

Sunrise Academy has one branch, Main. Its head holds the school admin role
pinned to Main. Every person at Sunrise is posted to Main or to the whole
school, so the pin carries no meaning: she grants school-wide roles, edits the
school-wide registrar and posts people school-wide, exactly as an unpinned
administrator does. The day Sunrise opens a second branch, the same grant makes
her a branch administrator again and those writes are refused.
"""
from __future__ import annotations

from vs_rbac.models import TenantUserRoleAssignment
from vs_rbac.tests.helpers import make_assignment, make_branch, make_school_admin

from .base import StaffFixture


class OneBranchAdminReachTests(StaffFixture):
    """The pinned head of a one-branch school against the unpinned rules."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        solo_tenant = cls.solo.tenant
        cls.pinned_head = make_school_admin(
            None, email="pinned.head@sunrise.test", tenant=solo_tenant,
        )
        make_assignment(cls.solo, cls.pinned_head, cls.solo_role, branch=cls.solo_branch)
        cls.solo_registrar = cls.make_staff(
            "registrar@sunrise.test", "Kemi", "Bello", branch=None,
            job_title="Registrar", tenant=solo_tenant, school=cls.solo,
        )
        cls.pinned_teacher = cls.make_staff(
            "pinned.teacher@sunrise.test", "Tobi", "Ojo", branch=cls.solo_branch,
            role=cls.solo_teacher_role, tenant=solo_tenant, school=cls.solo,
        )

    def school_wide_grants(self, staff):
        return TenantUserRoleAssignment.objects.filter(
            tenant=self.solo.tenant, user=staff.user, role=self.solo_role,
            branch__isnull=True, assignment_status="ACTIVE",
        ).count()

    def open_second_branch(self):
        return make_branch(self.solo, name="Ajah", is_main=False)

    def test_she_grants_a_school_wide_role(self):
        response = self.post(self.pinned_head, "staff-bulk-role", {
            "staff_ids": [self.solo_staff.pk], "role": "school_admin",
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.school_wide_grants(self.solo_staff), 1)

    def test_she_edits_the_school_wide_registrar(self):
        response = self.patch(
            self.pinned_head, "staff-detail", {"job_title": "Chief Registrar"},
            pk=self.solo_registrar.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["can_manage"])

    def test_she_posts_somebody_school_wide(self):
        response = self.post(self.pinned_head, "staff-bulk-posting", {
            "staff_ids": [self.solo_staff.pk], "branch": None,
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.solo_staff.refresh_from_db()
        self.assertIsNone(self.solo_staff.branch_id)

    def test_a_new_person_she_adds_without_a_branch_is_school_wide(self):
        from ..services.scoping import guard_postings

        self.assertEqual(
            guard_postings(self.pinned_head, self.solo.tenant, [], default_when_unset=True),
            [],
        )

    def test_a_reach_of_the_only_branch_reads_as_school_wide(self):
        response = self.get(self.solo_admin, "staff-roles", pk=self.pinned_teacher.pk)
        self.assertEqual(response.status_code, 200, response.data)
        reach = response.data["data"]["reach"]
        self.assertTrue(reach["school_wide"])
        self.assertEqual(reach["branches"], [])

    def test_a_second_branch_makes_her_a_branch_administrator_again(self):
        self.open_second_branch()

        granted = self.post(self.pinned_head, "staff-bulk-role", {
            "staff_ids": [self.solo_staff.pk], "role": "school_admin",
        })
        self.assertEqual(granted.status_code, 400, granted.data)
        self.assertIn("reach your own branches", str(granted.data))
        self.assertEqual(self.school_wide_grants(self.solo_staff), 0)

        edited = self.patch(
            self.pinned_head, "staff-detail", {"job_title": "Chief Registrar"},
            pk=self.solo_registrar.pk,
        )
        self.assertEqual(edited.status_code, 403, edited.data)

        posted = self.post(self.pinned_head, "staff-bulk-posting", {
            "staff_ids": [self.solo_staff.pk], "branch": None,
        })
        self.assertEqual(posted.status_code, 403, posted.data)

    def test_a_second_branch_narrows_the_reach_on_her_profile_again(self):
        self.open_second_branch()

        response = self.get(self.solo_admin, "staff-roles", pk=self.pinned_teacher.pk)
        reach = response.data["data"]["reach"]
        self.assertFalse(reach["school_wide"])
        self.assertEqual([row["id"] for row in reach["branches"]], [self.solo_branch.pk])
