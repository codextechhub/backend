"""The Receivables & collections tab.

The same Corona school as the overview tests: First Term runs 5 January to 31
March 2026; Ikeja's Tunde is billed 100,000 of First Term fees and pays 60,000
on 15 January; Lekki's Aisha owes last term's 100,000 and pays 40,000.

The tab answers for the reader the way the overview does: blocks need their
keys, and a bursar at Ikeja never sees Lekki's payers among the largest
balances. The collection curve reads the term's fees week by week against the
target the school set; a school that groups payers by class sees collection by
class, and books with no grouping see no such card.
"""
from __future__ import annotations

import datetime

from django.db.models import Q
from django.test import override_settings

from core.test_utils import TenantAPIClient
from vs_finance.dashboard import EVERY_BLOCK
from vs_finance.dashboard_blocks import Window
from vs_finance.dashboard_receivables import previous_window, receivables_view
from vs_finance.models import Account, Payment

from .tests_dashboard_overview import _OverviewFixture


def two_groups(entity, customer_ids):
    """A stand-in owner grouping: Ikeja's payers in "JSS1", everyone else unplaced."""
    from vs_finance.models import Customer
    from vs_finance.payer_groups import PayerGrouping

    ikeja = set(Customer.objects.filter(id__in=customer_ids, code="CIKJ").values_list("id", flat=True))
    return PayerGrouping(label="Class", groups={cid: "JSS1" for cid in ikeja})


def no_groups(entity, customer_ids):
    return None


class _ReceivablesFixture(_OverviewFixture):
    def view(self, window=None, reader=None):
        return receivables_view(self.books, reader=reader or EVERY_BLOCK, window=window, period=self.period)


class CollectionCurveTests(_ReceivablesFixture):
    def test_the_curve_climbs_week_by_week_to_the_share_paid(self):
        curve = self.view("term")["curve"]
        # 5 Jan start; Tunde's 60,000 lands in week 2; the view is as at 31 January.
        self.assertEqual(curve["current"], [0.0, 60.0, 60.0, 60.0])
        self.assertEqual(curve["weeks"], 13)
        self.assertEqual(curve["target_pct"], 90)

    def test_the_target_is_the_schools_own(self):
        from vs_finance.models import FinanceDocumentSettings

        FinanceDocumentSettings.objects.create(entity=self.books, term_collection_target_pct=85)
        self.assertEqual(self.view("term")["curve"]["target_pct"], 85)

    def test_the_previous_window_is_the_month_or_term_before(self):
        month = Window("month", "This month", "January 2026", datetime.date(2026, 1, 1), datetime.date(2026, 1, 31))
        before = previous_window(self.books, month)
        self.assertEqual((before.start, before.end), (datetime.date(2025, 12, 1), datetime.date(2025, 12, 31)))
        # First Term is the school's earliest term, so it has nothing to compare with.
        first_term = Window("term", "This term", "First Term", datetime.date(2026, 1, 5),
                            datetime.date(2026, 3, 31), invoices=Q(reference__in=["FEE:T1"]))
        self.assertIsNone(previous_window(self.books, first_term))


class GroupingTests(_ReceivablesFixture):
    @override_settings(FINANCE_PAYER_GROUP_PROVIDER="vs_finance.tests_dashboard_receivables.two_groups")
    def test_collection_is_read_per_group_the_owner_defines(self):
        from vs_finance.payer_groups import _provider

        _provider.cache_clear()
        groups = self.view("month")["groups"]
        _provider.cache_clear()
        self.assertEqual(groups["label"], "Class")
        self.assertEqual([(g["name"], g["collected"]["kobo"]) for g in groups["items"]], [("JSS1", 60_000)])

    @override_settings(FINANCE_PAYER_GROUP_PROVIDER="vs_finance.tests_dashboard_receivables.no_groups")
    def test_books_with_no_grouping_get_no_card(self):
        from vs_finance.payer_groups import _provider

        _provider.cache_clear()
        groups = self.view("month")["groups"]
        _provider.cache_clear()
        self.assertIsNone(groups)


class ReminderAndReliefTests(_ReceivablesFixture):
    def test_reminders_count_the_ones_followed_by_payment_within_a_week(self):
        from vs_finance.dunning import ensure_default_policy, generate_dunning
        from vs_finance.receivables import post_payment

        generate_dunning(self.books, as_of=datetime.date(2026, 1, 28), policy=ensure_default_policy(self.books))
        payment = Payment.objects.create(
            entity=self.books, customer=self.tunde, branch=self.ikeja, method="CASH",
            payment_date=datetime.date(2026, 1, 30), amount=40_000,
            deposit_account=Account.objects.get(entity=self.books, code="1100"),
        )
        post_payment(payment, allocations=[(self.term_invoice, 40_000)])

        first = self.view("month")["dunning"][0]
        self.assertEqual(first["sent"], 2)
        self.assertEqual(first["paid_pct"], 50.0)

    def test_overpayment_shows_as_credit_held(self):
        from vs_finance.receivables import post_payment

        payment = Payment.objects.create(
            entity=self.books, customer=self.tunde, branch=self.ikeja, method="ONLINE",
            payment_date=datetime.date(2026, 1, 20), amount=70_000,
            deposit_account=Account.objects.get(entity=self.books, code="1100"),
        )
        post_payment(payment, allocations=[(self.term_invoice, 40_000)])
        credit = self.view("month")["credit"]
        self.assertEqual(credit["total"]["kobo"], 30_000)
        self.assertEqual(credit["payers"], 1)

    def test_blocks_follow_their_keys(self):
        reader = self.reader("finance.invoice.view")
        d = self.view("month", reader)
        self.assertIsNone(d["dunning"])
        self.assertIsNone(d["concessions"])
        self.assertIsNone(d["plans"])
        self.assertIsNone(d["credit"])
        self.assertIsNone(d["curve"])  # Needs receipts as well as invoices.
        self.assertEqual(d["adjustments"], [])
        self.assertIsNotNone(d["largest"])


class LargestBalanceTests(_ReceivablesFixture):
    def test_a_branch_reader_sees_only_their_branchs_payers(self):
        ikeja = self.reader("finance.invoice.view", branch=self.ikeja)
        names = {r["code"] for r in self.view("month", ikeja)["largest"]}
        self.assertEqual(names, {"CIKJ"})
        school = {r["code"] for r in self.view("month")["largest"]}
        self.assertEqual(school, {"CIKJ", "CLEK"})

    def test_the_balance_is_split_by_age_and_names_the_last_action(self):
        row = next(r for r in self.view("month")["largest"] if r["code"] == "CLEK")
        self.assertEqual(row["owed"]["kobo"], 60_000)
        self.assertEqual(row["days_1_30"]["kobo"], 60_000)  # Due 25 January, as at 31 January.
        self.assertEqual(row["last_action"], "Paid 15 Jan")


class ReceivablesEndpointTests(_ReceivablesFixture):
    def test_the_endpoint_opens_to_a_finance_reader(self):
        user = self.grant(self.user_for(self.tenant, "ar-tab@corona.test"), "finance.invoice.view",
                          tenant=self.tenant, role_key="role-ar-tab")
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/reports/dashboard/receivables/?entity={self.books.code}&window=month&period=1",
        )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(response.json()["data"]["window"]["key"], "month")

    def test_a_reader_with_no_finance_key_is_refused(self):
        user = self.grant(self.user_for(self.tenant, "no-fin@corona.test"), "procurement.vendor.view",
                          tenant=self.tenant, role_key="role-no-fin")
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/reports/dashboard/receivables/?entity={self.books.code}",
        )
        self.assertEqual(response.status_code, 403)
