"""Dates procurement prints for people follow the buying tenant's display settings.

Harbour Academy writes dates DD/MM/YYYY on a 24-hour clock and keeps its
calendar in Nairobi. Bright Star keeps every default: "14 Mar 2026", a 12-hour
clock, Lagos. A purchase order's dates and an RFQ deadline read each school's
way, and the deadline a vendor reads names its zone, because the vendor sits
outside the school.
"""
import datetime
from types import SimpleNamespace

from django.test import TestCase

from vs_config.clock import TIME_ZONE_KEY, forget_tenant_zone
from vs_config.display import CLOCK_KEY, DATE_FORMAT_KEY
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_tenants.models import Tenant

from .po_email import _order_dates
from .vendor_portal import _vendor_moment, format_deadline

UTC = datetime.timezone.utc
#: 23:59 on 14 March in Lagos, and 01:59 on 15 March in Nairobi.
LAGOS_LAST_MINUTE_UTC = datetime.datetime(2026, 3, 14, 22, 59, 59, tzinfo=UTC)


def _set(tenant, key, value):
    set_value(
        definition=ConfigurationDefinition.objects.get(key=key),
        value=value, actor=None, tenant=tenant,
    )


class ProcurementDatesFollowTheTenantTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.harbour = Tenant.objects.create(
            name="Harbour Academy", slug="harbour-academy-procurement-dates",
            kind=Tenant.Kind.ORGANIZATION,
        )
        _set(cls.harbour, DATE_FORMAT_KEY, "DD_MM_YYYY")
        _set(cls.harbour, CLOCK_KEY, "H24")
        _set(cls.harbour, TIME_ZONE_KEY, "Africa/Nairobi")
        cls.bright_star = Tenant.objects.create(
            name="Bright Star School", slug="bright-star-procurement-dates",
            kind=Tenant.Kind.ORGANIZATION,
        )

    def setUp(self):
        forget_tenant_zone(self.harbour)
        forget_tenant_zone(self.bright_star)

    @staticmethod
    def _rfq(tenant):
        return SimpleNamespace(entity=SimpleNamespace(tenant=tenant), branch_id=None)

    def test_a_vendor_deadline_is_the_schools_wording_on_its_clock_with_the_zone(self):
        self.assertEqual(
            _vendor_moment(LAGOS_LAST_MINUTE_UTC, self._rfq(self.harbour)),
            "15/03/2026, 01:59 EAT",
        )
        self.assertEqual(
            _vendor_moment(LAGOS_LAST_MINUTE_UTC, self._rfq(self.bright_star)),
            "14 Mar 2026, 11:59 pm WAT",
        )

    def test_an_invitation_with_no_deadline_says_so(self):
        invitation = SimpleNamespace(deadline=None, rfq=self._rfq(self.harbour))
        self.assertEqual(format_deadline(invitation), "No deadline")

    def test_purchase_order_dates_are_the_schools_and_never_shift(self):
        po = SimpleNamespace(
            entity=SimpleNamespace(tenant=self.harbour),
            order_date=datetime.date(2026, 9, 29), expected_date=None,
        )
        self.assertEqual(_order_dates(po), ("29/09/2026", "Not specified"))
        po.entity.tenant = self.bright_star
        po.expected_date = datetime.date(2026, 10, 6)
        self.assertEqual(_order_dates(po), ("29 Sep 2026", "6 Oct 2026"))
