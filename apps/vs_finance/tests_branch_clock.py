"""An invoice's day is its branch's.

Corona Group keeps Lagos time, and its Ikeja branch keeps Nairobi's. At 21:30
UTC on 14 January it is 22:30 on the 14th at Lekki, which follows the school,
and 00:30 on the 15th at Ikeja. A fee run with no date bills each family on
its own branch's day, and a bill due on the 14th is overdue at Ikeja and not
yet at Lekki.

The school's clock is patched at ``vs_config.clock.tenant_now``, the one
place every "today" is read from, and a branch reads that same instant in its
own zone.
"""
from __future__ import annotations

import datetime
from contextlib import contextmanager
from unittest import mock

from vs_config.clock import TIME_ZONE_KEY, forget_tenant_zone, tenant_zone
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import set_value
from vs_finance.models import Account, FeeItem, Invoice

from .tests_branch_scope import _FinanceBranchFixture

INSTANT = datetime.datetime(2026, 1, 14, 21, 30, tzinfo=datetime.timezone.utc)
LAGOS_DAY = datetime.date(2026, 1, 14)
NAIROBI_DAY = datetime.date(2026, 1, 15)


@contextmanager
def at_instant():
    with mock.patch(
        "vs_config.clock.tenant_now",
        side_effect=lambda tenant: INSTANT.astimezone(tenant_zone(tenant)),
    ):
        yield


class InvoiceBranchClockTests(_FinanceBranchFixture):

    def setUp(self):
        super().setUp()
        set_value(
            definition=ConfigurationDefinition.objects.get(key=TIME_ZONE_KEY),
            value="Africa/Nairobi", actor=None, branch=self.ikeja,
        )
        forget_tenant_zone(self.tenant)
        self.at_ikeja = self.customer(self.books, "CLKI", self.ikeja)
        self.at_lekki = self.customer(self.books, "CLKL", self.lekki)

    def test_a_fee_run_with_no_date_bills_each_family_on_its_branchs_day(self):
        from vs_finance.fees import generate_invoices

        structure = self.fee_structure(self.books, "CLKF", None)
        FeeItem.objects.create(
            structure=structure, line_no=1, description="Tuition",
            revenue_account=Account.objects.get(entity=self.books, code="4100"),
            amount=300_000,
        )
        with at_instant():
            invoices = generate_invoices(structure, [self.at_ikeja, self.at_lekki])
        dated = {inv.customer_id: (inv.invoice_date, inv.due_date) for inv in invoices}
        ikeja_date, ikeja_due = dated[self.at_ikeja.pk]
        lekki_date, lekki_due = dated[self.at_lekki.pk]
        self.assertEqual((ikeja_date, lekki_date), (NAIROBI_DAY, LAGOS_DAY))
        self.assertEqual(ikeja_due - ikeja_date, lekki_due - lekki_date)

    def test_a_bill_due_on_the_14th_is_overdue_at_ikeja_only(self):
        from vs_finance.views import _invoice_bucket

        for customer, branch in ((self.at_ikeja, self.ikeja), (self.at_lekki, self.lekki)):
            Invoice.objects.create(
                entity=self.books, customer=customer, branch=branch, status="POSTED",
                invoice_date=datetime.date(2026, 1, 2), due_date=LAGOS_DAY,
            )
        posted = Invoice.objects.filter(entity=self.books)
        with at_instant():
            overdue = set(
                _invoice_bucket(posted, "overdue", self.tenant)
                .values_list("branch_id", flat=True)
            )
            issued = set(
                _invoice_bucket(posted, "issued", self.tenant)
                .values_list("branch_id", flat=True)
            )
        self.assertEqual(overdue, {self.ikeja.pk})
        self.assertEqual(issued, {self.lekki.pk})
