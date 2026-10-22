"""The names-only staff directory behind every staff picker.

Brightfield's finance officer may raise expense claims and nothing in the staff
module, and still needs to pick Chukwuemeka Eze as a claim's claimant. The
names directory gives her each active member of staff's account id and name and
nothing more. A parent and a pupil of the school, a member of staff whose role
picks nobody, and Sunrise's head (another school) are each kept out of
Brightfield's list.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_staff.constants import EmploymentStatus
from schools.vs_staff.views.names import PICKER_KEYS
from schools.vs_students.constants import Gender, Relationship, StudentStatus
from schools.vs_students.models import Guardian, Student, StudentGuardian
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
)

from .base import StaffFixture

NAMES = "staff-names"


class StaffNamesTests(StaffFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.pickers = {}
        for key in PICKER_KEYS:
            permission = make_permission(key, scope=PermissionScope.TENANT)
            slug = key.replace(".", "_")
            role = make_role(cls.school, name=f"Picks with {key}", key=f"picks_{slug}")
            make_role_permission(role, permission)
            user = make_school_admin(None, email=f"{slug}@brightfield.test", tenant=cls.tenant)
            make_assignment(cls.school, user, role, branch=cls.lekki)
            cls.pickers[key] = user
        cls.finance = cls.pickers["finance.expenseclaim.create"]

        # A parent and a pupil of Brightfield, each with a sign-in and no role.
        pupil = Student.all_objects.create(
            tenant=cls.tenant, branch=cls.lekki, first_name="Amaka", last_name="Eze",
            student_number="BFS/0001", date_of_birth=dt.date(2013, 4, 18),
            gender=Gender.FEMALE, status=StudentStatus.ACTIVE,
            enrolment_date=dt.date(2025, 9, 8),
            user=make_school_admin(None, email="amaka@pupils.brightfield.test", tenant=cls.tenant),
        )
        cls.pupil = pupil.user
        cls.parent = make_school_admin(None, email="mrs.eze@parents.test", tenant=cls.tenant)
        guardian = Guardian.all_objects.create(
            tenant=cls.tenant, full_name="Mrs Eze", email="mrs.eze@parents.test",
            phone="08035550101", user=cls.parent,
        )
        StudentGuardian.all_objects.create(
            tenant=cls.tenant, student=pupil, guardian=guardian,
            relationship=Relationship.MOTHER, is_primary=True,
        )

        cls.resigned = cls.make_staff(
            "gone@brightfield.test", "Kunle", "Bello", branch=cls.ikeja,
            status=EmploymentStatus.RESIGNED,
        )
        cls.suspended = cls.make_staff("off@brightfield.test", "Tayo", "Odu", branch=cls.lekki)
        cls.suspended.user.status = "SUSPENDED"
        cls.suspended.user.save(update_fields=["status"])
        cls.solo_finance = make_school_admin(None, email="finance@sunrise.test",
                                             tenant=cls.solo.tenant)
        solo_role = make_role(cls.solo, name="Claims", key="claims")
        make_role_permission(solo_role, make_permission("finance.expenseclaim.create",
                                                        scope=PermissionScope.TENANT))
        make_assignment(cls.solo, cls.solo_finance, solo_role, branch=None)

    def names(self, user, **params):
        response = self.get(user, NAMES, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_every_picker_key_opens_the_list(self):
        for key, user in self.pickers.items():
            with self.subTest(key=key):
                self.assertIn(
                    {"id": self.eze.user_id, "name": "Chukwuemeka Eze"}, self.names(user),
                )

    def test_a_parent_a_pupil_and_a_role_that_picks_nobody_are_refused(self):
        for who in (self.parent, self.pupil, self.nobody, self.eze.user):
            with self.subTest(who=who.email):
                self.assertEqual(self.get(who, NAMES).status_code, 403)

    def test_each_row_is_an_account_id_and_a_name_and_nothing_else(self):
        rows = self.names(self.finance)

        self.assertEqual({tuple(sorted(row)) for row in rows}, {("id", "name")})
        self.assertNotIn("@", str(rows))
        self.assertNotIn("BFS/STF", str(rows))

    def test_only_the_schools_active_staff_are_listed(self):
        ids = {row["id"] for row in self.names(self.finance)}

        self.assertTrue({self.eze.user_id, self.ikeja_teacher.user_id,
                         self.registrar.user_id} <= ids)
        for absent in (self.resigned.user_id, self.suspended.user_id, self.parent.pk,
                       self.pupil.pk, self.solo_staff.user_id):
            self.assertNotIn(absent, ids)

    def test_another_school_sees_only_its_own_staff(self):
        self.assertEqual(self.names(self.solo_finance),
                         [{"id": self.solo_staff.user_id, "name": "Bola Ade"}])

    def test_a_search_matches_names_and_never_an_email_or_staff_number(self):
        self.assertEqual([r["id"] for r in self.names(self.finance, q="Chukwu")],
                         [self.eze.user_id])
        self.assertEqual(self.names(self.finance, q="brightfield.test"), [])
        self.assertEqual(self.names(self.finance, q="BFS/STF/0012"), [])

    def test_nothing_found_is_an_empty_list(self):
        response = self.get(self.finance, NAMES, {"q": "Zzyzx"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"], [])
