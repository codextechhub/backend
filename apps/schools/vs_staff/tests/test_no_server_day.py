"""An appointment's start date is always named, and named on the posting's day.

The start date has no column default: the only default a model can have is the
server's UTC day. A write that names no day is refused by the database
(``IntegrityError``, the column is NOT NULL). At 23:30 UTC on 14 March the UTC
day is the 14th and it is already the 15th at Brightfield's Ikeja branch, which
keeps Nairobi time; an appointment there with no start date given starts on
the 15th.
"""
from __future__ import annotations

import datetime as dt
from unittest import mock

from django.db import IntegrityError, transaction

from schools.vs_staff.models import StaffOrgNode, StaffPosition, StaffPositionAssignment
from schools.vs_staff.services.organogram import StaffOrganogramService
from vs_config.clock import TIME_ZONE_KEY, forget_tenant_zone
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value

from .base import StaffFixture

#: The 14th in UTC, the 15th in Lagos and in Nairobi.
LATE_EVENING_UTC = dt.datetime(2026, 3, 14, 23, 30, tzinfo=dt.timezone.utc)


class NoServerDayTests(StaffFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.chidi = cls.make_staff("chidi@brightfield.test", "Chidi", "Eze", branch=cls.ikeja)
        node = StaffOrgNode.all_objects.create(
            tenant=cls.tenant, name="Ikeja Sciences", code="ISCI", kind="DEPARTMENT",
            branch=cls.ikeja,
        )
        cls.post = StaffPosition.all_objects.create(
            tenant=cls.tenant, title="Physics Teacher, Ikeja", code="IPHY", org_node=node,
        )

    def test_an_appointment_without_a_start_date_is_refused(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            StaffPositionAssignment.all_objects.create(
                tenant=self.tenant, staff=self.chidi, position=self.post,
            )

    def test_an_appointment_at_a_nairobi_branch_starts_on_the_branchs_day(self):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY),
            value="Africa/Nairobi", actor=None, branch=self.ikeja,
        )
        forget_tenant_zone(self.tenant)
        with mock.patch("django.utils.timezone.now", return_value=LATE_EVENING_UTC):
            appointment = StaffOrganogramService.assign_position(self.chidi, self.post)
        self.assertEqual(appointment.start_date, dt.date(2026, 3, 15))
        self.assertNotEqual(appointment.start_date, LATE_EVENING_UTC.date())
