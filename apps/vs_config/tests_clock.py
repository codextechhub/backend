"""``vs_config.clock``: a tenant's zone, its "now" and its "today".

The server runs in UTC, so the case that matters is the hour after midnight
in Lagos: at 23:30 UTC on 14 March it is 00:30 on 15 March in Lagos, and a
tenant there must be told it is the 15th. The tenant tests use two tenants:
Harbour keeps the platform default, and Rift Valley has chosen Africa/Nairobi.
The branch tests use a Lagos school with a branch on Nairobi time, and the
display tests the block a signed-in client caches.
"""
import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.test import TestCase

from vs_tenants.context import reset_current_tenant, set_current_tenant
from vs_tenants.models import Tenant

from .clock import (
    DEFAULT_TIME_ZONE,
    TIME_ZONE_KEY,
    branch_day_q,
    branch_now,
    branch_today,
    branch_zone,
    branch_zones,
    forget_tenant_zone,
    is_valid_time_zone,
    tenant_now,
    tenant_today,
    tenant_zone,
)
from .display import (
    CLOCK_KEY,
    D_MMM_YYYY,
    DATE_FORMAT_KEY,
    date_format_options,
    display_preferences,
    write_date,
)
from .exceptions import InvalidConfigurationValue
from .models import ConfigurationDefinition
from .services.resolution import set_value

#: 23:30 UTC on 14 March: already the 15th in Lagos and in Nairobi.
LATE_EVENING_UTC = datetime.datetime(2026, 3, 14, 23, 30, tzinfo=datetime.timezone.utc)

#: 21:30 UTC on 14 March: the 15th in Nairobi, still the 14th in Lagos.
NAIROBI_MIDNIGHT_UTC = datetime.datetime(2026, 3, 14, 21, 30, tzinfo=datetime.timezone.utc)


def _at(instant):
    return mock.patch("django.utils.timezone.now", return_value=instant)


class TenantClockTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.definition = ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY)
        cls.harbour = Tenant.objects.create(
            name="Harbour", slug="harbour", kind=Tenant.Kind.ORGANIZATION,
        )
        cls.rift = Tenant.objects.create(
            name="Rift Valley", slug="rift-valley", kind=Tenant.Kind.ORGANIZATION,
        )
        set_value(
            definition=cls.definition, value="Africa/Nairobi", actor=None,
            tenant=cls.rift,
        )

    def test_the_definition_defaults_to_lagos_for_platform_and_tenant(self):
        self.assertEqual(self.definition.default_value, DEFAULT_TIME_ZONE)
        self.assertEqual(self.definition.allowed_scopes, ["branch", "platform", "school"])
        self.assertEqual(tenant_zone(self.harbour), ZoneInfo("Africa/Lagos"))
        self.assertEqual(tenant_zone(None), ZoneInfo("Africa/Lagos"))

    def test_late_evening_utc_is_already_tomorrow_in_lagos(self):
        with _at(LATE_EVENING_UTC):
            self.assertEqual(tenant_today(self.harbour), datetime.date(2026, 3, 15))
            self.assertEqual(tenant_today(None), datetime.date(2026, 3, 15))
            now = tenant_now(self.harbour)
        self.assertEqual((now.hour, now.minute), (0, 30))
        self.assertEqual(now, LATE_EVENING_UTC)

    def test_a_tenant_on_nairobi_gets_its_own_day_and_clock(self):
        with _at(datetime.datetime(2026, 3, 14, 21, 30, tzinfo=datetime.timezone.utc)):
            self.assertEqual(tenant_today(self.rift), datetime.date(2026, 3, 15))
            self.assertEqual(tenant_today(self.harbour), datetime.date(2026, 3, 14))
            self.assertEqual(tenant_now(self.rift).hour, 0)

    def test_the_platform_value_is_what_a_tenant_without_its_own_inherits(self):
        set_value(definition=self.definition, value="Europe/London", actor=None)
        self.assertEqual(tenant_zone(self.harbour), ZoneInfo("Europe/London"))
        self.assertEqual(tenant_zone(None), ZoneInfo("Europe/London"))
        self.assertEqual(tenant_zone(self.rift), ZoneInfo("Africa/Nairobi"))

    def test_the_platform_tenant_reads_the_platform_layer(self):
        platform = Tenant.objects.get(kind=Tenant.Kind.PLATFORM)
        set_value(definition=self.definition, value="Africa/Accra", actor=None)
        self.assertEqual(tenant_zone(platform), ZoneInfo("Africa/Accra"))

    def test_the_zone_is_read_once_per_tenant_instance_until_forgotten(self):
        with self.assertNumQueries(2):
            tenant_zone(self.harbour)
        with self.assertNumQueries(0):
            tenant_today(self.harbour)
            tenant_now(self.harbour)
        set_value(
            definition=self.definition, value="Africa/Cairo", actor=None,
            tenant=self.harbour,
        )
        forget_tenant_zone(self.harbour)
        self.assertEqual(tenant_zone(self.harbour), ZoneInfo("Africa/Cairo"))

    def test_a_rows_copy_of_the_request_tenant_shares_its_memo(self):
        """A list reading today per row asks the configuration once."""
        token = set_current_tenant(self.harbour)
        try:
            tenant_zone(self.harbour)
            row_copy = Tenant.objects.get(pk=self.harbour.pk)
            with self.assertNumQueries(0):
                self.assertEqual(tenant_zone(row_copy), ZoneInfo("Africa/Lagos"))
            forget_tenant_zone(row_copy)
            self.assertFalse(hasattr(self.harbour, "_vs_config_tenant_zone"))
        finally:
            reset_current_tenant(token)

    def test_a_name_that_is_not_a_zone_is_refused_at_every_write(self):
        for bad in ("Lagos", "Africa/Atlantis", "UTC+1", "", "localtime", "../etc/passwd"):
            with self.subTest(value=bad), self.assertRaises(InvalidConfigurationValue):
                set_value(
                    definition=self.definition, value=bad, actor=None,
                    tenant=self.harbour,
                )
        self.assertEqual(tenant_zone(self.harbour), ZoneInfo("Africa/Lagos"))

    def test_a_stored_value_that_is_not_a_zone_falls_back_to_lagos(self):
        """A row written around the guard (a raw update) never breaks a reader."""
        row = set_value(
            definition=self.definition, value="Africa/Nairobi", actor=None,
            tenant=self.harbour,
        )
        type(row).all_objects.filter(pk=row.pk).update(value="Mars/Olympus")
        self.assertEqual(tenant_zone(self.harbour), ZoneInfo("Africa/Lagos"))

    def test_is_valid_time_zone(self):
        self.assertTrue(is_valid_time_zone("Africa/Lagos"))
        self.assertTrue(is_valid_time_zone("America/New_York"))
        self.assertFalse(is_valid_time_zone(None))
        self.assertFalse(is_valid_time_zone(7))


