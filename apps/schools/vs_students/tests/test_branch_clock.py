"""A student's day is their branch's.

Brightfield keeps Lagos time, and its Ikeja branch keeps Nairobi's. At 21:30
UTC on 14 March it is 22:30 on the 14th in Lagos (and at Lekki, which follows
the school) and 00:30 on the 15th at Ikeja. Sunrise has one branch and keeps
the school's zone, so it is the proof that a school with nothing set sees no
change.

The school's clock is patched at ``vs_config.clock.tenant_now``, the one
place every "today" is read from, and a branch reads that same instant in its
own zone.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from unittest import mock

from schools.vs_students.constants import StudentStatus
from schools.vs_students.models import Student
from vs_config.clock import TIME_ZONE_KEY, forget_tenant_zone, tenant_zone
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value

from .base import StudentsFixture

#: 22:30 on 14 March in Lagos, 00:30 on 15 March in Nairobi.
INSTANT = dt.datetime(2026, 3, 14, 21, 30, tzinfo=dt.timezone.utc)
LAGOS_DAY = dt.date(2026, 3, 14)
NAIROBI_DAY = dt.date(2026, 3, 15)


@contextmanager
def at_instant():
    with mock.patch(
        "vs_config.clock.tenant_now",
        side_effect=lambda tenant: INSTANT.astimezone(tenant_zone(tenant)),
    ):
        yield


class StudentBranchClockTests(StudentsFixture):

    def setUp(self):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY),
            value="Africa/Nairobi", actor=None, branch=self.ikeja,
        )
        forget_tenant_zone(self.tenant)

    def enrol(self, branch, user=None, **overrides):
        with at_instant():
            response = self.post(
                user or self.admin, "student-list",
                self.enrolment_body(branch=str(branch.pk), **overrides),
            )
        self.assertEqual(response.status_code, 201, response.data)
        return Student.all_objects.get(pk=response.data["data"]["id"])

    def test_an_enrolment_with_no_date_is_dated_on_its_branchs_today(self):
        at_ikeja = self.enrol(self.ikeja)
        at_lekki = self.enrol(
            self.lekki, first_name="Musa", last_name="Bello",
            date_of_birth="2013-05-02",
        )
        self.assertEqual(at_ikeja.enrolment_date, NAIROBI_DAY)
        self.assertEqual(at_lekki.enrolment_date, LAGOS_DAY)

    def test_an_applicant_is_dated_on_its_branchs_today(self):
        applicant = self.enrol(
            self.ikeja, as_applicant=True, applied_for=self.jss1.pk, school_class=None,
        )
        self.assertEqual(applicant.status, StudentStatus.APPLICANT)
        self.assertEqual(applicant.applied_on, NAIROBI_DAY)

    def test_an_offer_ending_on_the_14th_has_lapsed_at_ikeja_only(self):
        ikeja = self.student(
            branch=self.ikeja, first="Tunde", status=StudentStatus.APPLICANT,
            offer_expires_on=LAGOS_DAY,
        )
        lekki = self.student(
            branch=self.lekki, first="Musa", status=StudentStatus.APPLICANT,
            offer_expires_on=LAGOS_DAY,
        )
        with at_instant():
            expired = {
                student.pk: self.get(self.admin, "student-detail", pk=student.pk)
                .data["data"]["offer_expired"]
                for student in (ikeja, lekki)
            }
        self.assertEqual(expired, {ikeja.pk: True, lekki.pk: False})

    def test_a_birthday_on_the_15th_has_come_at_ikeja_and_not_at_lekki(self):
        from schools.vs_students.serializers import _age_on

        born = dt.date(2013, 3, 15)
        with at_instant():
            self.assertEqual(_age_on(born, tenant=self.tenant, branch=self.ikeja.pk), 13)
            self.assertEqual(_age_on(born, tenant=self.tenant, branch=self.lekki.pk), 12)

    def test_a_single_branch_school_keeps_the_schools_day(self):
        from vs_config.clock import branch_today, tenant_today

        with at_instant():
            self.assertEqual(branch_today(self.solo.tenant, self.solo_branch), LAGOS_DAY)
            self.assertEqual(tenant_today(self.solo.tenant), LAGOS_DAY)
