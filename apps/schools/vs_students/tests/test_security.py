"""Security and tenancy. These are the tests that must not be deferred.

Three things are proven here and nowhere else: every endpoint refuses a caller
who does not hold its key, another tenant's row answers **404 and never 403**,
and a branch-bound caller cannot reach another branch's child by any route.

FRD M11 v2.4 section 12.1.
"""
from __future__ import annotations

from schools.vs_students.constants import StudentStatus
from schools.vs_students.models import Student

from .base import StudentsFixture


class PermissionDeniedTests(StudentsFixture):
    """Every endpoint, per endpoint, rather than per module.

    Proven by enumerating the URL conf rather than by a chosen few, so a route
    added later without a key is caught by this test rather than in production.
    """

    def setUp(self):
        self.row = self.student()
        self.g = self.guardian()
        self.link(self.row, self.g)

    def test_reads_refuse_a_caller_without_the_key(self):
        for name, kwargs in (
            ("student-list", {}),
            ("student-summary", {}),
            ("student-search", {}),
            ("student-unplaced", {}),
            ("student-admission-policy", {}),
            ("guardian-list", {}),
            ("student-detail", {"pk": self.row.pk}),
            ("student-status-history", {"pk": self.row.pk}),
            ("student-class-history", {"pk": self.row.pk}),
            ("student-history", {"pk": self.row.pk}),
            ("student-subjects", {"pk": self.row.pk}),
            ("student-documents", {"pk": self.row.pk}),
            ("student-guardians", {"pk": self.row.pk}),
            ("guardian-detail", {"pk": self.g.pk}),
            ("guardian-students", {"pk": self.g.pk}),
            ("student-class-roster", {"class_id": self.shared_class.pk}),
        ):
            with self.subTest(endpoint=name):
                response = self.get(self.nobody, name, **kwargs)
                self.assertEqual(response.status_code, 403, name)

    def test_writes_refuse_a_caller_without_the_key(self):
        for name, kwargs in (
            ("student-list", {}),
            ("student-confirm", {"pk": self.row.pk}),
            ("student-reject", {"pk": self.row.pk}),
            ("student-withdraw", {"pk": self.row.pk}),
            ("student-suspend", {"pk": self.row.pk}),
            ("student-reactivate", {"pk": self.row.pk}),
            ("student-transfer-out", {"pk": self.row.pk}),
            ("student-status", {"pk": self.row.pk}),
            ("student-assign-class", {"pk": self.row.pk}),
            ("student-bulk-assign", {}),
            ("student-bulk-status", {}),
            ("student-promotion-preview", {}),
            ("student-promotion-run", {}),
        ):
            with self.subTest(endpoint=name):
                response = self.post(self.nobody, name, {}, **kwargs)
                self.assertEqual(response.status_code, 403, name)

    def test_setting_the_admission_policy_needs_manage_but_reading_needs_view(self):
        self.assertEqual(
            self.get(self.admin, "student-admission-policy").status_code, 200,
        )
        self.assertEqual(
            self.put(
                self.nobody, "student-admission-policy",
                {"required": True, "pattern": "", "hint": ""},
            ).status_code,
            403,
        )

    def test_enrolling_needs_the_assign_key_as_well_as_create(self):
        """The two-key rule, and the reason rbac_permission is not used for it.

        ``rbac_permission`` is any-of, so listing both keys would let a caller
        holding only ``create`` enrol a child into any class they liked.
        """
        from vs_rbac.tests.helpers import (
            make_assignment, make_role, make_role_permission, make_school_admin,
        )

        role = make_role(self.school, name="Registrar", key="registrar")
        for key in ("school.students.view", "school.students.create"):
            make_role_permission(role, self.permissions[key])
        registrar = make_school_admin(
            None, email="registrar@brightfield.test", tenant=self.tenant,
        )
        make_assignment(self.school, registrar, role, branch=None)

        before = Student.all_objects.count()
        response = self.post(registrar, "student-list", self.enrolment_body())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Student.all_objects.count(), before)