class BranchClockTests(TestCase):
    """A branch that keeps its own zone, and branches that follow their school.

    Harbour School runs on Lagos time with two branches: Apapa follows the
    school and Mombasa keeps Africa/Nairobi. At 21:30 UTC on 14 March it is
    22:30 on the 14th in Lagos and already 00:30 on the 15th in Nairobi.
    """

    @classmethod
    def setUpTestData(cls):
        from vs_tenants.models import Branch

        cls.definition = ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY)
        cls.harbour = Tenant.objects.create(
            name="Harbour School", slug="harbour-school", kind=Tenant.Kind.ORGANIZATION,
        )
        cls.apapa = Branch.all_objects.create(
            tenant=cls.harbour, name="Apapa Branch", code=1, is_main=True,
        )
        cls.mombasa = Branch.all_objects.create(
            tenant=cls.harbour, name="Mombasa Branch", code=2,
        )
        set_value(
            definition=cls.definition, value="Africa/Nairobi", actor=None,
            branch=cls.mombasa,
        )

    def setUp(self):
        forget_tenant_zone(self.harbour)

    def test_a_branch_with_its_own_zone_turns_its_day_at_its_own_midnight(self):
        with _at(NAIROBI_MIDNIGHT_UTC):
            self.assertEqual(tenant_today(self.harbour), datetime.date(2026, 3, 14))
            self.assertEqual(branch_today(self.harbour, self.apapa), datetime.date(2026, 3, 14))
            self.assertEqual(branch_today(self.harbour, self.mombasa), datetime.date(2026, 3, 15))
            self.assertEqual(branch_now(self.harbour, self.mombasa).hour, 0)
            self.assertEqual(branch_now(self.harbour, self.mombasa), NAIROBI_MIDNIGHT_UTC)

    def test_a_branch_is_named_by_row_id_or_digits_and_none_is_the_school(self):
        with _at(NAIROBI_MIDNIGHT_UTC):
            for ref in (self.mombasa, self.mombasa.pk, str(self.mombasa.pk)):
                with self.subTest(ref=ref):
                    self.assertEqual(branch_zone(self.harbour, ref), ZoneInfo("Africa/Nairobi"))
            self.assertEqual(branch_zone(self.harbour, None), ZoneInfo("Africa/Lagos"))
            self.assertEqual(branch_zone(None, self.mombasa), ZoneInfo("Africa/Nairobi"))
            self.assertEqual(branch_today(self.harbour, None), datetime.date(2026, 3, 14))

    def test_another_tenants_branch_reads_the_tenant_asked_about(self):
        other = Tenant.objects.create(
            name="Rift", slug="rift-school", kind=Tenant.Kind.ORGANIZATION,
        )
        self.assertEqual(branch_zone(other, self.mombasa.pk), ZoneInfo("Africa/Lagos"))

    def test_a_branch_follows_its_school_when_the_school_moves(self):
        set_value(
            definition=self.definition, value="Africa/Johannesburg", actor=None,
            tenant=self.harbour,
        )
        forget_tenant_zone(self.harbour)
        self.assertEqual(branch_zone(self.harbour, self.apapa), ZoneInfo("Africa/Johannesburg"))
        self.assertEqual(branch_zone(self.harbour, self.mombasa), ZoneInfo("Africa/Nairobi"))

    def test_the_school_and_every_branch_zone_are_read_in_two_queries(self):
        """The same two queries a school's own zone always cost, then none."""
        with self.assertNumQueries(2):
            self.assertEqual(
                branch_zones(self.harbour), {self.mombasa.pk: ZoneInfo("Africa/Nairobi")},
            )
        with self.assertNumQueries(0):
            self.assertEqual(tenant_zone(self.harbour), ZoneInfo("Africa/Lagos"))
            branch_today(self.harbour, self.mombasa)
            branch_today(self.harbour, self.apapa)
            branch_day_q(self.harbour, "branch", lambda day: Q(due_date__lt=day))

    def test_forgetting_the_tenant_zone_forgets_the_branch_zones_too(self):
        branch_zones(self.harbour)
        set_value(
            definition=self.definition, value="Africa/Cairo", actor=None,
            branch=self.apapa,
        )
        forget_tenant_zone(self.harbour)
        self.assertEqual(branch_zone(self.harbour, self.apapa), ZoneInfo("Africa/Cairo"))

    def test_a_stored_branch_value_that_is_not_a_zone_follows_the_school(self):
        row = set_value(
            definition=self.definition, value="Africa/Kampala", actor=None,
            branch=self.apapa,
        )
        type(row).all_objects.filter(pk=row.pk).update(value="Mars/Olympus")
        self.assertEqual(branch_zone(self.harbour, self.apapa), ZoneInfo("Africa/Lagos"))

    def test_the_platform_and_no_tenant_have_no_branch_zones(self):
        platform = Tenant.objects.get(kind=Tenant.Kind.PLATFORM)
        self.assertEqual(branch_zones(platform), {})
        self.assertEqual(branch_zones(None), {})
        self.assertEqual(branch_zone(None, None), ZoneInfo("Africa/Lagos"))

    def test_a_filter_judges_each_row_on_its_own_branchs_day(self):
        """What is overdue at 00:30 in Mombasa is not yet overdue in Apapa."""
        due_14th = lambda day: Q(due_date__lt=day)  # noqa: E731
        with _at(NAIROBI_MIDNIGHT_UTC):
            condition = branch_day_q(self.harbour, "branch", due_14th)
        rows = [
            {"due_date": datetime.date(2026, 3, 14), "branch": self.mombasa.pk},
            {"due_date": datetime.date(2026, 3, 14), "branch": self.apapa.pk},
            {"due_date": datetime.date(2026, 3, 14), "branch": None},
        ]
        self.assertEqual(
            [_matches(condition, row) for row in rows], [True, False, False],
        )

    def test_a_school_whose_branches_share_its_zone_filters_exactly_as_before(self):
        from vs_config.models import ConfigurationValue

        ConfigurationValue.all_objects.filter(branch=self.mombasa).delete()
        forget_tenant_zone(self.harbour)
        on_day = lambda day: Q(due_date__lt=day)  # noqa: E731
        with _at(NAIROBI_MIDNIGHT_UTC):
            self.assertEqual(
                branch_day_q(self.harbour, "branch", on_day),
                on_day(tenant_today(self.harbour)),
            )
            self.assertEqual(
                branch_today(self.harbour, self.mombasa), tenant_today(self.harbour),
            )

    def test_the_school_clock_patched_moves_every_branch_with_it(self):
        """Tests set a school's day at ``tenant_now``; a branch reads the same instant."""
        moment = datetime.datetime(2026, 3, 14, 22, 30, tzinfo=ZoneInfo("Africa/Lagos"))
        with mock.patch("vs_config.clock.tenant_now", return_value=moment):
            self.assertEqual(branch_today(self.harbour, self.apapa), datetime.date(2026, 3, 14))
            self.assertEqual(branch_today(self.harbour, self.mombasa), datetime.date(2026, 3, 15))


