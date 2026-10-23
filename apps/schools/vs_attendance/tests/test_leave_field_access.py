"""What a register says about somebody's leave follows the leave switches.

A staff register locks a person who is on approved leave and, for a reader who
holds ``school.leave.view``, says which leave it is: the request, its type and
the day it ends. A class register names the teacher it was taken on behalf of
and, when that teacher is away, the same type and last day. Both are facts of
the leave request, so a role whose ``school.leave`` type or dates switch is off
reads that the person is on leave and nothing about which kind or until when.

Brightfield has two branches and Sunrise one; the rule is the same at both.
"""
from __future__ import annotations

import datetime as dt

from schools.vs_staff.models import LeaveRequest
from vs_rbac.models import TenantUserRoleAssignment
from vs_rbac.tests.helpers import install_declared_fields, set_field_access

from .base import TODAY
from .staff_base import StaffFixture

LEAVE_TYPE = "school.leave.leave_type"
LEAVE_DATES = "school.leave.dates"


class _LeaveSwitches(StaffFixture):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields("school.leave")
        cls.end = TODAY + dt.timedelta(days=10)
        cls.leave = cls.approve_leave(cls.funke, TODAY - dt.timedelta(days=2), cls.end)
        cls.solo_leave = cls.approve_leave(cls.halima, TODAY - dt.timedelta(days=2), cls.end)
        cls.solo_role = TenantUserRoleAssignment.objects.get(user=cls.solo_registrar).role

    def close(self, role, *keys):
        set_field_access(role, *keys, read=False, write=False)


class StaffRegisterLeaveTests(_LeaveSwitches):
    def leave_of(self, user, person, branch):
        response = self.staff_day(user)
        self.assertEqual(response.status_code, 200, response.data)
        return self.staff_rows(response, branch)[person.pk]["leave"]

    def test_with_every_switch_open_the_row_names_the_type_and_the_last_day(self):
        self.assertEqual(self.leave_of(self.registrar, self.funke, self.lekki), {
            "id": self.leave.pk, "type": "MATERNITY", "type_label": "Maternity",
            "end_date": self.end,
        })
        self.assertEqual(self.leave_of(self.solo_registrar, self.halima, self.solo_branch), {
            "id": self.solo_leave.pk, "type": "MATERNITY", "type_label": "Maternity",
            "end_date": self.end,
        })

    def test_a_role_that_may_not_read_the_type_is_not_told_it(self):
        self.close(self.staff_role, LEAVE_TYPE)
        self.close(self.solo_role, LEAVE_TYPE)

        for user, person, branch in (
            (self.registrar, self.funke, self.lekki),
            (self.solo_registrar, self.halima, self.solo_branch),
        ):
            with self.subTest(user=user.email):
                leave = self.leave_of(user, person, branch)
                self.assertNotIn("type", leave)
                self.assertNotIn("type_label", leave)
                self.assertEqual(leave["end_date"], self.end)

    def test_a_role_that_may_not_read_the_dates_is_not_told_the_last_day(self):
        self.close(self.staff_role, LEAVE_DATES)

        leave = self.leave_of(self.registrar, self.funke, self.lekki)

        self.assertNotIn("end_date", leave)
        self.assertEqual(leave["type_label"], "Maternity")

    def test_the_people_with_no_home_branch_follow_the_same_rule(self):
        self.approve_leave(self.adaeze, TODAY - dt.timedelta(days=2), TODAY + dt.timedelta(days=3))
        self.close(self.staff_role, LEAVE_TYPE, LEAVE_DATES)

        row = self.data(self.staff_day(self.registrar))["no_home_branch"]["rows"][0]

        self.assertEqual(set(row["leave"]), {"id"})
        self.assertTrue(row["locked"])

    def test_a_write_that_re_reads_the_group_obeys_the_same_switches(self):
        self.close(self.staff_role, LEAVE_TYPE, LEAVE_DATES)

        response = self.mark_staff(self.registrar, self.lekki, self.peter, "P")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(self.staff_rows(response, self.lekki)[self.funke.pk]["leave"]), {"id"})


class ClassRegisterLeaveTests(_LeaveSwitches):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.class_leave = LeaveRequest.all_objects.create(
            tenant=cls.tenant, staff=cls.eze_staff, leave_type="SICK", days=3,
            start_date=dt.date(2025, 10, 13), end_date=dt.date(2025, 10, 15),
            status="APPROVED",
        )

    def on_leave(self):
        self.fill(self.admin, self.jss1a)
        self.submit(self.admin, self.jss1a)
        return self.data(self.read(self.admin, self.jss1a))["taken_by"]["on_leave"]

    def test_with_every_switch_open_the_register_names_the_type_and_the_last_day(self):
        leave = self.on_leave()

        self.assertEqual(leave["leave_type_label"], "Sick")
        self.assertEqual(str(leave["ends_on"]), "2025-10-15")

    def test_a_role_that_may_not_read_the_type_is_not_told_it(self):
        self.close(self.admin_role, LEAVE_TYPE)

        leave = self.on_leave()

        self.assertNotIn("leave_type", leave)
        self.assertNotIn("leave_type_label", leave)
        self.assertEqual(str(leave["ends_on"]), "2025-10-15")

    def test_a_role_that_may_not_read_the_dates_is_not_told_the_last_day(self):
        self.close(self.admin_role, LEAVE_DATES)

        leave = self.on_leave()

        self.assertNotIn("ends_on", leave)
        self.assertEqual(leave["leave_type_label"], "Sick")


class OwnLeaveOnTheRegistersTests(_LeaveSwitches):
    """A person reads their own leave on a register whatever their role's switches say."""

    def test_the_owner_reads_their_own_row_whole_and_a_colleagues_cut(self):
        from vs_rbac.tests.helpers import make_assignment

        make_assignment(self.tenant, self.funke.user, self.staff_role, branch=None)
        self.approve_leave(self.peter, TODAY - dt.timedelta(days=2), self.end)
        self.close(self.staff_role, LEAVE_TYPE, LEAVE_DATES)

        response = self.staff_day(self.funke.user)
        self.assertEqual(response.status_code, 200, response.data)
        rows = self.staff_rows(response, self.lekki)

        self.assertEqual(rows[self.funke.pk]["leave"]["type_label"], "Maternity")
        self.assertEqual(rows[self.funke.pk]["leave"]["end_date"], self.end)
        self.assertEqual(set(rows[self.peter.pk]["leave"]), {"id"})

    def test_the_teacher_a_class_register_was_taken_for_reads_their_own_leave(self):
        LeaveRequest.all_objects.create(
            tenant=self.tenant, staff=self.eze_staff, leave_type="SICK", days=3,
            start_date=dt.date(2025, 10, 13), end_date=dt.date(2025, 10, 15),
            status="APPROVED",
        )
        self.fill(self.admin, self.jss1a)
        self.submit(self.admin, self.jss1a)
        self.close(self.teacher_role, LEAVE_TYPE, LEAVE_DATES)
        self.close(self.admin_role, LEAVE_TYPE, LEAVE_DATES)

        own = self.data(self.read(self.eze, self.jss1a))["taken_by"]["on_leave"]
        other = self.data(self.read(self.admin, self.jss1a))["taken_by"]["on_leave"]

        self.assertEqual(own["leave_type_label"], "Sick")
        self.assertEqual(str(own["ends_on"]), "2025-10-15")
        self.assertEqual(other, {})