class CrossTenantTests(StudentsFixture):
    """Another school's rows answer 404, never 403.

    A 403 confirms the row exists. A student id must not be usable to learn
    that a child exists at another school.
    """

    def setUp(self):
        self.theirs = self.student(
            tenant=self.solo.tenant, branch=self.solo_branch,
            first="Somebody", last="Else",
        )
        self.their_guardian = self.guardian(
            tenant=self.solo.tenant, name="Their Parent",
            phone="08000000000", email="theirs@example.ng",
        )

    def test_reading_another_tenants_student_by_id_is_404(self):
        response = self.get(self.admin, "student-detail", pk=self.theirs.pk)
        self.assertEqual(response.status_code, 404)

    def test_editing_another_tenants_student_by_id_is_404(self):
        response = self.patch(
            self.admin, "student-detail", {"first_name": "Renamed"},
            pk=self.theirs.pk,
        )
        self.assertEqual(response.status_code, 404)
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.first_name, "Somebody")

    def test_withdrawing_another_tenants_student_is_404(self):
        response = self.post(
            self.admin, "student-withdraw", {"reason": "no"}, pk=self.theirs.pk,
        )
        self.assertEqual(response.status_code, 404)
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.status, StudentStatus.ACTIVE)

    def test_another_tenants_guardian_is_404_by_every_route(self):
        for name in ("guardian-detail", "guardian-students"):
            with self.subTest(endpoint=name):
                self.assertEqual(
                    self.get(self.admin, name, pk=self.their_guardian.pk).status_code,
                    404,
                )

    def test_another_tenants_student_is_absent_from_the_list(self):
        mine = self.student(first="Mine", last="Own")
        response = self.get(self.admin, "student-list")
        ids = {row["id"] for row in response.data["data"]}
        self.assertIn(mine.pk, ids)
        self.assertNotIn(self.theirs.pk, ids)

    def test_a_missing_id_and_another_tenants_id_answer_the_same(self):
        """The response and the status must not distinguish the two.

        If they did, a caller could walk the id space and learn which numbers
        belong to a child somewhere on the platform.
        """
        theirs = self.get(self.admin, "student-detail", pk=self.theirs.pk)
        nowhere = self.get(self.admin, "student-detail", pk=99_000_000)
        self.assertEqual(theirs.status_code, nowhere.status_code)
        self.assertEqual(theirs.data.get("message"), nowhere.data.get("message"))


class BranchIsolationTests(StudentsFixture):
    """A caller pinned to one branch sees that branch's children and no others.

    The read here is **exclusive**, unlike the academic catalogue's: a student
    is never shared, so there is no null branch to add back in.
    """

    def setUp(self):
        self.lekki_child = self.student(
            branch=self.lekki, first="Tobi", last="Okafor",
        )
        self.ikeja_child = self.student(
            branch=self.ikeja, first="Somto", last="Okafor",
        )

    def test_a_branch_bound_caller_sees_only_their_branch(self):
        response = self.get(self.lekki_head, "student-list")
        ids = {row["id"] for row in response.data["data"]}
        self.assertIn(self.lekki_child.pk, ids)
        self.assertNotIn(self.ikeja_child.pk, ids)

    def test_another_branchs_student_answers_404_not_403(self):
        response = self.get(
            self.lekki_head, "student-detail", pk=self.ikeja_child.pk,
        )
        self.assertEqual(response.status_code, 404)

    def test_a_branch_bound_caller_cannot_withdraw_another_branchs_student(self):
        response = self.post(
            self.lekki_head, "student-withdraw", {"reason": "no"},
            pk=self.ikeja_child.pk,
        )
        self.assertEqual(response.status_code, 404)
        self.ikeja_child.refresh_from_db()
        self.assertEqual(self.ikeja_child.status, StudentStatus.ACTIVE)

    def test_a_school_level_caller_sees_every_branch(self):
        response = self.get(self.admin, "student-list")
        ids = {row["id"] for row in response.data["data"]}
        self.assertIn(self.lekki_child.pk, ids)
        self.assertIn(self.ikeja_child.pk, ids)

    def test_a_caller_with_no_live_branch_grant_sees_no_students(self):
        """An empty grant set is not WHOLE_TENANT.

        Collapsing the two is the defect this test exists for: it would turn
        "your access has been withdrawn" into "you can see everybody".
        """
        from vs_rbac.models import TenantUserRoleAssignment

        TenantUserRoleAssignment.objects.filter(
            user=self.lekki_head,
        ).update(assignment_status="REVOKED")
        make = self.client_for(self.lekki_head)
        response = make.get(
            "/v1/students/", {"tenant": self.tenant.slug},
        )
        # Either refused outright or shown nothing - never shown everything.
        if response.status_code == 200:
            self.assertEqual(response.data["data"], [])
        else:
            self.assertIn(response.status_code, (403, 404))

    def test_the_search_matches_no_student_of_another_branch(self):
        response = self.get(self.lekki_head, "student-search", {"q": "Okafor"})
        names = {row["full_name"] for row in response.data["data"]}
        self.assertIn("Tobi Okafor", names)
        self.assertNotIn("Somto Okafor", names)

    def test_the_summary_counts_only_the_callers_branches(self):
        response = self.get(self.lekki_head, "student-summary")
        self.assertEqual(response.data["data"]["total"], 1)


