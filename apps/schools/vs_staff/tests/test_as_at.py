"""Reading a staff profile as it stood at the end of an earlier day.

The recorder's clock is patched for each change, so every test reads the
profile before and after a change and proves each tab answers with what it
held on that day: the record, the account's name, the roles, the
qualifications and the leave.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from unittest import mock

from schools.vs_staff.constants import LeaveStatus, LeaveType
from schools.vs_staff.models import LeaveRequest, StaffQualification
from vs_history.as_at import RECORD_DAY_TIMEZONE

from .base import StaffFixture


def _at(month, day, hour=10):
    return dt.datetime(2026, month, day, hour, tzinfo=RECORD_DAY_TIMEZONE)


@contextmanager
def recorded_on(month, day):
    with mock.patch("vs_history.recorder.timezone.now", return_value=_at(month, day)):
        yield


class StaffAsAtTests(StaffFixture):
    def setUp(self):
        with recorded_on(3, 1):
            self.person = self.make_staff(
                "bola@brightfield.test", "Bola", "Ade", branch=self.lekki,
                role=self.teacher_role,
            )

    def as_at(self, name, day, user=None, **kwargs):
        return self.get(user or self.admin, name, {"as_at": day}, **kwargs)

    def test_the_live_record_names_the_day_its_history_starts(self):
        data = self.get(self.admin, "staff-detail", pk=self.person.pk).data["data"]
        self.assertEqual(data["history_starts"], "2026-03-01")
        self.assertEqual((data["first_name"], data["last_name"]), ("Bola", "Ade"))

    def test_the_name_and_job_title_read_as_they_were(self):
        with recorded_on(3, 10):
            self.person.user.last_name = "Adeyemi"
            self.person.user.save(update_fields=["last_name"])
            self.person.job_title = "Head of Science"
            self.person.save()
        before = self.as_at("staff-detail", "2026-03-05", pk=self.person.pk).data["data"]
        after = self.as_at("staff-detail", "2026-03-10", pk=self.person.pk).data["data"]
        self.assertEqual(before["full_name"], "Bola Ade")
        self.assertEqual(before["job_title"], "Teacher")
        self.assertEqual(before["as_at"]["date"], "2026-03-05")
        self.assertEqual(after["full_name"], "Bola Adeyemi")
        self.assertEqual(after["job_title"], "Head of Science")

    def test_a_role_revoked_later_is_held_on_the_earlier_day(self):
        grant = self.person.user.tenant_role_assignments.get()
        with recorded_on(3, 12):
            grant.assignment_status = "REVOKED"
            grant.revoked_at = _at(3, 12)
            grant.save()
        before = self.as_at("staff-roles", "2026-03-05", pk=self.person.pk).data["data"]
        after = self.as_at("staff-roles", "2026-03-12", pk=self.person.pk).data["data"]
        self.assertEqual([row["role"] for row in before["roles"]], ["Teacher"])
        self.assertEqual(before["revoked"], [])
        self.assertEqual(after["roles"], [])
        self.assertEqual([row["role"] for row in after["revoked"]], ["Teacher"])

    def test_a_qualification_removed_later_is_listed_on_the_earlier_day(self):
        with recorded_on(3, 2):
            row = StaffQualification.all_objects.create(
                tenant=self.tenant, staff=self.person, qualification="B.Sc Physics",
                institution="UNILAG", year_obtained=2015,
            )
        with recorded_on(3, 20):
            row.delete()
        before = self.as_at("staff-qualifications", "2026-03-05", pk=self.person.pk).data["data"]
        after = self.as_at("staff-qualifications", "2026-03-21", pk=self.person.pk).data["data"]
        self.assertEqual([q["qualification"] for q in before], ["B.Sc Physics"])
        self.assertEqual(after, [])

    def test_leave_reads_as_it_stood_and_the_day_decides_on_leave(self):
        with recorded_on(3, 2):
            leave = LeaveRequest.all_objects.create(
                tenant=self.tenant, staff=self.person, leave_type=LeaveType.ANNUAL,
                start_date=dt.date(2026, 3, 4), end_date=dt.date(2026, 3, 6), days=3,
                status=LeaveStatus.PENDING,
            )
        with recorded_on(3, 3):
            leave.status = LeaveStatus.APPROVED
            leave.save()
        pending = self.as_at("staff-leave", "2026-03-02", pk=self.person.pk).data["data"]
        running = self.as_at("staff-detail", "2026-03-05", pk=self.person.pk).data["data"]
        self.assertEqual(pending["leave"][0]["status"], LeaveStatus.PENDING)
        self.assertEqual(pending["days_taken"], [])
        self.assertTrue(running["on_leave_today"])
        self.assertEqual(running["on_leave_until"], dt.date(2026, 3, 6))

    def test_a_day_before_the_history_starts_is_refused(self):
        response = self.as_at("staff-detail", "2026-02-20", pk=self.person.pk)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["error"]["detail"]["history_starts"], "2026-03-01")

    def test_another_school_cannot_read_the_record_at_any_date(self):
        response = self.as_at("staff-detail", "2026-03-05", user=self.solo_admin, pk=self.person.pk)
        self.assertEqual(response.status_code, 404)

    def test_a_branch_head_cannot_read_another_branch_at_any_date(self):
        with recorded_on(3, 1):
            other = self.make_staff("ikeja2@brightfield.test", "Kemi", "Oni", branch=self.ikeja)
        response = self.as_at("staff-detail", "2026-03-05", user=self.lekki_head, pk=other.pk)
        self.assertEqual(response.status_code, 404)
