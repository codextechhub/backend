"""Who may reach what, and what a refusal says when they may not.

Security first, because the ship-check asks for it first and because these are
the failures nobody notices until somebody reads another school's roster.

Three rules run through the file. **Another school's row answers 404, never
403**, and never with wording that says the person does not exist: a 403
confirms the row is there, and the same address may legitimately be an account
somewhere else. **The branch narrowing is inclusive**, so a branch admin sees
their own people plus the school-wide ones and not another branch's. And **a
person always reaches their own record**, whatever they hold.

FRD M12 v2.1 section 12.1.
"""
from __future__ import annotations

from schools.vs_staff.constants import EmploymentStatus

from .base import StaffFixture


class KeyEnforcementTests(StaffFixture):
    def test_a_caller_with_no_keys_is_refused_the_directory(self):
        response = self.get(self.nobody, "staff-list")
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_teacher_can_read_the_directory(self):
        """The defect this module's first change fixed.

        The live endpoint was gated on ``school.administrators.view``, which a
        teacher does not hold, while ``school.teachers.view`` was granted to
        teachers and reached nothing. So the key a school's role builder showed
        as "can see staff records" opened no screen at all.
        """
        response = self.get(self.eze.user, "staff-list")
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_teacher_cannot_create_staff(self):
        response = self.post(self.eze.user, "staff-list", self.invite_body())
        self.assertEqual(response.status_code, 403, response.data)

    def test_the_lifecycle_needs_its_own_key(self):
        """``manage`` is not implied by ``update``.

        Terminating somebody is not a branch decision and not an edit, which is
        why it is the one SENSITIVE key on the resource.
        """
        response = self.post(
            self.eze.user, "staff-status", {"to_status": "SUSPENDED"},
            pk=self.registrar.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_holding_manage_does_not_let_you_suspend_an_account(self):
        """The two vocabularies have two key families and neither implies the other.

        A caller holding every staff key and none of the account keys can move
        somebody to SUSPENDED on their record, and cannot suspend their login
        directly. Asserted because a single key covering both would be the
        shortcut somebody takes later.
        """
        from vs_rbac.models import TenantRolePermission

        TenantRolePermission.objects.filter(
            role=self.role,
            permission__key="school.administrators.suspend",
        ).delete()
        response = self.post(
            self.admin, "staff-account-suspend", pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)

    def test_leave_view_is_not_granted_to_a_teacher(self):
        """Who is off sick is not something every colleague may read."""
        response = self.get(self.eze.user, "staff-leave", pk=self.registrar.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_teacher_may_apply_for_their_own_leave(self):
        response = self.post(
            self.eze.user, "staff-leave",
            {
                "leave_type": "ANNUAL",
                "start_date": "2025-12-22",
                "end_date": "2026-01-02",
            },
            pk=self.eze.pk,
        )
        # 201 or a template refusal, but never 403: the key is held.
        self.assertNotEqual(response.status_code, 403, response.data)


class CrossTenantTests(StaffFixture):
    """Another school's ids, on every route that takes one."""

    def test_another_schools_person_is_404_on_the_record(self):
        response = self.get(self.admin, "staff-detail", pk=self.solo_staff.pk)
        self.assertEqual(response.status_code, 404, response.data)

    def test_the_404_does_not_say_the_person_does_not_exist(self):
        """Wording matters as much as the code here.

        The same address may legitimately be an account at another school, and
        a refusal that said "this user does not exist" would be both wrong and
        a disclosure of the opposite fact.
        """
        response = self.get(self.admin, "staff-detail", pk=self.solo_staff.pk)
        self.assertIn("at this school", str(response.data).lower())

    def test_every_child_route_answers_404_for_another_schools_person(self):
        for name in (
            "staff-detail", "staff-history", "staff-roles",
            "staff-qualifications", "staff-documents",
        ):
            with self.subTest(route=name):
                response = self.get(self.admin, name, pk=self.solo_staff.pk)
                self.assertEqual(response.status_code, 404, name)

    def test_another_schools_person_cannot_be_moved(self):
        response = self.post(
            self.admin, "staff-bulk-posting",
            {"staff_ids": [self.solo_staff.pk], "branch": self.lekki.pk},
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_bulk_move_naming_one_foreign_id_moves_nobody(self):
        """Resolved before anything is written, so a partial bulk never happens."""
        before = self.eze.branch_id
        self.post(
            self.admin, "staff-bulk-posting",
            {
                "staff_ids": [self.eze.pk, self.solo_staff.pk],
                "branch": self.ikeja.pk,
            },
        )
        self.eze.refresh_from_db()
        self.assertEqual(self.eze.branch_id, before)

    def test_a_bulk_role_grant_naming_one_foreign_id_grants_nothing(self):
        from vs_rbac.models import TenantUserRoleAssignment

        before = TenantUserRoleAssignment.objects.filter(tenant=self.tenant).count()
        response = self.post(
            self.admin, "staff-bulk-role",
            {
                "staff_ids": [self.registrar.pk, self.solo_staff.pk],
                "role": "teacher",
            },
        )
        self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(
            TenantUserRoleAssignment.objects.filter(tenant=self.tenant).count(),
            before,
        )


class BranchIsolationTests(StaffFixture):
    """The narrowing is inclusive, which is the whole difference from students."""

    def test_a_branch_admin_sees_their_branch_and_the_school_wide_people(self):
        response = self.get(self.lekki_head, "staff-list")
        self.assertEqual(response.status_code, 200, response.data)
        names = {row["full_name"] for row in response.data["data"]}
        self.assertIn("Chukwuemeka Eze", names)
        self.assertIn("Adaeze Nwankwo", names, "school-wide people belong to every branch")

    def test_a_branch_admin_does_not_see_another_branchs_people(self):
        response = self.get(self.lekki_head, "staff-list")
        names = {row["full_name"] for row in response.data["data"]}
        self.assertNotIn("Ibrahim Sule", names)

    def test_a_branch_admin_reaching_another_branchs_person_gets_404(self):
        response = self.get(
            self.lekki_head, "staff-detail", pk=self.ikeja_teacher.pk,
        )
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_person_with_no_posting_is_never_rendered_as_blank(self):
        """A null posting means school-wide, and is a real answer.

        Rendering it as blank or "Not set" would tell a school its registrar had
        incomplete data, when what she actually has is the whole school.
        """
        response = self.get(self.admin, "staff-detail", pk=self.registrar.pk)
        self.assertEqual(response.data["data"]["branch_name"], "School-wide")
        self.assertTrue(response.data["data"]["posted_school_wide"])

    def test_the_search_finds_nobody_the_directory_would_hide(self):
        response = self.get(self.lekki_head, "staff-search", {"q": "Sule"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"], [])

    def test_a_search_shorter_than_two_characters_returns_nothing(self):
        response = self.get(self.admin, "staff-search", {"q": "a"})
        self.assertEqual(response.data["data"], [])

    def test_the_search_carries_no_email_address(self):
        """The palette is the most casually visible surface in the module."""
        response = self.get(self.admin, "staff-search", {"q": "Eze"})
        self.assertTrue(response.data["data"])
        for row in response.data["data"]:
            self.assertNotIn("email", row)


class SelfServiceTests(StaffFixture):
    """A person always reaches their own record, and may change little of it."""

    def test_somebody_holding_nothing_reads_their_own_record(self):
        staff = self.make_staff(
            "quiet@brightfield.test", "Quiet", "Person", branch=self.lekki,
        )
        response = self.get(staff.user, "staff-detail", pk=staff.pk)
        self.assertEqual(response.status_code, 200, response.data)

    def test_somebody_holding_nothing_cannot_read_a_colleague(self):
        staff = self.make_staff(
            "quiet2@brightfield.test", "Quiet", "Two", branch=self.lekki,
        )
        response = self.get(staff.user, "staff-detail", pk=self.eze.pk)
        self.assertEqual(response.status_code, 403, response.data)

    def test_a_person_may_change_their_own_phone(self):
        response = self.patch(
            self.eze.user, "staff-detail", {"phone": "0803 555 9999"},
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_person_may_not_change_their_own_hire_date(self):
        """Editing your own hire date is editing your own tenure.

        Refused with 422 naming the field rather than a bare 403, so the form
        can show it where the reader has to change it.
        """
        response = self.patch(
            self.eze.user, "staff-detail", {"hire_date": "2015-01-01"},
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)
        self.assertIn("hire_date", str(response.data))
        self.assertEqual(
            response.data["error"]["code"], "FIELD_NOT_SELF_EDITABLE",
        )

    def test_a_person_may_not_give_themselves_a_job_title(self):
        response = self.patch(
            self.eze.user, "staff-detail", {"job_title": "Head Teacher"},
            pk=self.eze.pk,
        )
        self.assertEqual(response.status_code, 422, response.data)

    def test_a_person_reads_their_own_documents_holding_nothing(self):
        staff = self.make_staff(
            "quiet3@brightfield.test", "Quiet", "Three", branch=self.lekki,
        )
        response = self.get(staff.user, "staff-documents", pk=staff.pk)
        self.assertEqual(response.status_code, 200, response.data)

    def test_a_person_may_not_add_to_their_own_qualifications(self):
        """A qualification somebody types about themselves is a claim."""
        staff = self.make_staff(
            "quiet4@brightfield.test", "Quiet", "Four", branch=self.lekki,
        )
        response = self.post(
            staff.user, "staff-qualifications",
            {"qualification": "PhD"}, pk=staff.pk,
        )
        self.assertEqual(response.status_code, 403, response.data)


class PermissionOverrideVisibilityTests(StaffFixture):
    def test_a_caller_without_the_override_key_sees_no_override_block(self):
        """Absent, not empty.

        An empty block says "there are none here", which is exactly the fact the
        restriction exists to withhold: a person must not be able to learn that
        exceptions exist on their own account.
        """
        from vs_rbac.models import TenantRolePermission

        TenantRolePermission.objects.filter(
            role=self.role, permission__key="school.user_overrides.view",
        ).delete()
        response = self.get(self.admin, "staff-roles", pk=self.eze.pk)
        self.assertIsNone(response.data["data"]["overrides"])

    def test_a_school_admin_holding_the_key_sees_the_block(self):
        response = self.get(self.admin, "staff-roles", pk=self.eze.pk)
        self.assertEqual(response.data["data"]["overrides"], [])


class NobodyEndsTheirOwnEmploymentTests(StaffFixture):
    """A school administrator is on the staff list, and cannot use it on herself.

    Every move the lifecycle offers from Active either closes the login or ends
    the job, and Terminated closes it for good: "cannot be reopened without
    CodeX". At a school with one administrator there is nobody left to undo
    either, so the school would be locked out of its own system by one
    confirmation dialog. Somebody genuinely leaving is recorded by a colleague,
    which is who would have to do it once they had gone anyway.
    """

    def setUp(self):
        super().setUp()
        self.head = self.make_staff(
            "grace@brightfield.test", "Grace", "Okonkwo", branch=None,
            job_title="IT Head", role=self.role,
        )

    def test_terminating_yourself_is_refused(self):
        response = self.post(
            self.head.user, "staff-status",
            {"to_status": "TERMINATED", "reason": "Leaving",
             "last_working_day": "2026-12-18"},
            pk=self.head.pk,
        )

        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "CANNOT_ACT_ON_SELF")
        self.head.refresh_from_db()
        self.assertEqual(self.head.employment_status, EmploymentStatus.ACTIVE)

    def test_suspending_your_own_account_is_refused(self):
        response = self.post(
            self.head.user, "staff-account-suspend", pk=self.head.pk,
        )

        self.assertEqual(response.status_code, 422, response.data)
        self.assertEqual(response.data["error"]["code"], "CANNOT_ACT_ON_SELF")
        self.head.user.refresh_from_db()
        self.assertEqual(self.head.user.status, "ACTIVE")

    def test_the_drawer_offers_no_moves_on_your_own_record(self):
        """Read from the same rule the POST enforces.

        A list of moves the API will refuse is a drawer contradicting the
        server, and the reader finds out by pressing the button.
        """
        response = self.get(self.head.user, "staff-status", pk=self.head.pk)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["options"], [])
        # And says why, because an empty list otherwise reads as a record that
        # has been closed.
        self.assertIn("your own", response.data["data"]["note"])

    def test_the_same_person_may_still_do_it_to_a_colleague(self):
        """The guard is about who the record belongs to, not about the key."""
        response = self.post(
            self.head.user, "staff-status",
            {"to_status": "SUSPENDED", "reason": "Pending a review"},
            pk=self.registrar.pk,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.registrar.refresh_from_db()
        self.assertEqual(self.registrar.employment_status, EmploymentStatus.SUSPENDED)
