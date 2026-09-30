"""Times in the bell schedule's refusals follow the school's clock.

A new period that overlaps another is refused with the other period's times,
so the person knows what to move. A school on the 24-hour clock reads
"08:00 - 08:45"; a school that has chosen nothing reads the default 12-hour
clock, "8:00 am - 8:45 am".
"""
from __future__ import annotations

from vs_config.clock import forget_tenant_zone
from vs_config.display import CLOCK_KEY
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value

from .base import _Base


class PeriodOverlapFollowsTheSchoolsClockTests(_Base):
    def setUp(self):
        forget_tenant_zone(self.tenant)

    def clash(self):
        response = self.post(self.admin, "calendar-period-list", {
            "label": "Clash", "start_time": "08:30", "end_time": "09:00",
            "period_type": "LESSON",
        })
        self.assertEqual(response.status_code, 422, response.data)
        return response.data["message"]

    def test_the_overlap_names_the_other_periods_times_on_a_24_hour_clock(self):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=CLOCK_KEY),
            value="H24", actor=None, tenant=self.tenant,
        )
        forget_tenant_zone(self.tenant)
        self.assertIn("Period 1 (08:00 - 08:45)", self.clash())

    def test_the_overlap_at_a_school_that_chose_nothing_reads_the_12_hour_clock(self):
        self.assertIn("Period 1 (8:00 am - 8:45 am)", self.clash())
