"""``vs_config.clock``: a tenant's zone, its "now" and its "today".

The server runs in UTC, so the case that matters is the hour after midnight
in Lagos: at 23:30 UTC on 14 March it is 00:30 on 15 March in Lagos, and a
tenant there must be told it is the 15th. Two tenants throughout: Harbour
keeps the platform default, and Rift Valley has chosen Africa/Nairobi.
"""
import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import TestCase

from vs_tenants.context import reset_current_tenant, set_current_tenant
from vs_tenants.models import Tenant

from .clock import (
    DEFAULT_TIME_ZONE,
    TIME_ZONE_KEY,
    forget_tenant_zone,
    is_valid_time_zone,
    tenant_now,
    tenant_today,
    tenant_zone,
)
from .exceptions import InvalidConfigurationValue
from .models import ConfigurationDefinition
from .services.resolution import set_value

#: 23:30 UTC on 14 March: already the 15th in Lagos and in Nairobi.
LATE_EVENING_UTC = datetime.datetime(2026, 3, 14, 23, 30, tzinfo=datetime.timezone.utc)


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
        self.assertEqual(self.definition.allowed_scopes, ["platform", "school"])
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
