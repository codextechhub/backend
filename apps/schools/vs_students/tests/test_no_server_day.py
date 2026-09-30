"""A student's dates are always named, and named on the branch's day.

The enrolment date, a placement's effective date and a status change's
effective date have no column default: the only default a model can have is
the server's UTC day, which is the day before for the first hour after
midnight in Lagos. A write that names no day is refused by the database
(``IntegrityError``, the column is NOT NULL). At 23:30 UTC on 14 March the UTC
day is the 14th, and it is already the 15th at Brightfield's Ikeja branch,
which keeps Nairobi time; that is the day a status change there records.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.db import IntegrityError, transaction

from schools.vs_students.constants import Gender, StudentStatus
from schools.vs_students.models import ClassEnrolment, Student, StudentStatusLog
from schools.vs_students.services.status import transition
from vs_config.clock import TIME_ZONE_KEY, forget_tenant_zone
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value

from .base import StudentsFixture

#: The 14th in UTC, the 15th in Lagos and in Nairobi.
LATE_EVENING_UTC = dt.datetime(2026, 3, 14, 23, 30, tzinfo=dt.timezone.utc)


class NoServerDayTests(StudentsFixture):

    def test_a_student_without_an_enrolment_date_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Student.all_objects.create(
                tenant=self.tenant, branch=self.lekki, first_name="Ada",
                last_name="Obi", date_of_birth=dt.date(2013, 4, 18),
                gender=Gender.FEMALE, status=StudentStatus.ACTIVE,
            )

    def test_a_placement_without_an_effective_date_is_refused(self):
        student = self.student()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClassEnrolment.all_objects.create(
                tenant=self.tenant, student=student,
                school_class=self.shared_class, session=self.year, is_active=True,
            )

    def test_a_status_change_without_an_effective_date_is_refused(self):
        student = self.student()
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentStatusLog.all_objects.create(
                tenant=self.tenant, student=student,
                from_status=StudentStatus.ACTIVE, to_status=StudentStatus.SUSPENDED,
            )

    def test_a_status_change_at_a_nairobi_branch_after_utc_midnight_less_one_hour(self):
        """Recorded on the branch's day (the 15th), not the server's (the 14th)."""
        set_value(
            definition=ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY),
            value="Africa/Nairobi", actor=None, branch=self.ikeja,
        )
        forget_tenant_zone(self.tenant)
        student = self.student(branch=self.ikeja)
        with mock.patch("django.utils.timezone.now", return_value=LATE_EVENING_UTC):
            transition(student, StudentStatus.SUSPENDED, actor=self.admin, reason="Conduct")
        log = StudentStatusLog.all_objects.get(student=student)
        self.assertEqual(log.effective_date, dt.date(2026, 3, 15))
        self.assertNotEqual(log.effective_date, LATE_EVENING_UTC.date())
