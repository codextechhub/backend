"""A teacher is put only on a class at a branch their roles cover.

Eze is a Teacher at Lekki and Sule a Teacher at Ikeja. A teacher sees the
classes at their own branches, so Eze made class teacher of an Ikeja class
would hold it on paper and never find its register. The appointment is refused
and names the branch to give Eze a role at; once Eze holds the Teacher role at
Ikeja too, it goes through.
"""
from __future__ import annotations

from schools.vs_academics.models import SchoolClass
from schools.vs_staff.models import TeachingAssignment
from schools.vs_staff.services import teaching
from vs_rbac.tests.helpers import make_assignment

from .base import StaffFixture


class ClassTeacherBranchTests(StaffFixture):
    def appoint(self, school_class, staff):
        return self.put(
            self.admin, "staff-class-teacher",
            {"school_class": school_class.pk, "staff": staff.pk},
        )

    def test_a_teacher_from_another_branch_is_refused_and_told_where_to_add_them(self):
        response = self.appoint(self.ikeja_class, self.eze)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "TEACHER_NOT_AT_BRANCH")
        self.assertEqual(
            response.data["message"],
            "Chukwuemeka Eze does not work at Ikeja. Give them the Teacher role there first.",
        )
        self.ikeja_class.refresh_from_db()
        self.assertIsNone(self.ikeja_class.class_teacher_id)

    def test_a_teacher_given_a_role_at_both_branches_can_lead_either(self):
        make_assignment(self.school, self.eze.user, self.teacher_role, branch=self.ikeja)
        response = self.appoint(self.ikeja_class, self.eze)
        self.assertEqual(response.status_code, 200, response.data)
        self.ikeja_class.refresh_from_db()
        self.assertEqual(self.ikeja_class.class_teacher_id, self.eze.pk)

    def test_a_teacher_at_the_classs_own_branch_is_appointed(self):
        response = self.appoint(self.ikeja_class, self.ikeja_teacher)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_school_wide_class_takes_a_teacher_from_any_branch(self):
        response = self.appoint(self.shared_class, self.ikeja_teacher)
        self.assertEqual(response.status_code, 200, response.data)

    def test_clearing_a_class_teacher_is_never_refused(self):
        response = self.put(
            self.admin, "staff-class-teacher",
            {"school_class": self.ikeja_class.pk, "staff": None},
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_one_branch_school_never_asks(self):
        # Sunrise's teacher holds no role at all and still passes.
        school_class = SchoolClass(
            tenant=self.solo.tenant, name="Basic 1", branch=self.solo_branch,
        )
        teaching.assert_teacher_reaches_class(
            self.solo.tenant, self.solo_staff.user, school_class,
        )


class SubjectDutyBranchTests(StaffFixture):
    def test_a_subject_in_another_branchs_class_is_refused(self):
        response = self.post(
            self.admin, "staff-teaching",
            {"school_class": self.ikeja_class.pk, "subject": self.maths.pk, "part": "LEAD"},
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "TEACHER_NOT_AT_BRANCH")
        self.assertFalse(TeachingAssignment.objects.filter(staff=self.eze).exists())

    def test_a_subject_in_their_own_branchs_class_is_assigned(self):
        response = self.post(
            self.admin, "staff-teaching",
            {"school_class": self.lekki_class.pk, "subject": self.maths.pk, "part": "LEAD"},
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 201, response.data)