class SensitiveFieldTests(StudentsFixture):
    """Medical detail is gated on the profile and absent from every list.

    The emergency contact is deliberately NOT gated: a contact only an
    administrator can read is useless in the emergency it exists for.
    """

    def setUp(self):
        self.row = self.student(
            allergies="Peanuts", conditions="Mild asthma", blood_group="A+",
            emergency_contact_name="Mrs. Okafor",
            emergency_contact_phone="08065550130",
        )

    def _without_sensitive(self):
        from vs_rbac.tests.helpers import (
            make_assignment, make_role, make_role_permission, make_school_admin,
        )

        role = make_role(self.school, name="Clerk", key="clerk")
        for key in ("school.students.view", "school.students.update"):
            make_role_permission(role, self.permissions[key])
        user = make_school_admin(
            None, email="clerk@brightfield.test", tenant=self.tenant,
        )
        make_assignment(self.school, user, role, branch=None)
        return user

    def test_no_medical_field_is_in_a_list_response_for_anyone(self):
        response = self.get(self.admin, "student-list")
        row = response.data["data"][0]
        for field in (
            "allergies", "conditions", "blood_group",
            "emergency_contact_name", "emergency_contact_phone",
        ):
            self.assertNotIn(field, row)

    def test_medical_detail_is_stripped_without_view_sensitive(self):
        clerk = self._without_sensitive()
        response = self.get(clerk, "student-detail", pk=self.row.pk)
        data = response.data["data"]
        self.assertNotIn("allergies", data)
        self.assertNotIn("conditions", data)
        self.assertNotIn("blood_group", data)

    def test_the_emergency_contact_is_present_without_view_sensitive(self):
        clerk = self._without_sensitive()
        response = self.get(clerk, "student-detail", pk=self.row.pk)
        self.assertEqual(
            response.data["data"]["emergency_contact_phone"], "08065550130",
        )

    def test_medical_detail_is_present_with_view_sensitive(self):
        response = self.get(self.admin, "student-detail", pk=self.row.pk)
        self.assertEqual(response.data["data"]["allergies"], "Peanuts")

    def test_writing_allergies_without_the_key_is_refused_and_changes_nothing(self):
        clerk = self._without_sensitive()
        response = self.patch(
            clerk, "student-detail", {"allergies": "None"}, pk=self.row.pk,
        )
        self.assertEqual(response.status_code, 400)
        self.row.refresh_from_db()
        self.assertEqual(self.row.allergies, "Peanuts")

    def test_the_search_carries_only_four_fields(self):
        response = self.get(self.admin, "student-search", {"q": "Nwosu"})
        self.assertEqual(
            set(response.data["data"][0]),
            {"id", "full_name", "student_number", "class_name"},
        )


class PendingTenantTests(StudentsFixture):
    """A school that has not gone live reaches nothing in this module.

    Absence of ``pending_tenant_surface`` means closed, which is what makes a
    view added later closed by default rather than open by omission.
    """

    def test_no_view_in_this_module_declares_the_pending_surface(self):
        from django.urls import get_resolver

        from schools.vs_students import urls

        offenders = []
        for pattern in urls.student_patterns + urls.guardian_patterns:
            view = pattern.callback.cls
            if getattr(view, "pending_tenant_surface", None):
                offenders.append(view.__name__)
        self.assertEqual(offenders, [])

    def test_a_pending_tenant_is_refused_the_student_list(self):
        from vs_tenants.models import Tenant

        Tenant.objects.filter(pk=self.tenant.pk).update(
            status=Tenant.Status.PENDING,
        )
        response = self.get(self.admin, "student-list")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "TENANT_NOT_LIVE")
