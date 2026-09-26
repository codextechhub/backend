"""A branch administrator reads their branch and the school-wide people, and changes only their own.

The fixture's Lekki head holds every staff key, pinned to Lekki. The registrar
is posted school-wide, Eze to Lekki and Sule to Ikeja. The whole-tenant admin is
the control: nothing here narrows them.
"""
from __future__ import annotations

from vs_rbac.models import TenantUserRoleAssignment

from .base import StaffFixture


class SharedRecordsAreReadOnlyTests(StaffFixture):
    """School-wide people are visible to a branch administrator and not theirs to change."""

    def test_a_branch_admin_can_open_the_school_wide_registrar(self):
        response = self.get(self.lekki_head, "staff-detail", pk=self.registrar.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["data"]["can_manage"])

    def test_a_branch_admin_cannot_edit_the_school_wide_registrar(self):
        response = self.patch(
            self.lekki_head, "staff-detail", {"job_title": "Bursar"},
            pk=self.registrar.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.registrar.refresh_from_db()
        self.assertEqual(self.registrar.job_title, "Registrar")

    def test_a_branch_admin_can_edit_their_own_branchs_person(self):
        response = self.patch(
            self.lekki_head, "staff-detail", {"job_title": "Head of Maths"},
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["can_manage"])

    def test_every_write_on_a_school_wide_person_is_refused(self):
        writes = (
            ("post", "staff-qualifications", {"title": "BSc"}),
            ("post", "staff-leave", {
                "leave_type": "ANNUAL", "start_date": "2026-01-05",
                "end_date": "2026-01-06",
            }),
            ("post", "staff-account-suspend", {}),
            ("patch", "staff-account-email", {"email": "new@brightfield.test"}),
            ("post", "staff-invitation-revoke", {"reason": "Wrong person"}),
        )
        for method, name, body in writes:
            with self.subTest(route=name):
                response = getattr(self, method)(
                    self.lekki_head, name, body, pk=self.registrar.pk,
                )
                self.assertEqual(response.status_code, 403, (name, response.data))
                self.assertIn("SHARED_RECORD_READ_ONLY", str(response.data), name)

    def test_somebody_also_posted_to_another_branch_is_read_only_too(self):
        self.post(self.admin, "staff-bulk-posting", {
            "staff_ids": [self.eze.pk], "branch_ids": [self.lekki.pk, self.ikeja.pk],
        })
        response = self.patch(
            self.lekki_head, "staff-detail", {"job_title": "Head"}, pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_school_wide_admin_still_manages_the_registrar(self):
        response = self.patch(
            self.admin, "staff-detail", {"job_title": "Chief Registrar"},
            pk=self.registrar.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["data"]["can_manage"])

    def test_the_directory_marks_which_rows_the_viewer_may_change(self):
        response = self.get(self.lekki_head, "staff-list")
        rows = {row["full_name"]: row["can_manage"] for row in response.data["data"]}
        self.assertFalse(rows["Adaeze Nwankwo"])
        self.assertTrue(rows["Chukwuemeka Eze"])


class PostingsStayInsideTheCallersBranchesTests(StaffFixture):
    """A branch administrator posts people only to their own branches."""

    def test_moving_somebody_to_another_branch_is_refused(self):
        response = self.post(self.lekki_head, "staff-bulk-posting", {
            "staff_ids": [self.eze.pk], "branch": self.ikeja.pk,
        })
        self.assertEqual(response.status_code, 403, response.data)
        self.assertIn("BRANCH_OUTSIDE_REACH", str(response.data))
        self.eze.refresh_from_db()
        self.assertEqual(self.eze.branch_id, self.lekki.pk)

    def test_making_somebody_school_wide_is_refused(self):
        response = self.post(self.lekki_head, "staff-bulk-posting", {
            "staff_ids": [self.eze.pk], "branch": None,
        })
        self.assertEqual(response.status_code, 403, response.data)
        self.eze.refresh_from_db()
        self.assertEqual(self.eze.branch_id, self.lekki.pk)

    def test_claiming_the_school_wide_registrar_for_one_branch_is_refused(self):
        response = self.post(self.lekki_head, "staff-bulk-posting", {
            "staff_ids": [self.registrar.pk], "branch": self.lekki.pk,
        })
        self.assertEqual(response.status_code, 403, response.data)
        self.registrar.refresh_from_db()
        self.assertIsNone(self.registrar.branch_id)

    def test_the_record_edit_cannot_move_a_posting_elsewhere(self):
        response = self.patch(
            self.lekki_head, "staff-detail", {"branch": self.ikeja.pk}, pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.eze.refresh_from_db()
        self.assertEqual(self.eze.branch_id, self.lekki.pk)

    def test_a_new_person_is_filed_under_the_callers_branch(self):
        response = self.post(
            self.lekki_head, "staff-list",
            self.invite_body(email="new.lekki@brightfield.test"),
        )
        self.assertEqual(response.status_code, 201, response.data)
        from ..models import StaffProfile

        person = StaffProfile.all_objects.get(pk=response.data["data"]["id"])
        self.assertEqual(person.branch_id, self.lekki.pk)

    def test_a_new_person_cannot_be_posted_to_another_branch(self):
        response = self.post(
            self.lekki_head, "staff-list",
            self.invite_body(email="new.ikeja@brightfield.test", branch=self.ikeja.pk),
        )
        self.assertEqual(response.status_code, 403, response.data)
        self.assertIn("BRANCH_OUTSIDE_REACH", str(response.data))

    def test_a_school_wide_admin_may_still_post_anybody_anywhere(self):
        response = self.post(self.admin, "staff-bulk-posting", {
            "staff_ids": [self.eze.pk], "branch": None,
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.eze.refresh_from_db()
        self.assertIsNone(self.eze.branch_id)


class RoleGrantsStayInsideTheCallersBranchesTests(StaffFixture):
    """Granting a role cannot widen somebody past the granter's own branches."""

    def _grants(self, user):
        return TenantUserRoleAssignment.objects.filter(
            tenant=self.tenant, user=user, assignment_status="ACTIVE",
        ).count()

    def test_a_school_wide_grant_from_a_branch_admin_is_refused(self):
        before = self._grants(self.eze.user)
        response = self.post(self.lekki_head, "staff-bulk-role", {
            "staff_ids": [self.eze.pk], "role": "school_admin",
        })
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("reach your own branches", str(response.data))
        self.assertEqual(self._grants(self.eze.user), before)

    def test_a_grant_pinned_to_the_callers_branch_is_allowed(self):
        response = self.post(self.lekki_head, "staff-bulk-role", {
            "staff_ids": [self.eze.pk], "role": "school_admin", "branch": self.lekki.pk,
        })
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_grant_to_the_school_wide_registrar_is_refused(self):
        response = self.post(self.lekki_head, "staff-bulk-role", {
            "staff_ids": [self.registrar.pk], "role": "teacher", "branch": self.lekki.pk,
        })
        self.assertEqual(response.status_code, 403, response.data)


class PostingDimensionIsPerViewerTests(StaffFixture):
    """A viewer who works in one branch is never shown where people are posted."""

    def test_a_single_branch_viewer_gets_no_posting_fields(self):
        response = self.get(self.lekki_head, "staff-list")
        self.assertFalse(response.data["multi_branch"])
        self.assertEqual(response.data["counts"]["breakdown_by"], "role")
        for row in response.data["data"]:
            self.assertIsNone(row["branch_name"])
            self.assertIsNone(row["posted_school_wide"])

    def test_a_single_branch_viewer_has_no_roster(self):
        response = self.get(
            self.lekki_head, "staff-roster", {"branch": self.lekki.pk},
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_whole_school_viewer_still_sees_postings(self):
        response = self.get(self.admin, "staff-list")
        self.assertTrue(response.data["multi_branch"])
        self.assertEqual(response.data["counts"]["breakdown_by"], "branch")

    def test_a_viewer_covering_two_branches_reads_only_their_rosters(self):
        from vs_rbac.tests.helpers import make_assignment, make_branch, make_school_admin

        yaba = make_branch(self.school, name="Yaba", is_main=False)
        deputy = make_school_admin(None, email="deputy@brightfield.test", tenant=self.tenant)
        make_assignment(self.school, deputy, self.role, branch=self.lekki)
        make_assignment(self.school, deputy, self.role, branch=self.ikeja)

        own = self.get(deputy, "staff-roster", {"branch": self.ikeja.pk})
        self.assertEqual(own.status_code, 200, own.data)
        other = self.get(deputy, "staff-roster", {"branch": yaba.pk})
        self.assertEqual(other.status_code, 404, other.data)

