"""Dates in the sentences the staff record writes follow the school's settings.

Brightfield writes its dates DD/MM/YYYY. The warning that a leave request
overlaps another names the other request's days, and the refusal to end an
appointment before it began names the day it began; both are read by a person
at the moment they act, so both are written the school's way. The numeric
formats print a span with both dates in full, and the default format shortens
it ("27 - 31 Oct 2025").
"""
from __future__ import annotations

import datetime as dt

from schools.vs_staff.exceptions import NotEligibleForPost
from schools.vs_staff.models import LeaveRequest, StaffPositionAssignment
from schools.vs_staff.services.leave import _overlap_warning
from schools.vs_staff.services.organogram import StaffOrganogramService
from vs_config.clock import forget_tenant_zone
from vs_config.display import DATE_FORMAT_KEY
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value

from .base import StaffFixture


class StaffSentencesFollowTheSchoolsDateFormatTests(StaffFixture):
    def setUp(self):
        forget_tenant_zone(self.tenant)

    def use_day_first(self):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=DATE_FORMAT_KEY),
            value="DD_MM_YYYY", actor=None, tenant=self.tenant,
        )
        forget_tenant_zone(self.tenant)

    def annual(self, start, end):
        return LeaveRequest(
            tenant=self.tenant, staff=self.eze, leave_type="ANNUAL",
            start_date=start, end_date=end, days=5,
        )

    def test_the_overlap_warning_names_each_clash_the_schools_way(self):
        self.use_day_first()
        warning = _overlap_warning(
            [self.annual(dt.date(2025, 10, 27), dt.date(2025, 10, 31))], self.tenant,
        )
        self.assertEqual(warning["code"], "LEAVE_OVERLAP")
        self.assertEqual(
            warning["message"],
            "This overlaps leave already recorded for this person: "
            "Annual 27/10/2025 - 31/10/2025",
        )

    def test_the_overlap_warning_at_a_school_that_chose_nothing_shortens_the_span(self):
        warning = _overlap_warning(
            [self.annual(dt.date(2025, 10, 27), dt.date(2025, 10, 31))], self.tenant,
        )
        self.assertTrue(warning["message"].endswith("Annual 27 - 31 Oct 2025"))

    def test_no_clash_is_no_warning(self):
        self.assertIsNone(_overlap_warning([], self.tenant))

    def test_ending_an_appointment_before_it_began_names_the_start_the_schools_way(self):
        self.use_day_first()
        appointment = StaffPositionAssignment(
            tenant=self.tenant, staff=self.eze, start_date=dt.date(2026, 3, 10),
        )
        with self.assertRaises(NotEligibleForPost) as refused:
            StaffOrganogramService.end_assignment(
                appointment, end_date=dt.date(2026, 3, 1),
            )
        self.assertIn("started on 10/03/2026", refused.exception.message)
