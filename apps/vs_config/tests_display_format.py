"""``vs_config.display``: dates and times printed the way a school reads them.

Harbour School runs on Lagos time with two branches: Apapa follows the school
and Mombasa keeps Africa/Nairobi. 21:30 UTC on 14 March 2026 is 22:30 on the
14th in Lagos and already 00:30 on the 15th in Nairobi, so an instant then is
dated the 14th at Apapa and the 15th at Mombasa. 13:30 UTC on 29 September is
2:30 pm in Lagos. Every expected string is what ``school-fe``
``src/lib/dates.ts`` prints for the same value and settings.
"""
import datetime

from django.template import Context, Template
from django.test import TestCase

from vs_tenants.context import reset_current_tenant, set_current_tenant
from vs_tenants.models import Branch, Tenant

from .clock import TIME_ZONE_KEY, forget_tenant_zone
from .display import (
    CLOCK_KEY,
    DATE_FORMAT_KEY,
    DD_MM_YYYY,
    H24,
    YYYY_MM_DD,
    display_style,
    format_date,
    format_date_range,
    format_datetime,
    format_month,
    format_time,
    write_date,
)
from .models import ConfigurationDefinition
from .services.resolution import set_value

UTC = datetime.timezone.utc
#: 2:30 pm in Lagos, 4:30 pm in Nairobi.
AFTERNOON = datetime.datetime(2026, 9, 29, 13, 30, tzinfo=UTC)
#: The 14th in Lagos, the 15th in Nairobi.
NAIROBI_MIDNIGHT = datetime.datetime(2026, 3, 14, 21, 30, tzinfo=UTC)


class _School(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.zone = ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY)
        cls.date_format = ConfigurationDefinition.objects.get(key=DATE_FORMAT_KEY)
        cls.clock = ConfigurationDefinition.objects.get(key=CLOCK_KEY)
        cls.school = Tenant.objects.create(
            name="Harbour School", slug="harbour-print", kind=Tenant.Kind.ORGANIZATION,
        )
        cls.apapa = Branch.all_objects.create(
            tenant=cls.school, name="Apapa Branch", code=1, is_main=True,
        )
        cls.mombasa = Branch.all_objects.create(
            tenant=cls.school, name="Mombasa Branch", code=2,
        )
        set_value(definition=cls.zone, value="Africa/Nairobi", actor=None, branch=cls.mombasa)

    def setUp(self):
        forget_tenant_zone(self.school)

    def choose(self, *, date_format=None, clock=None):
        if date_format:
            set_value(definition=self.date_format, value=date_format, actor=None, tenant=self.school)
        if clock:
            set_value(definition=self.clock, value=clock, actor=None, tenant=self.school)
        forget_tenant_zone(self.school)