def _matches(condition, row):
    """Evaluate a flat ``Q`` of ``__lt``/``__in`` lookups against *row* in Python."""
    if isinstance(condition, tuple):
        lookup, value = condition
        field, op = lookup.rsplit("__", 1)
        actual = row[field]
        if op == "lt":
            return actual is not None and actual < value
        if op == "in":
            return actual is not None and actual in value
        raise AssertionError(f"unexpected lookup {lookup}")
    results = [_matches(child, row) for child in condition.children]
    outcome = all(results) if condition.connector == "AND" else any(results)
    return not outcome if condition.negated else outcome


class DisplayPreferencesTests(TestCase):
    """The block a signed-in client caches: zone, date format, clock, branch zones."""

    @classmethod
    def setUpTestData(cls):
        from vs_tenants.models import Branch

        cls.zone = ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY)
        cls.date_format = ConfigurationDefinition.objects.get(key=DATE_FORMAT_KEY)
        cls.clock = ConfigurationDefinition.objects.get(key=CLOCK_KEY)
        cls.school = Tenant.objects.create(
            name="Coastline", slug="coastline", kind=Tenant.Kind.ORGANIZATION,
        )
        cls.branch = Branch.all_objects.create(tenant=cls.school, name="Kisumu", code=1)

    def test_a_school_that_has_chosen_nothing_reads_the_defaults(self):
        self.assertEqual(display_preferences(self.school), {
            "time_zone": "Africa/Lagos", "date_format": "D_MMM_YYYY",
            "clock": "H12", "branch_zones": {},
        })

    def test_the_schools_choices_and_its_branch_zones(self):
        set_value(definition=self.date_format, value="DD_MM_YYYY", actor=None, tenant=self.school)
        set_value(definition=self.clock, value="H24", actor=None, tenant=self.school)
        set_value(definition=self.zone, value="Africa/Nairobi", actor=None, branch=self.branch)
        self.assertEqual(display_preferences(self.school), {
            "time_zone": "Africa/Lagos", "date_format": "DD_MM_YYYY",
            "clock": "H24", "branch_zones": {str(self.branch.pk): "Africa/Nairobi"},
        })

    def test_a_platform_value_is_inherited_and_a_school_value_wins(self):
        set_value(definition=self.clock, value="H24", actor=None)
        self.assertEqual(display_preferences(self.school)["clock"], "H24")
        self.assertEqual(display_preferences(None)["clock"], "H24")
        set_value(definition=self.clock, value="H12", actor=None, tenant=self.school)
        self.assertEqual(display_preferences(self.school)["clock"], "H12")

    def test_the_format_and_clock_refuse_anything_outside_their_choices(self):
        for definition, bad in ((self.date_format, "MM_DD_YYYY"), (self.clock, "H13")):
            with self.subTest(key=definition.key), self.assertRaises(InvalidConfigurationValue):
                set_value(definition=definition, value=bad, actor=None, tenant=self.school)

    def test_neither_the_format_nor_the_clock_can_be_set_for_a_branch(self):
        from .exceptions import InvalidConfigurationScope

        for definition in (self.date_format, self.clock):
            with self.subTest(key=definition.key), self.assertRaises(InvalidConfigurationScope):
                set_value(
                    definition=definition, value=definition.default_value, actor=None,
                    branch=self.branch,
                )

    def test_a_stored_value_outside_the_choices_reads_as_the_default(self):
        row = set_value(definition=self.clock, value="H24", actor=None, tenant=self.school)
        type(row).all_objects.filter(pk=row.pk).update(value="H99")
        self.assertEqual(display_preferences(self.school)["clock"], "H12")

    def test_the_examples_a_school_is_offered(self):
        day = datetime.date(2026, 9, 29)
        self.assertEqual(
            [option["label"] for option in date_format_options(day)],
            ["29 Sep 2026", "29/09/2026", "2026-09-29"],
        )
        self.assertEqual(write_date(datetime.date(2026, 3, 5), D_MMM_YYYY), "5 Mar 2026")
