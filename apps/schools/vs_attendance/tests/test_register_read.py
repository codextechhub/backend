"""Reading a class's register for one day.

The security cases come first (a caller without the key, another school's class,
another branch's class), then the shape of the document at each stage of the
day, in a school with several branches and in a school with one.
"""
from __future__ import annotations

import datetime as dt

from core.test_utils import TenantAPIClient

from .base import (
    FOUNDERS_DAY,
    LAST_FRIDAY,
    LEKKI_OPEN_DAY,
    TODAY,
    AttendanceFixture,
)


class RegisterAccessTests(AttendanceFixture):
    def test_a_caller_without_the_view_key_is_refused(self):
        self.assertEqual(self.read(self.nobody, self.jss1a).status_code, 403)

    def test_another_schools_class_is_a_404_not_a_403(self):
        """Sunrise's class id, asked for by Brightfield's registrar, does not exist."""
        response = self.read(self.admin, self.solo_class)
        self.assertEqual(response.status_code, 404)

    def test_a_class_in_a_branch_the_caller_cannot_see_is_a_404(self):
        self.assertEqual(self.read(self.lekki_admin, self.sss2).status_code, 404)
        self.assertEqual(self.read(self.ikeja_admin, self.jss1a).status_code, 404)

    def test_a_class_in_the_callers_own_branch_opens(self):
        self.assertEqual(self.read(self.lekki_admin, self.jss1a).status_code, 200)
        self.assertEqual(self.read(self.ikeja_admin, self.sss2).status_code, 200)

    def test_a_malformed_date_is_a_400_with_a_sentence(self):
        response = TenantAPIClient(self.admin).get(
            self.url("attendance-register", self.jss1a, "13-10-2025"),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.code(response), "INVALID_DATE")

    def test_an_unknown_class_is_a_404(self):
        response = TenantAPIClient(self.admin).get(
            f"/v1/attendance/registers/999999/{TODAY}/",
        )
        self.assertEqual(response.status_code, 404)


class BranchOfTheRegisterTests(AttendanceFixture):
    def test_a_shared_class_at_a_school_with_several_branches_is_refused(self):
        response = self.read(self.admin, self.shared)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.code(response), "CLASS_HAS_NO_BRANCH")
        self.assertEqual(
            response.data["message"],
            "Give this class a branch before taking its register.",
        )

    def test_a_shared_class_at_a_school_with_one_branch_belongs_to_that_branch(self):
        response = self.read(self.solo_admin, self.solo_class)
        self.assertEqual(response.status_code, 200, response.data)
        body = self.data(response)
        self.assertEqual(body["school_class"]["name"], "Basic 3")
        self.assertEqual([r["name"] for r in body["rows"]], ["Ebube Okoro"])

    def test_the_branch_is_named_at_a_school_with_several_and_absent_at_one(self):
        several = self.data(self.read(self.admin, self.jss1a))["school_class"]
        self.assertEqual(several["branch"]["name"], "Lekki Branch")
        solo = self.data(self.read(self.solo_admin, self.solo_class))["school_class"]
        self.assertIsNone(solo["branch"])


