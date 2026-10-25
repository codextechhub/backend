"""A teacher keeps reach of the classes they hold, whatever changes later.

Brightfield runs Lekki and Ikeja. Sule is a Teacher at Ikeja and leads JSS1 C
there. When he moves to Lekki, taking his Ikeja role away, moving it to Lekki,
or replacing it with a Lekki one would leave JSS1 C with a class teacher who
cannot see it, so each is refused until the class is handed over. Somebody left
with no role at all still sees their home posting, so that alone strands
nothing. Moving JSS1 C itself to Lekki is
refused for the same reason. ``teachers_outside_their_branch`` lists the cases
already in the data.
"""
from __future__ import annotations

from io import StringIO

from django.core.management import call_command

from schools.vs_academics.models import SchoolClass
from schools.vs_staff.exceptions import TeacherStillHoldsClasses
from schools.vs_staff.models import TeachingAssignment
from vs_rbac.models import TenantRoleGrantRequest, TenantUserRoleAssignment
from vs_rbac.services import apply_role_grant_request, grant_role, raise_role_grant_request
from vs_rbac.tests.helpers import make_assignment, make_permission, make_role_permission

from .base import StaffFixture

EXTRA_KEYS = (
    "school.roles.view", "school.roles.assign", "school.roles.update",
    "academics.classes.update",
)