class EveryFormatAndClockTests(_School):
    """Each date format with each clock, for each kind of value."""

    CASES = {
        ("D_MMM_YYYY", "H12"): ("29 Sep 2026", "29 Sep 2026, 2:30 pm", "2:30 pm"),
        ("D_MMM_YYYY", "H24"): ("29 Sep 2026", "29 Sep 2026, 14:30", "14:30"),
        ("DD_MM_YYYY", "H12"): ("29/09/2026", "29/09/2026, 2:30 pm", "2:30 pm"),
        ("DD_MM_YYYY", "H24"): ("29/09/2026", "29/09/2026, 14:30", "14:30"),
        ("YYYY_MM_DD", "H12"): ("2026-09-29", "2026-09-29, 2:30 pm", "2:30 pm"),
        ("YYYY_MM_DD", "H24"): ("2026-09-29", "2026-09-29, 14:30", "14:30"),
    }

    def test_every_combination(self):
        for (date_format, clock), (day, stamp, time) in self.CASES.items():
            with self.subTest(date_format=date_format, clock=clock):
                self.choose(date_format=date_format, clock=clock)
                self.assertEqual(format_date(datetime.date(2026, 9, 29), self.school), day)
                self.assertEqual(format_date("2026-09-29", self.school), day)
                self.assertEqual(format_date(AFTERNOON, self.school), day)
                self.assertEqual(format_datetime(AFTERNOON, self.school), stamp)
                self.assertEqual(format_datetime(AFTERNOON.isoformat(), self.school), stamp)
                self.assertEqual(format_time(AFTERNOON, self.school), time)
                self.assertEqual(format_time(datetime.time(14, 30), self.school), time)
                self.assertEqual(format_time("14:30:00", self.school), time)
                self.assertEqual(format_month(AFTERNOON, self.school), "Sep 2026")

    def test_the_defaults_are_d_mmm_yyyy_and_a_twelve_hour_clock(self):
        self.assertEqual(format_datetime(AFTERNOON, self.school), "29 Sep 2026, 2:30 pm")
        self.assertEqual(format_date(datetime.date(2026, 3, 5), self.school), "5 Mar 2026")

    def test_twelve_hour_edges(self):
        for hour, minute, h12, h24 in (
            (0, 0, "12:00 am", "00:00"), (0, 5, "12:05 am", "00:05"),
            (8, 0, "8:00 am", "08:00"), (12, 0, "12:00 pm", "12:00"),
            (23, 59, "11:59 pm", "23:59"),
        ):
            with self.subTest(hour=hour, minute=minute):
                self.choose(clock="H12")
                self.assertEqual(format_time(datetime.time(hour, minute), self.school), h12)
                self.choose(clock=H24)
                self.assertEqual(format_time(datetime.time(hour, minute), self.school), h24)

    def test_a_month_is_named_in_every_format(self):
        for date_format in ("D_MMM_YYYY", DD_MM_YYYY, YYYY_MM_DD):
            with self.subTest(date_format=date_format):
                self.choose(date_format=date_format)
                self.assertEqual(format_month(datetime.date(2026, 9, 1), self.school), "Sep 2026")
                self.assertEqual(format_month("2026-09", self.school), "Sep 2026")
                self.assertEqual(format_month((2026, 9), self.school), "Sep 2026")
                self.assertEqual(
                    format_month(datetime.date(2026, 9, 1), self.school, month="long"),
                    "September 2026",
                )

    def test_the_options_the_screens_have(self):
        day = datetime.date(2026, 9, 29)
        self.assertEqual(format_date(day, self.school, year=False), "29 Sep")
        self.assertEqual(format_date(day, self.school, month="long"), "29 September 2026")
        self.assertEqual(format_date(day, self.school, weekday="long"), "Tuesday, 29 Sep 2026")
        self.assertEqual(format_date(day, self.school, weekday="short"), "Tue, 29 Sep 2026")
        self.assertEqual(write_date(day, DD_MM_YYYY, year=False), "29/09")
        self.assertEqual(write_date(day, YYYY_MM_DD, year=False), "09-29")

    def test_a_zone_abbreviation_for_a_reader_outside_the_school(self):
        self.assertEqual(
            format_datetime(AFTERNOON, self.school, with_zone=True), "29 Sep 2026, 2:30 pm WAT",
        )
        self.assertEqual(
            format_datetime(AFTERNOON, self.school, branch=self.mombasa, with_zone=True),
            "29 Sep 2026, 4:30 pm EAT",
        )

    def test_nothing_prints_as_blank_and_the_unreadable_as_written(self):
        for fn in (format_date, format_datetime, format_time, format_month):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(fn(None, self.school), "")
                self.assertEqual(fn("", self.school), "")
                self.assertEqual(fn("soon", self.school), "soon")
        self.assertEqual(format_date("2026-02-30", self.school), "2026-02-30")


class ZoneTests(_School):
    def test_a_calendar_date_is_never_shifted(self):
        """A due date is the same day at every branch and on every clock."""
        day = datetime.date(2026, 3, 14)
        for branch in (None, self.apapa, self.mombasa):
            with self.subTest(branch=branch):
                self.assertEqual(format_date(day, self.school, branch=branch), "14 Mar 2026")
                self.assertEqual(format_datetime(day, self.school, branch=branch), "14 Mar 2026")
                self.assertEqual(format_month(day, self.school, branch=branch), "Mar 2026")

    def test_an_instant_is_dated_at_its_branch(self):
        self.assertEqual(format_date(NAIROBI_MIDNIGHT, self.school), "14 Mar 2026")
        self.assertEqual(format_date(NAIROBI_MIDNIGHT, self.school, branch=self.apapa), "14 Mar 2026")
        self.assertEqual(format_date(NAIROBI_MIDNIGHT, self.school, branch=self.mombasa), "15 Mar 2026")
        self.assertEqual(
            format_datetime(NAIROBI_MIDNIGHT, self.school, branch=self.mombasa),
            "15 Mar 2026, 12:30 am",
        )
        self.assertEqual(
            format_datetime(NAIROBI_MIDNIGHT, self.school, branch=self.mombasa.pk),
            "15 Mar 2026, 12:30 am",
        )
        self.assertEqual(format_datetime(NAIROBI_MIDNIGHT, self.school), "14 Mar 2026, 10:30 pm")

    def test_a_month_turns_at_the_branchs_midnight(self):
        month_end = datetime.datetime(2026, 3, 31, 21, 30, tzinfo=UTC)
        self.assertEqual(format_month(month_end, self.school), "Mar 2026")
        self.assertEqual(format_month(month_end, self.school, branch=self.mombasa), "Apr 2026")

    def test_a_naive_datetime_is_utc(self):
        naive = datetime.datetime(2026, 9, 29, 13, 30)
        self.assertEqual(format_datetime(naive, self.school), "29 Sep 2026, 2:30 pm")
        self.assertEqual(format_datetime("2026-09-29T13:30:00", self.school), "29 Sep 2026, 2:30 pm")
        self.assertEqual(format_datetime("2026-09-29T13:30:00Z", self.school), "29 Sep 2026, 2:30 pm")

    def test_an_instant_already_in_another_zone_is_moved_to_the_schools(self):
        from zoneinfo import ZoneInfo

        in_london = AFTERNOON.astimezone(ZoneInfo("Europe/London"))
        self.assertEqual(format_datetime(in_london, self.school), "29 Sep 2026, 2:30 pm")

    def test_a_wall_time_takes_no_zone(self):
        self.assertEqual(format_time("08:00", self.school, branch=self.mombasa), "8:00 am")

    def test_none_and_the_platform_tenant_read_the_platform(self):
        set_value(definition=self.clock, value=H24, actor=None)
        platform = Tenant.objects.get(kind=Tenant.Kind.PLATFORM)
        self.assertEqual(format_datetime(AFTERNOON, None), "29 Sep 2026, 14:30")
        self.assertEqual(format_datetime(AFTERNOON, platform), "29 Sep 2026, 14:30")
        self.assertEqual(format_datetime(AFTERNOON, self.school), "29 Sep 2026, 14:30")