class RegisterShapeTests(AttendanceFixture):
    def test_an_untouched_register_lists_the_roster_with_nothing_marked(self):
        body = self.data(self.read(self.admin, self.jss1a))
        self.assertEqual(body["status"], "NOT_STARTED")
        self.assertIsNone(body["closed"])
        self.assertIsNone(body["taken_by"])
        self.assertEqual(body["school_class"]["class_teacher_name"], "Chidi Eze")
        self.assertEqual(
            [row["name"] for row in body["rows"]],
            ["Tunde Bello", "Kelechi Eze", "Chiamaka Nwosu", "Amina Yusuf"],
        )
        self.assertEqual(
            body["counts"],
            {"total": 3, "present": 0, "late": 0, "absent": 0, "excused": 0,
             "unmarked": 3, "locked": 1},
        )

    def test_the_suspended_child_is_listed_locked_and_counted_nowhere(self):
        rows = {row["name"]: row for row in self.data(self.read(self.admin, self.jss1a))["rows"]}
        amina = rows["Amina Yusuf"]
        self.assertTrue(amina["locked"])
        self.assertIsNone(amina["mark"])
        self.assertFalse(rows["Tunde Bello"]["locked"])

    def test_a_child_placed_later_is_not_listed(self):
        names = [r["name"] for r in self.data(self.read(self.admin, self.jss1a))["rows"]]
        self.assertNotIn("Obinna Okafor", names)

    def test_the_status_follows_the_marks(self):
        self.assertEqual(
            self.mark(self.admin, self.jss1a, self.tunde, "P").status_code, 200,
        )
        self.assertEqual(self.data(self.read(self.admin, self.jss1a))["status"], "IN_PROGRESS")
        self.fill(self.admin, self.jss1a)
        self.assertEqual(self.data(self.read(self.admin, self.jss1a))["status"], "READY")
        self.submit(self.admin, self.jss1a)
        self.assertEqual(self.data(self.read(self.admin, self.jss1a))["status"], "SUBMITTED")

    def test_late_counts_as_present_and_is_counted_on_its_own(self):
        self.mark(self.admin, self.jss1a, self.tunde, "L", arrived_at="08:20")
        self.mark(self.admin, self.jss1a, self.kelechi, "P")
        self.mark(self.admin, self.jss1a, self.chiamaka, "A")
        counts = self.data(self.read(self.admin, self.jss1a))["counts"]
        self.assertEqual(counts["present"], 2)
        self.assertEqual(counts["late"], 1)
        self.assertEqual(counts["absent"], 1)
        self.assertEqual(counts["unmarked"], 0)

    def test_the_arrival_time_is_written_as_hours_and_minutes(self):
        self.mark(self.admin, self.jss1a, self.tunde, "L", arrived_at="08:20:45")
        row = self.rows(self.read(self.admin, self.jss1a))[self.tunde.pk]
        self.assertEqual(row["arrived_at"], "08:20")

    def test_a_class_with_nobody_placed_says_there_is_no_register_to_take(self):
        body = self.data(self.read(self.admin, self.pry4a))
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["history"], [])
        self.assertTrue(body["roster_empty"])
        self.assertEqual(body["status"], "NOT_STARTED")
        self.assertEqual(body["counts"]["total"], 0)

    def test_an_empty_roster_is_a_list_and_not_an_object(self):
        body = self.data(self.read(self.admin, self.pry4a))
        self.assertIsInstance(body["rows"], list)
        self.assertIsInstance(body["history"], list)

    def test_a_submitted_register_travels_as_json(self):
        """Dates, times and instants nested in the body survive the renderer."""
        import json

        self.mark(self.eze, self.jss1a, self.tunde, "L", arrived_at="08:20")
        self.fill(self.eze, self.jss1a)
        self.submit(self.eze, self.jss1a)
        self.mark(self.eze, self.jss1a, self.tunde, "P")
        body = json.loads(self.read(self.eze, self.jss1a).content)["data"]
        self.assertEqual(body["date"], "2025-10-13")
        self.assertEqual(body["date_label"], "13 Oct 2025")
        self.assertIsInstance(
            dt.datetime.fromisoformat(body["taken_by"]["submitted_at"].replace("Z", "+00:00")),
            dt.datetime,
        )
        self.assertEqual(body["history"][0]["from_mark"], "L")
        self.assertEqual(
            [row["arrived_at"] for row in body["rows"] if row["name"] == "Tunde Bello"],
            [None],
        )

    def test_a_register_read_creates_nothing(self):
        from ..models import ClassRegister

        self.read(self.admin, self.jss1a)
        self.assertFalse(ClassRegister.objects.filter(school_class=self.jss1a).exists())


class ClosedDayTests(AttendanceFixture):
    def test_a_school_wide_closure_is_carried_with_its_reason(self):
        body = self.data(self.read(self.admin, self.jss1a, FOUNDERS_DAY))
        self.assertEqual(body["status"], "CLOSED_DAY")
        self.assertEqual(body["closed"], {"reason": "Founders' Day", "code": "CLOSED_BY_EVENT"})
        self.assertEqual(body["rows"], [])

    def test_a_branch_closure_closes_that_branchs_classes_and_not_the_others(self):
        self.assertEqual(
            self.data(self.read(self.admin, self.jss1a, LEKKI_OPEN_DAY))["status"], "CLOSED_DAY",
        )
        self.assertNotEqual(
            self.data(self.read(self.admin, self.sss2, LEKKI_OPEN_DAY))["status"], "CLOSED_DAY",
        )

    def test_a_weekend_is_closed(self):
        body = self.data(self.read(self.admin, self.jss1a, dt.date(2025, 10, 11)))
        self.assertEqual(body["closed"]["code"], "NOT_TEACHING_DAY")

    def test_an_ordinary_past_day_is_open(self):
        self.assertIsNone(self.data(self.read(self.admin, self.jss1a, LAST_FRIDAY))["closed"])


class ReadOnlyTests(AttendanceFixture):
    def test_a_view_only_caller_reads_but_cannot_mark(self):
        from vs_rbac.tests.helpers import make_assignment, make_school_admin

        from ..constants import PERM_REGISTER_VIEW
        from .base import make_key_role

        role = make_key_role(self.tenant, "Viewer", "viewer", (PERM_REGISTER_VIEW,))
        viewer = make_school_admin(None, email="viewer@brightfield.test", tenant=self.tenant)
        make_assignment(self.tenant, viewer, role, branch=None)

        self.assertEqual(self.read(viewer, self.jss1a).status_code, 200)
        self.assertEqual(self.mark(viewer, self.jss1a, self.tunde, "P").status_code, 403)
        self.assertEqual(self.submit(viewer, self.jss1a).status_code, 403)
