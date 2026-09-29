"""A member of staff's day is their branch's.

Brightfield keeps Lagos time, and its Ikeja branch keeps Nairobi's. At 21:30
UTC on 14 March it is 22:30 on the 14th at Lekki, which follows the school,
and 00:30 on the 15th at Ikeja. Amaka (Lekki) and Chidi (Ikeja) both have
approved leave ending on the 14th: Chidi is back, Amaka is away until
midnight.

The school's clock is patched at ``vs_config.clock.tenant_now``, the one
place every "today" is read from, and a branch reads that same instant in its
own zone.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from unittest import mock

from schools.vs_staff.constants import LeaveStatus
from schools.vs_staff.models import LeaveRequest, StaffProfile
from schools.vs_staff.services.leave import on_leave_expression, on_leave_today
from vs_config.clock import TIME_ZONE_KEY, forget_tenant_zone, tenant_zone
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_tenants.context import reset_current_tenant, set_current_tenant

from .base import StaffFixture

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


class StaffBranchClockTests(StaffFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.amaka = cls.make_staff("amaka@brightfield.test", "Amaka", "Obi", branch=cls.lekki)
        cls.chidi = cls.make_staff("chidi@brightfield.test", "Chidi", "Eze", branch=cls.ikeja)
        cls.leave = {
            person.pk: LeaveRequest.all_objects.create(
                tenant=cls.tenant, staff=person, leave_type="ANNUAL",
                start_date=dt.date(2026, 3, 10), end_date=LAGOS_DAY,
                days=5, status=LeaveStatus.APPROVED,
            )
            for person in (cls.amaka, cls.chidi)
        }

    def setUp(self):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY),
            value="Africa/Nairobi", actor=None, branch=self.ikeja,
        )
        forget_tenant_zone(self.tenant)

    def test_leave_ending_on_the_14th_is_over_at_ikeja_and_running_at_lekki(self):
        with at_instant():
            away = on_leave_today(self.tenant)
        self.assertIn(self.amaka.pk, away)
        self.assertNotIn(self.chidi.pk, away)

    def test_the_directory_expression_agrees_with_the_set(self):
        token = set_current_tenant(self.tenant)
        try:
            with at_instant():
                flags = dict(
                    StaffProfile.all_objects.filter(pk__in=[self.amaka.pk, self.chidi.pk])
                    .annotate(away=on_leave_expression())
                    .values_list("pk", "away"),
                )
        finally:
            reset_current_tenant(token)
        self.assertEqual(flags, {self.amaka.pk: True, self.chidi.pk: False})

    def test_the_leave_reads_completed_on_its_persons_branch_day(self):
        with at_instant():
            self.assertEqual(self.leave[self.chidi.pk].display_status(), "COMPLETED")
            self.assertEqual(self.leave[self.amaka.pk].display_status(), LeaveStatus.APPROVED)

    def test_without_a_branch_zone_both_are_away_as_before(self):
        from vs_config.models import ConfigurationValue

        ConfigurationValue.all_objects.filter(branch=self.ikeja).delete()
        forget_tenant_zone(self.tenant)
        with at_instant():
            away = on_leave_today(self.tenant)
        self.assertTrue({self.amaka.pk, self.chidi.pk} <= away)