class RangeTests(_School):
    def test_the_shortenings(self):
        for start, end, expected in (
            ("2025-11-21", "2025-11-21", "21 Nov 2025"),
            ("2025-11-21", None, "21 Nov 2025"),
            ("2025-10-27", "2025-10-31", "27 - 31 Oct 2025"),
            ("2025-10-28", "2025-11-02", "28 Oct - 2 Nov 2025"),
            ("2025-12-19", "2026-01-02", "19 Dec 2025 - 2 Jan 2026"),
        ):
            with self.subTest(start=start, end=end):
                self.assertEqual(format_date_range(start, end, self.school), expected)

    def test_a_numeric_format_prints_both_dates_in_full(self):
        self.choose(date_format=DD_MM_YYYY)
        self.assertEqual(
            format_date_range(datetime.date(2025, 10, 27), datetime.date(2025, 10, 31), self.school),
            "27/10/2025 - 31/10/2025",
        )


class StyleMemoTests(_School):
    def test_the_style_is_read_once_per_tenant_until_forgotten(self):
        with self.assertNumQueries(2):
            display_style(self.school)
        with self.assertNumQueries(0):
            for _ in range(5):
                format_date(datetime.date(2026, 9, 29), self.school)
        set_value(definition=self.date_format, value=DD_MM_YYYY, actor=None, tenant=self.school)
        self.assertEqual(format_date(datetime.date(2026, 9, 29), self.school), "29 Sep 2026")
        forget_tenant_zone(self.school)
        self.assertEqual(format_date(datetime.date(2026, 9, 29), self.school), "29/09/2026")

    def test_a_rows_copy_of_the_request_tenant_shares_its_memo(self):
        token = set_current_tenant(self.school)
        try:
            display_style(self.school)
            row_copy = Tenant.objects.get(pk=self.school.pk)
            with self.assertNumQueries(0):
                display_style(row_copy)
        finally:
            reset_current_tenant(token)


class TemplateFilterTests(_School):
    def render(self, source, **context):
        return Template("{% load display_dates %}" + source).render(Context(context))

    def test_each_filter_with_a_tenant_or_a_branch(self):
        self.choose(clock=H24)
        self.assertEqual(
            self.render("{{ d|display_date:t }}", d=datetime.date(2026, 9, 29), t=self.school),
            "29 Sep 2026",
        )
        self.assertEqual(
            self.render("{{ i|display_datetime:t }}", i=NAIROBI_MIDNIGHT, t=self.school),
            "14 Mar 2026, 22:30",
        )
        self.assertEqual(
            self.render("{{ i|display_datetime:b }}", i=NAIROBI_MIDNIGHT, b=self.mombasa),
            "15 Mar 2026, 00:30",
        )
        self.assertEqual(
            self.render("{{ i|display_time:b }}", i=NAIROBI_MIDNIGHT, b=self.mombasa), "00:30",
        )
        self.assertEqual(
            self.render("{{ d|display_month:t }}", d=datetime.date(2026, 9, 29), t=self.school),
            "Sep 2026",
        )
