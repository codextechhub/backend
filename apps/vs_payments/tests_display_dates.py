"""A settlement file writes its dates the tenant's way.

Harbour Academy writes dates DD/MM/YYYY on a 24-hour clock, in Lagos. Its
settlement export is read by its bursar, so the window in the subtitle and a
bank line's date read in that format, and a gateway confirmation, which is an
instant, reads on Lagos's clock: a payment confirmed at 23:30 UTC on 14 March
is 00:30 on 15 March at the school.
"""
import datetime
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from vs_config.clock import forget_tenant_zone
from vs_config.display import CLOCK_KEY, DATE_FORMAT_KEY
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_tenants.models import Tenant

from .reconciliation import SettlementReconciliation, SettlementRow, UnmatchedBankLine
from .views import _maybe_export_settlement

UTC = datetime.timezone.utc


class SettlementExportDatesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = Tenant.objects.create(
            name="Harbour Academy", slug="harbour-academy-settlement-dates",
            kind=Tenant.Kind.ORGANIZATION,
        )
        for key, value in ((DATE_FORMAT_KEY, "DD_MM_YYYY"), (CLOCK_KEY, "H24")):
            set_value(
                definition=ConfigurationDefinition.objects.get(key=key),
                value=value, actor=None, tenant=cls.school,
            )

    def setUp(self):
        forget_tenant_zone(self.school)
        self.entity = SimpleNamespace(tenant=self.school, code="HARBOUR")

    def _table(self, recon, view):
        """The ReportTable the export would render, captured before rendering."""
        request = SimpleNamespace(query_params={"export": "csv", "view": view})
        with mock.patch("vs_finance.views._maybe_export", lambda request, table, **_: table):
            return _maybe_export_settlement(request, recon, self.entity)

    def _recon(self, **window):
        return SettlementReconciliation(
            entity_id=1, entity_code="HARBOUR", provider="",
            start_date=window.get("start"), end_date=window.get("end"),
            rows=[SettlementRow(
                kind="COLLECTION", gateway_id=1, reference="PAY-1", provider="fake",
                provider_reference="", amount=500_000,
                confirmed_at=datetime.datetime(2026, 3, 14, 23, 30, tzinfo=UTC),
            )],
            unmatched_bank_lines=[UnmatchedBankLine(
                bank_line_id=1, bank_account_id=1, txn_date=datetime.date(2026, 3, 16),
                description="Transfer", reference="BNK-1", amount=20_000,
            )],
        )

    def test_the_window_and_a_bank_line_date_are_in_the_schools_format(self):
        table = self._table(
            self._recon(start=datetime.date(2026, 3, 1), end=datetime.date(2026, 3, 31)),
            "unmatched",
        )
        self.assertEqual(table.subtitle, "HARBOUR · 01/03/2026 - 31/03/2026")
        self.assertEqual(table.rows[0][0], "16/03/2026")

    def test_a_confirmation_reads_on_the_schools_clock(self):
        table = self._table(self._recon(), "unsettled")
        self.assertEqual(table.subtitle, "HARBOUR · All dates")
        self.assertEqual(table.rows[0][0], "15/03/2026, 00:30")

    def test_a_window_open_at_one_end_says_which(self):
        self.assertEqual(
            self._table(self._recon(start=datetime.date(2026, 3, 1)), "unmatched").subtitle,
            "HARBOUR · From 01/03/2026",
        )
        self.assertEqual(
            self._table(self._recon(end=datetime.date(2026, 3, 31)), "unmatched").subtitle,
            "HARBOUR · Up to 31/03/2026",
        )