class _Fixture(StaffFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        for key in EXTRA_KEYS:
            if key not in cls.permissions:
                make_role_permission(cls.role, make_permission(key))
        cls.sule_grant = TenantUserRoleAssignment.objects.get(
            user=cls.ikeja_teacher.user, role=cls.teacher_role,
        )
        SchoolClass.all_objects.filter(pk=cls.ikeja_class.pk).update(
            class_teacher=cls.ikeja_teacher,
        )

    def rbac(self, method, name, body=None, **kwargs):
        return getattr(self, method)(
            self.admin, name, body, tenant_slug=self.tenant.slug, **kwargs,
        )


class RoleChangeTests(_Fixture):
    def moved_to_lekki(self):
        """Sule now also holds the Teacher role at Lekki, so losing Ikeja leaves him Lekki only."""
        make_assignment(self.school, self.ikeja_teacher.user, self.teacher_role, branch=self.lekki)

    def test_revoking_the_branch_of_a_class_they_lead_is_refused(self):
        self.moved_to_lekki()
        response = self.rbac(
            "post", "rbac-assignment-revoke", {"reason_note": "Moving to Lekki"},
            id=self.sule_grant.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "TEACHER_STILL_HOLDS_CLASSES")
        self.assertEqual(
            response.data["message"],
            "Ibrahim Sule still teaches JSS1 C at Ikeja. Hand it to someone there first.",
        )
        self.sule_grant.refresh_from_db()
        self.assertEqual(self.sule_grant.assignment_status, "ACTIVE")

    def test_revoking_goes_through_once_the_class_is_handed_over(self):
        self.moved_to_lekki()
        SchoolClass.all_objects.filter(pk=self.ikeja_class.pk).update(class_teacher=None)
        response = self.rbac(
            "post", "rbac-assignment-revoke", {"reason_note": "Moving to Lekki"},
            id=self.sule_grant.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_subject_duty_counts_as_well(self):
        self.moved_to_lekki()
        SchoolClass.all_objects.filter(pk=self.ikeja_class.pk).update(class_teacher=None)
        TeachingAssignment.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.ikeja_class,
            subject=self.maths, staff=self.ikeja_teacher, part="LEAD",
        )
        response = self.rbac(
            "post", "rbac-assignment-revoke", {"reason_note": "Moving to Lekki"},
            id=self.sule_grant.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(
            response.data["error"]["detail"]["held"], {"Ikeja": {"JSS1 C": ["Mathematics"]}},
        )

    def test_moving_the_grant_to_another_branch_is_refused(self):
        response = self.rbac(
            "patch", "rbac-assignment-detail", {"branch": self.lekki.pk},
            id=self.sule_grant.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.sule_grant.refresh_from_db()
        self.assertEqual(self.sule_grant.branch_id, self.ikeja.pk)

    def test_replacing_the_role_with_one_at_another_branch_is_refused(self):
        with self.assertRaises(TeacherStillHoldsClasses):
            grant_role(
                tenant=self.tenant, user=self.ikeja_teacher.user, role=self.teacher_role,
                branch=self.lekki, actor=self.admin, replaces=self.sule_grant,
            )
        self.sule_grant.refresh_from_db()
        self.assertEqual(self.sule_grant.assignment_status, "ACTIVE")
        self.assertFalse(TenantUserRoleAssignment.objects.filter(
            user=self.ikeja_teacher.user, branch=self.lekki,
        ).exists())

    def test_a_role_change_that_keeps_the_branch_records_who_and_why(self):
        outcome = grant_role(
            tenant=self.tenant, user=self.ikeja_teacher.user, role=self.teacher_role,
            branch=self.ikeja, actor=self.admin, replaces=self.sule_grant,
            reason_note="Same branch, fresh grant.",
        )
        self.assertIsNotNone(outcome.assignment)
        self.sule_grant.refresh_from_db()
        self.assertEqual(self.sule_grant.assignment_status, "REVOKED")
        self.assertEqual(self.sule_grant.revoked_by_id, self.admin.pk)
        self.assertIn("Same branch, fresh grant.", self.sule_grant.reason_note)

    def test_a_change_approved_after_the_class_was_given_records_apply_failed(self):
        SchoolClass.all_objects.filter(pk=self.ikeja_class.pk).update(class_teacher=None)
        request = raise_role_grant_request(
            tenant=self.tenant, requested_by=self.admin, user=self.ikeja_teacher.user,
            role=self.teacher_role, branch=self.lekki, replaces=self.sule_grant,
        )
        # JSS1 C is given to Sule while the request waits.
        SchoolClass.all_objects.filter(pk=self.ikeja_class.pk).update(
            class_teacher=self.ikeja_teacher,
        )
        decided = apply_role_grant_request(request, reviewer=self.admin)
        self.assertEqual(decided.status, TenantRoleGrantRequest.Status.APPLY_FAILED)
        self.assertIn("still teaches JSS1 C", decided.reviewer_notes)
        self.sule_grant.refresh_from_db()
        self.assertEqual(self.sule_grant.assignment_status, "ACTIVE")

    def test_narrowing_the_teacher_role_to_one_branch_is_refused(self):
        response = self.rbac(
            "patch", "rbac-role-detail", {"branch_ids": [self.lekki.pk], "reason": "Lekki only"},
            key="teacher",
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "TEACHER_STILL_HOLDS_CLASSES")

    def test_a_one_branch_school_never_asks(self):
        solo_grant = make_assignment(
            self.solo, self.solo_staff.user, self.solo_teacher_role, branch=self.solo_branch,
        )
        solo_grant.revoke(by_user=self.solo_admin)
        solo_grant.save()
        from vs_rbac.reach_guards import assert_reach_kept

        assert_reach_kept(self.solo.tenant, [self.solo_staff.user])


class ClassMoveTests(_Fixture):
    def move(self, branch):
        return self.patch(
            self.admin, "academics-class-detail",
            {"branch": branch.pk if branch else None}, pk=self.ikeja_class.pk,
        )

    def test_moving_a_class_away_from_its_teachers_branch_is_refused(self):
        response = self.move(self.lekki)
        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "CLASS_TEACHERS_NOT_AT_BRANCH")
        self.assertEqual(
            response.data["message"],
            "Ibrahim Sule does not work at Lekki. Give them a role there first, "
            "or hand JSS1 C to someone who does.",
        )
        self.ikeja_class.refresh_from_db()
        self.assertEqual(self.ikeja_class.branch_id, self.ikeja.pk)

    def test_moving_it_goes_through_when_its_teacher_works_at_both(self):
        make_assignment(self.school, self.ikeja_teacher.user, self.teacher_role, branch=self.lekki)
        response = self.move(self.lekki)
        self.assertEqual(response.status_code, 200, response.data)

    def test_making_it_school_wide_is_not_asked(self):
        response = self.move(None)
        self.assertEqual(response.status_code, 200, response.data)


class ListingCommandTests(_Fixture):
    def test_it_lists_a_class_held_outside_the_teachers_branches(self):
        TeachingAssignment.all_objects.create(
            tenant=self.tenant, session=self.year, school_class=self.ikeja_class,
            subject=self.english, staff=self.eze, part="LEAD",
        )
        out = StringIO()
        call_command("teachers_outside_their_branch", tenant="brightfield", stdout=out)
        self.assertIn(
            "brightfield: Chukwuemeka Eze - JSS1 C at Ikeja (English Language)", out.getvalue(),
        )
        self.assertIn("1 teacher hold classes outside their branches.", out.getvalue())
