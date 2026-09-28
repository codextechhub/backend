"""Which day "today" is on the calendar reads: the school's, not the server's.

The server keeps UTC. At 23:30 UTC on 5 January it is 00:30 on 6 January in
Lagos, the first day of Brightfield's second term, so the hub must already
say the second term is running. A school that has set Africa/Nairobi moves
over three hours earlier than UTC does.

Only the clock the calendar asks is moved, so the signed-in session itself is
checked against the real time.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest import mock

from vs_config.clock import TIME_ZONE_KEY
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value

from .base import _Base


def _school_clock_at(instant):
    return mock.patch("vs_config.clock.timezone", SimpleNamespace(now=lambda: instant))


class SchoolDayTests(_Base):
    def test_the_hub_turns_the_day_at_lagos_midnight_not_utc_midnight(self):
        late_evening = dt.datetime(2026, 1, 5, 23, 30, tzinfo=dt.timezone.utc)
        with _school_clock_at(late_evening):
            current = self.get(self.admin, "calendar-current").data["data"]
            overview = self.get(self.admin, "calendar-overview").data["data"]
        self.assertEqual(str(current["on"]), "2026-01-06")
        self.assertEqual(current["term"]["id"], self.second_term.pk)
        self.assertEqual(overview["term"]["id"], self.second_term.pk)
        self.assertEqual(overview["term"]["days_elapsed"], 1)

    def test_an_hour_earlier_it_is_still_the_holiday(self):
        before_midnight = dt.datetime(2026, 1, 5, 22, 30, tzinfo=dt.timezone.utc)
        with _school_clock_at(before_midnight):
            current = self.get(self.admin, "calendar-current").data["data"]
        self.assertEqual(str(current["on"]), "2026-01-05")

    def test_a_school_on_nairobi_time_reads_its_own_day(self):
        set_value(
            definition=ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY),
            value="Africa/Nairobi", actor=None, tenant=self.tenant,
        )
        evening = dt.datetime(2026, 1, 5, 21, 30, tzinfo=dt.timezone.utc)
        with _school_clock_at(evening):
            current = self.get(self.admin, "calendar-current").data["data"]
        self.assertEqual(str(current["on"]), "2026-01-06")
        self.assertEqual(current["term"]["id"], self.second_term.pk)

    def test_an_explicit_on_still_wins(self):
        late_evening = dt.datetime(2026, 1, 5, 23, 30, tzinfo=dt.timezone.utc)
        with _school_clock_at(late_evening):
            current = self.get(
                self.admin, "calendar-current", {"on": "2025-10-15"},
            ).data["data"]
        self.assertEqual(str(current["on"]), "2025-10-15")
        self.assertEqual(current["term"]["id"], self.first_term.pk)
