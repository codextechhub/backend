"""Dates in an export follow the school's display settings.

Harbour Academy writes dates DD/MM/YYYY on a 24-hour clock, in Lagos. A
people-mode file, a filter sentence, a schedule description and the expiry
refusal all read that way; a system-mode file keeps ISO dates and UTC times,
because an importer is written once. A file is named for the day it is at the
school, so an export run at 23:30 UTC (00:30 in Lagos) carries the next day.
"""
import datetime
from unittest import mock

from django.test import TestCase

from vs_config.clock import forget_tenant_zone
from vs_config.display import CLOCK_KEY, DATE_FORMAT_KEY
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_tenants.models import Tenant

from .catalogue import KIND_DATE, KIND_DATETIME, KIND_TIME, render_value
from .constants import DownloadRefusal, Recurrence, ValuesMode
from .models import ExportDefinition, ExportFile, ExportRun, ExportSchedule
from .scheduling import describe
from .views import _refusal_message

UTC = datetime.timezone.utc
#: 00:30 on 15 March in Lagos.
LATE_EVENING_UTC = datetime.datetime(2026, 3, 14, 23, 30, tzinfo=UTC)


class ExportDatesFollowTheSchoolTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = Tenant.objects.create(
            name="Harbour Academy", slug="harbour-academy-exports",
            kind=Tenant.Kind.ORGANIZATION,
        )
        set_value(
            definition=ConfigurationDefinition.objects.get(key=DATE_FORMAT_KEY),
            value="DD_MM_YYYY", actor=None, tenant=cls.school,
        )
        set_value(
            definition=ConfigurationDefinition.objects.get(key=CLOCK_KEY),
            value="H24", actor=None, tenant=cls.school,
        )

    def setUp(self):
        forget_tenant_zone(self.school)

    def test_a_people_cell_is_the_schools_and_a_system_cell_is_iso(self):
        day = datetime.date(2026, 7, 26)
        self.assertEqual(render_value(KIND_DATE, day, ValuesMode.PEOPLE, tenant=self.school), "26/07/2026")
        self.assertEqual(render_value(KIND_DATE, day, ValuesMode.SYSTEM, tenant=self.school), "2026-07-26")
        self.assertEqual(
            render_value(KIND_DATETIME, LATE_EVENING_UTC, ValuesMode.PEOPLE, tenant=self.school),
            "15/03/2026, 00:30",
        )
        self.assertEqual(
            render_value(KIND_DATETIME, LATE_EVENING_UTC, ValuesMode.SYSTEM, tenant=self.school),
            "2026-03-14T23:30:00",
        )

    def test_a_wall_time_is_on_the_schools_clock_for_people_only(self):
        bell = datetime.time(8, 5)
        self.assertEqual(render_value(KIND_TIME, bell, ValuesMode.PEOPLE, tenant=self.school), "08:05")
        self.assertEqual(render_value(KIND_TIME, bell, ValuesMode.SYSTEM, tenant=self.school), "08:05:00")
        self.assertEqual(render_value(KIND_TIME, bell, ValuesMode.PEOPLE, tenant=None), "8:05 am")

    def test_a_download_is_named_for_the_day_at_the_school(self):
        definition = ExportDefinition(tenant=self.school, file_name_pattern="invoices-{date}")
        with mock.patch("django.utils.timezone.now", return_value=LATE_EVENING_UTC):
            self.assertEqual(definition.render_file_name(), "invoices-2026-03-15")

    def test_the_schedule_reads_back_in_the_schools_words_and_keeps_its_zone(self):
        definition = ExportDefinition(tenant=self.school)
        once = ExportSchedule(
            definition=definition, recurrence=Recurrence.ONCE, at_time=datetime.time(15, 0),
            timezone_name="Africa/Nairobi", starts_on=datetime.date(2026, 10, 1),
        )
        self.assertIn("runs once on 01/10/2026 at 15:00 (Africa/Nairobi)", describe(once))
        daily = ExportSchedule(
            definition=definition, recurrence=Recurrence.DAILY, at_time=datetime.time(3, 0),
            timezone_name="Africa/Lagos", starts_on=datetime.date(2026, 10, 1),
            ends_on=datetime.date(2026, 12, 31),
        )
        self.assertIn(
            "runs every day at 03:00 (Africa/Lagos), starting 01/10/2026, ending 31/12/2026",
            describe(daily),
        )

    def test_the_expiry_refusal_dates_the_instant_at_the_school(self):
        file = ExportFile(run=ExportRun(tenant=self.school), available_until=LATE_EVENING_UTC)
        self.assertIn("on 15/03/2026.", _refusal_message(DownloadRefusal.EXPIRED, file))
