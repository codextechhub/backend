"""Read switches on the finance dashboards, the close, the sealed figures and the year archive.

Every figure in this area is worked out by the books, so every field is read-only
and open to every role until a school closes it. A switch hides one whole card,
block, trend or exception list: the key is absent from the response, the cards
beside it stay, and a card the caller's permission keys already withhold stays
``None`` whatever the switch says.

Corona runs Ikeja, Lekki and Yaba; Single Site has one branch. Lagoon View (Ikeja
and Lekki) and Harbour Primary (one branch) carry the close, seal and archive
cases.
"""
from __future__ import annotations

import datetime
import itertools
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory

from core.test_utils import TenantAPIClient
from vs_finance.close_access import check_readable, redact_close_refusal
from vs_finance.dashboard import EVERY_BLOCK, DashboardReader, finance_dashboard
from vs_finance.models import Account, FiscalPeriod, LedgerSeal
from vs_rbac.field_enforcement import can_read
from vs_rbac.field_registry import get_declaration
from vs_rbac.models import TenantRoleTemplate
from vs_rbac.tests.helpers import (
    install_declared_fields,
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
    make_school_admin,
    set_field_access,
)

from .tests_dashboard_spend import _SpendFixture
from .tests_record_retention import _Books

DASHBOARD = "finance.dashboard"
PERIOD = "finance.period"

#: Every key that opens a dashboard block, so an "everything open" reader reaches each card.
ALL_KEYS = (
    "finance.report.view", "finance.invoice.view", "finance.payment.view",
    "finance.bankaccount.view", "finance.tax.view", "finance.period.view",
    "finance.journal.view", "finance.paymentplan.view", "finance.pettycash.view",
    "finance.dunning.view", "finance.concession.view", "finance.expenseclaim.view",
    "finance.budget.view", "finance.payrollrun.view", "finance.fixedasset.view",
    "finance.creditnote.view", "finance.refund.view", "finance.writeoff.view",
)

#: The registered names of each dashboard tab, by the group the registry gives them.
OVERVIEW = "Overview"
RECEIVABLES = "Receivables and collections"
SPEND = "Cash, spend and compliance"

URLS = {
    OVERVIEW: "reports/dashboard/",
    RECEIVABLES: "reports/dashboard/receivables/",
    SPEND: "reports/dashboard/spend/",
}

#: The payload keys that are context or labels and carry no figure.
NOT_CARDS = frozenset({
    "books", "reader_first_name", "window", "windows", "entity", "fiscal_year", "period",
    "period_label", "as_of", "narrowed", "kpis",
})

_counter = itertools.count(1)


def _specs(group, *, shared=()):
    """The dashboard field specs of one tab: its own group plus the cards shared with it."""
    declaration = get_declaration("finance", "dashboard")
    return [s for s in declaration.fields if s.group == group or s.name in shared]


def _request_for(user, tenant):
    request = APIRequestFactory().get("/")
    request.user = user
    request.tenant = tenant
    return request


class _DashboardFieldFixture(_SpendFixture):
    """Corona's books with the dashboard, close and seal fields installed."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields(
            DASHBOARD, PERIOD, "finance.seal", "finance.fiscalyear",
            "finance.deferredincome", "finance.interbranch",
        )

    @classmethod
    def hiding(cls, tenant, *fields, resource=DASHBOARD, keys=ALL_KEYS, branch=None):
        """A user holding ``keys`` whose role has Read off on each of ``fields``."""
        number = next(_counter)
        role_key = f"fa-dash-{number}"
        user = cls.grant(
            cls.user_for(tenant, f"fa-dash-{number}@corona.test"), *keys,
            tenant=tenant, role_key=role_key, branch=branch,
        )
        if fields:
            role = TenantRoleTemplate.objects.get(tenant=tenant, key=role_key)
            set_field_access(
                role, *[f"{resource}.{name}" for name in fields], read=False, write=False,
            )
        return user

    def get(self, user, tab, entity=None):
        entity = entity or self.books
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/{URLS[tab]}?entity={entity.code}&window=month&period=1",
        )
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data["data"]

    def reader_for(self, user, tenant=None):
        tenant = tenant or self.tenant
        return DashboardReader.for_user(user, tenant, request=_request_for(user, tenant))


class RegistryShapeTests(SimpleTestCase):
    """Every figure here is server-owned: read-only, open by default, a tenant's."""

    def test_every_field_is_read_only_open_by_default_and_tenant_scoped(self):
        for resource in ("dashboard", "period", "seal", "fiscalyear"):
            for spec in get_declaration("finance", resource).fields:
                with self.subTest(resource=resource, field=spec.name):
                    self.assertFalse(spec.writable)
                    self.assertFalse(spec.sensitive)
                    self.assertEqual(spec.scope, "TENANT")

    def test_every_close_check_finance_registers_is_a_field_of_the_period_resource(self):
        from vs_finance.close import CHECK_TITLES
        from vs_finance.close_access import OWN_CHECKS

        self.assertLessEqual(set(CHECK_TITLES), OWN_CHECKS)


class DashboardCardSwitchTests(_DashboardFieldFixture):
    """One switch per card on each tab: absent when closed, present by default."""

    def test_every_figure_a_payload_carries_has_a_switch(self):
        """A card added to a tab without a switch fails here."""
        for tab in (OVERVIEW, RECEIVABLES, SPEND):
            specs = _specs(tab, shared=("collections", "receivables_summary")
                           if tab == RECEIVABLES else ())
            data = self.get(self.hiding(self.tenant), tab)
            cards = {n for s in specs if not s.name.startswith("kpi_") for n in s.resolved_api_names}
            with self.subTest(tab=tab):
                self.assertEqual(set(data) - NOT_CARDS, cards)
            if tab == OVERVIEW:
                kpis = {n for s in specs if s.name.startswith("kpi_") for n in s.resolved_api_names}
                self.assertEqual(set(data["kpis"]), kpis)

    def check_tab(self, tab, *, shared=()):
        opener = self.hiding(self.tenant)
        baseline = self.get(opener, tab)
        for spec in _specs(tab, shared=shared):
            names = set(spec.resolved_api_names)
            in_kpis = spec.name.startswith("kpi_")
            with self.subTest(tab=tab, card=spec.name):
                top = baseline["kpis"] if in_kpis else baseline
                self.assertLessEqual(names, set(top), "a card is present by default")
                data = self.get(self.hiding(self.tenant, spec.name), tab)
                shown = data["kpis"] if in_kpis else data
                self.assertEqual(set(shown), set(top) - names, "only this card goes")
                if in_kpis:
                    self.assertEqual(set(data), set(baseline))
                elif tab == OVERVIEW:
                    self.assertEqual(set(data["kpis"]), set(baseline["kpis"]))

    def test_every_overview_card_has_its_own_switch(self):
        self.check_tab(OVERVIEW, shared=("collections", "receivables_summary"))

    def test_every_receivables_card_has_its_own_switch(self):
        self.check_tab(RECEIVABLES, shared=("collections", "receivables_summary"))

    def test_every_cash_and_spend_card_has_its_own_switch(self):
        self.check_tab(SPEND)

    def test_one_switch_hides_collections_on_both_tabs(self):
        user = self.hiding(self.tenant, "collections")
        self.assertNotIn("collections", self.get(user, OVERVIEW))
        self.assertNotIn("collections", self.get(user, RECEIVABLES))

    def test_hiding_the_budget_card_leaves_revenue_against_budget(self):
        data = self.get(self.hiding(self.tenant, "budget_lines"), OVERVIEW)
        self.assertNotIn("budget", data)
        self.assertIn("revenue_vs_budget", data)

    def test_the_shown_figures_are_unchanged_by_the_switches(self):
        baseline = self.get(self.hiding(self.tenant), OVERVIEW)
        data = self.get(self.hiding(self.tenant, "channels", "kpi_payables"), OVERVIEW)
        for key in ("collections", "ar_aging", "top_overdue", "trend", "receivables_summary"):
            self.assertEqual(data[key], baseline[key], key)
        self.assertEqual(data["kpis"]["cash_position"], baseline["kpis"]["cash_position"])


class DashboardReachTests(_DashboardFieldFixture):
    """The switches only narrow: permission keys, branches and tenants still decide first."""

    def test_a_card_the_keys_withhold_stays_none_and_a_closed_card_is_absent(self):
        user = self.hiding(self.tenant, keys=("finance.report.view",))
        data = self.get(user, OVERVIEW)
        self.assertIn("bank_accounts", data)
        self.assertIsNone(data["bank_accounts"])
        self.assertIsNone(data["top_payers"])

        closed = self.get(
            self.hiding(self.tenant, "bank_accounts", "top_payers", keys=("finance.report.view",)),
            OVERVIEW,
        )
        self.assertNotIn("bank_accounts", closed)
        self.assertNotIn("top_payers", closed)

    def test_a_clerk_with_open_switches_gets_the_page_with_no_figures(self):
        user = self.hiding(self.tenant, keys=("payments.payout.view",))
        data = self.get(user, OVERVIEW)
        self.assertTrue(all(value is None for value in data["kpis"].values()))
        for block in ("revenue_vs_budget", "ar_aging", "trend", "close_progress"):
            self.assertIsNone(data[block], block)

    def test_a_branch_bursar_stays_narrowed_to_their_branch_with_a_card_closed(self):
        keys = ("finance.invoice.view", "finance.payment.view")
        closed_aging = self.hiding(self.tenant, "ar_aging", keys=keys, branch=self.ikeja)
        data = self.get(closed_aging, OVERVIEW)
        self.assertTrue(data["narrowed"])
        self.assertNotIn("ar_aging", data)
        # Tunde owes 40,000 of Ikeja's 100,000; Aisha's Lekki balance is never counted.
        self.assertEqual(data["kpis"]["receivables"]["value"]["kobo"], 40_000)

        closed_kpi = self.hiding(self.tenant, "kpi_receivables", keys=keys, branch=self.ikeja)
        data = self.get(closed_kpi, OVERVIEW)
        self.assertNotIn("receivables", data["kpis"])
        self.assertEqual(data["ar_aging"]["total"]["kobo"], 40_000)

    def test_a_branch_bursars_collections_cover_their_branch_whether_or_not_others_are_closed(self):
        keys = ("finance.invoice.view", "finance.payment.view")
        user = self.hiding(self.tenant, "channels", keys=keys, branch=self.ikeja)
        data = self.get(user, OVERVIEW)
        self.assertEqual(data["collections"]["collected"]["kobo"], 60_000)
        self.assertIsNone(data["branches"])

    def test_a_single_branch_school_reads_the_same_switches(self):
        solo = self.solo_tenant
        keys = ("finance.report.view", "finance.invoice.view", "finance.payment.view")
        opener = self.hiding(solo, keys=keys)
        data = self.get(opener, OVERVIEW, self.solo_books)
        self.assertIn("branches", data)
        self.assertIsNone(data["branches"])  # One branch: no comparison to draw.

        closed = self.get(
            self.hiding(solo, "kpi_payables", "branch_comparison", keys=keys),
            OVERVIEW, self.solo_books,
        )
        self.assertNotIn("branches", closed)
        self.assertNotIn("payables", closed["kpis"])
        self.assertIn("cash_position", closed["kpis"])

    def test_another_tenants_switches_do_not_reach_this_one(self):
        self.hiding(self.tenant, "collections", "kpi_cash_position")
        rival_user = self.hiding(
            self.rival_tenant, keys=("finance.report.view", "finance.invoice.view"),
        )
        data = self.get(rival_user, OVERVIEW, self.rival_books)
        self.assertIn("collections", data)
        self.assertIn("cash_position", data["kpis"])

    def test_this_tenants_closed_cards_never_open_another_tenants_books(self):
        user = self.hiding(self.tenant, "collections")
        response = TenantAPIClient(user=user).get(
            f"/v1/finance/reports/dashboard/?entity={self.rival_books.code}",
        )
        self.assertEqual(response.status_code, 404)


class DerivedItemTests(_DashboardFieldFixture):
    """An item that restates a card follows that card's switch."""

    def attention(self, user, **kwargs):
        d = finance_dashboard(
            self.books, period=self.period, window="month",
            reader=self.reader_for(user), user=user, **kwargs,
        )
        return d

    def keys_of(self, items):
        return {i["key"] for i in items}

    def test_the_bank_lines_item_needs_the_bank_balances_card(self):
        from vs_finance.models import BankAccount, BankStatementLine

        bank = BankAccount.objects.create(
            entity=self.books, name="Operations",
            gl_account=Account.objects.get(entity=self.books, code="1100"),
        )
        BankStatementLine.objects.create(
            bank_account=bank, txn_date=datetime.date(2026, 1, 20), amount=5_000,
        )
        keys = ("finance.bankaccount.view",)
        open_items = self.attention(self.hiding(self.tenant, keys=keys))["attention"]
        self.assertIn("bank_lines", self.keys_of(open_items))

        closed = self.attention(self.hiding(self.tenant, "bank_accounts", keys=keys))
        self.assertEqual(self.keys_of(closed["attention"]), set())
        self.assertIsNone(closed["bank_accounts"])

    def _behind_plan(self):
        from vs_finance.installments import activate_payment_plan, build_installments
        from vs_finance.models import PaymentPlan

        invoice = self.posted(self.invoice(self.books, self.aisha, self.lekki), reference="FEE:T1")
        plan = PaymentPlan.objects.create(
            entity=self.books, customer=self.aisha, invoice=invoice, branch=self.lekki,
            start_date=datetime.date(2026, 1, 12), frequency="MONTHLY", installment_count=2,
            total_amount=100_000,
        )
        build_installments(plan)
        activate_payment_plan(plan)

    def test_payment_plan_items_need_the_payment_plans_card(self):
        self._behind_plan()
        keys = ("finance.paymentplan.view",)
        opened = self.attention(self.hiding(self.tenant, keys=keys))
        self.assertIn("plans_behind", self.keys_of(opened["attention"]))
        self.assertIn("instalments", {i["kind"] for i in opened["upcoming"]})

        closed = self.attention(self.hiding(self.tenant, "payment_plans", keys=keys))
        self.assertNotIn("plans_behind", self.keys_of(closed["attention"]))
        self.assertNotIn("instalments", {i["kind"] for i in closed["upcoming"]})

    def test_the_upcoming_tax_item_needs_the_tax_calendar_and_the_attention_item_tax_owed(self):
        from vs_finance.models import TaxFiling, TaxObligation

        vat = TaxObligation.objects.get(entity=self.books, code="VAT")
        TaxFiling.objects.create(
            entity=self.books, obligation=vat, period_start=datetime.date(2026, 1, 1),
            period_end=datetime.date(2026, 1, 31), due_date=datetime.date(2026, 2, 5),
            amount_due=20_000,
        )
        keys = ("finance.tax.view",)
        opened = self.attention(self.hiding(self.tenant, keys=keys))
        self.assertIn("tax", self.keys_of(opened["attention"]))
        self.assertIn("tax", {i["kind"] for i in opened["upcoming"]})

        no_calendar = self.attention(self.hiding(self.tenant, "tax_calendar", keys=keys))
        self.assertIn("tax", self.keys_of(no_calendar["attention"]))
        self.assertNotIn("tax", {i["kind"] for i in no_calendar["upcoming"]})

        no_owed = self.attention(self.hiding(self.tenant, "tax_owed", keys=keys))
        self.assertNotIn("tax", self.keys_of(no_owed["attention"]))
        self.assertIn("tax", {i["kind"] for i in no_owed["upcoming"]})

    def test_the_unapplied_receipts_item_needs_credit_held_for_payers(self):
        from vs_finance.receivables import post_payment
        from vs_finance.models import Payment

        payment = Payment.objects.create(
            entity=self.books, customer=self.tunde, branch=self.ikeja, method="CASH",
            payment_date=datetime.date(2026, 1, 20), amount=7_000,
            deposit_account=Account.objects.get(entity=self.books, code="1100"),
        )
        post_payment(payment, allocations=[])
        keys = ("finance.payment.view",)
        self.assertIn(
            "unallocated", self.keys_of(self.attention(self.hiding(self.tenant, keys=keys))["attention"]),
        )
        closed = self.attention(self.hiding(self.tenant, "credit_held", keys=keys))
        self.assertNotIn("unallocated", self.keys_of(closed["attention"]))

    def test_the_budget_lines_need_revenue_against_budget(self):
        from vs_finance.models import Budget, BudgetLine

        budget = Budget.objects.create(
            entity=self.books, branch=None, fiscal_year=self.period.fiscal_year, name="Plan",
        )
        BudgetLine.objects.create(
            budget=budget, account=Account.objects.get(entity=self.books, code="5300"),
            period_no=1, amount=80_000,
        )
        keys = ("finance.report.view",)
        opened = self.attention(self.hiding(self.tenant, keys=keys))
        self.assertIsNotNone(opened["budget"])
        self.assertIsNotNone(opened["revenue_vs_budget"])

        closed = self.attention(self.hiding(self.tenant, "revenue_vs_budget", keys=keys))
        self.assertIsNone(closed["revenue_vs_budget"])
        self.assertIsNone(closed["budget"])

    def test_the_close_progress_card_counts_only_the_checks_the_reader_may_read(self):
        keys = ("finance.period.view",)
        opened = self.attention(self.hiding(self.tenant, keys=keys))["close_progress"]
        names = {c["name"] for c in opened["checks"]}
        self.assertIn("trial_balance_balanced", names)

        closed = self.attention(
            self.hiding(self.tenant, "trial_balance_balanced", resource=PERIOD, keys=keys),
        )["close_progress"]
        self.assertNotIn("trial_balance_balanced", {c["name"] for c in closed["checks"]})
        self.assertEqual(closed["total"], opened["total"] - 1)
        self.assertEqual(closed["total"], len(closed["checks"]))

    def test_a_reader_without_a_request_reads_every_card(self):
        self.assertTrue(EVERY_BLOCK.shows("collections", "tax_owed", "bank_accounts"))


class CloseCheckReadabilityTests(_DashboardFieldFixture):
    """The one question every restatement of a check asks."""

    def request(self, *fields, resource=PERIOD):
        return _request_for(self.hiding(self.tenant, *fields, resource=resource), self.tenant)

    def test_a_check_is_read_under_its_own_switch(self):
        request = self.request("ar_reconciled")
        self.assertFalse(check_readable(request, "ar_reconciled"))
        self.assertTrue(check_readable(request, "trial_balance_balanced"))

    def test_a_check_another_module_adds_is_read_under_other_checks(self):
        request = self.request("other_checks")
        for name in ("ap_reconciled", "grir_explained", "a_check_added_tomorrow"):
            self.assertFalse(check_readable(request, name), name)
        self.assertTrue(check_readable(request, "ar_reconciled"))

    def test_a_check_that_restates_a_figure_needs_that_figure_too(self):
        for check, resource, hidden in (
            ("deferred_income_released", "finance.deferredincome", "pending_balance"),
            ("sealed_figures_unchanged", "finance.seal", "verdict"),
            ("inter_branch_balanced", "finance.interbranch", "net_owed"),
        ):
            with self.subTest(check=check):
                request = self.request(hidden, resource=resource)
                self.assertTrue(can_read(request, f"{PERIOD}.{check}"))
                self.assertFalse(check_readable(request, check))

    def test_a_request_free_render_reads_every_check(self):
        self.assertTrue(check_readable(None, "ar_reconciled"))


class _CloseFixture(_Books):
    """Lagoon View (Ikeja, Lekki) and Harbour Primary (one branch) with the fields installed."""

    CLOSE_KEYS = (
        "finance.period.view", "finance.period.close", "finance.period.reopen",
        "finance.period.lock", "finance.seal.view", "finance.fiscalyear.archive",
        "finance.fiscalyear.reopen",
    )

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        install_declared_fields(
            PERIOD, "finance.seal", "finance.fiscalyear", "finance.deferredincome",
            "finance.interbranch",
        )

    @classmethod
    def staff(cls, school, branch, label, *fields, resource=PERIOD, role_branch=None):
        """A user with the close keys whose role has Read off on each of ``fields``."""
        number = next(_counter)
        role = make_role(school, name=f"Closer {label} {number}")
        for key in cls.CLOSE_KEYS:
            make_role_permission(role, make_permission(key))
        user = make_school_admin(branch, email=f"{label}-{number}@closing.example.com")
        make_assignment(school, user, role, branch=role_branch)
        if fields:
            set_field_access(
                role, *[f"{resource}.{name}" for name in fields], read=False, write=False,
            )
        return user

    def lagoon_staff(self, label, *fields, resource=PERIOD):
        return self.staff(self.lagoon, self.ikeja, label, *fields, resource=resource)

    def harbour_staff(self, label, *fields, resource=PERIOD):
        return self.staff(self.harbour, self.harbour_main, label, *fields, resource=resource)

    def checklist_url(self, period, books):
        return f"/v1/finance/periods/{period.pk}/checklist/?entity={books.code}"

    def checklist(self, user, period=None, books=None):
        books = books or self.books
        period = period or self.month(1, books)
        response = TenantAPIClient(user=user).get(self.checklist_url(period, books))
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data["data"]


class ChecklistFieldAccessTests(_CloseFixture):
    """What closing a month would find, check by check."""

    def test_the_checklist_is_complete_by_default(self):
        data = self.checklist(self.lagoon_staff("open"))
        names = {i["name"] for i in data["items"]}
        self.assertLessEqual(
            {"trial_balance_balanced", "no_draft_journals", "ar_reconciled",
             "depreciation_posted", "deferred_income_released", "inter_branch_balanced",
             "sealed_figures_unchanged"},
            names,
        )
        self.assertEqual(data["total"], len(data["items"]))
        self.assertIn("status", data["period"])
        self.assertIn("passed", data)

    def test_each_own_check_has_a_switch(self):
        base = {i["name"] for i in self.checklist(self.lagoon_staff("base"))["items"]}
        for name in sorted(base & {
            "trial_balance_balanced", "no_draft_journals", "ar_reconciled",
            "depreciation_posted", "deferred_income_released", "inter_branch_balanced",
            "sealed_figures_unchanged",
        }):
            with self.subTest(check=name):
                data = self.checklist(self.lagoon_staff(name, name))
                shown = {i["name"] for i in data["items"]}
                self.assertEqual(shown, base - {name})
                self.assertEqual(data["total"], len(base) - 1)
                self.assertIn("period", data)

    def test_the_progress_switch_removes_the_counts_and_leaves_the_checks(self):
        data = self.checklist(self.lagoon_staff("progress", "checklist_progress"))
        for key in ("passed", "done", "total"):
            self.assertNotIn(key, data)
        self.assertTrue(data["items"])
        self.assertIn("period", data)

    def test_the_close_state_switch_removes_the_months_state_and_leaves_the_rest(self):
        data = self.checklist(self.lagoon_staff("state", "close_state"))
        self.assertNotIn("status", data["period"])
        self.assertNotIn("closed_at", data["period"])
        self.assertIn("name", data["period"])
        self.assertTrue(data["items"])
        self.assertIn("passed", data)

    def test_a_deferred_income_check_needs_the_deferred_income_figure(self):
        user = self.lagoon_staff("deferred", "pending_balance", resource="finance.deferredincome")
        names = {i["name"] for i in self.checklist(user)["items"]}
        self.assertNotIn("deferred_income_released", names)
        self.assertIn("ar_reconciled", names)

    def test_the_sealed_figures_check_needs_the_seal_verdict(self):
        user = self.lagoon_staff("sealcheck", "verdict", resource="finance.seal")
        names = {i["name"] for i in self.checklist(user)["items"]}
        self.assertNotIn("sealed_figures_unchanged", names)

    def test_a_one_branch_school_reads_the_same_switches(self):
        books = self.harbour_books
        base = self.checklist(self.harbour_staff("open"), books=books)
        closed = self.checklist(
            self.harbour_staff("closed", "ar_reconciled", "checklist_progress"), books=books,
        )
        self.assertNotIn("ar_reconciled", {i["name"] for i in closed["items"]})
        self.assertNotIn("done", closed)
        self.assertEqual(
            {i["name"] for i in closed["items"]},
            {i["name"] for i in base["items"]} - {"ar_reconciled"},
        )

    def test_another_tenants_switches_do_not_reach_this_one(self):
        self.lagoon_staff("hider", "ar_reconciled", "close_state")
        data = self.checklist(self.harbour_staff("other"), books=self.harbour_books)
        self.assertIn("ar_reconciled", {i["name"] for i in data["items"]})
        self.assertIn("status", data["period"])


class CloseRefusalTests(_CloseFixture):
    """A refusal to close names the failed checks the caller may read, and no others."""

    UNBALANCED = mock.patch(
        "vs_finance.reports.trial_balance",
        return_value=mock.Mock(is_balanced=False, difference=500),
    )

    def close(self, user, period, books, body=None):
        return TenantAPIClient(user=user).post(
            f"/v1/finance/periods/{period.pk}/close/?entity={books.code}",
            {"run_depreciation": False, "release_deferred": False, **(body or {})},
            format="json",
        )

    def test_the_refusal_names_the_failed_check_to_a_reader_of_it(self):
        with self.UNBALANCED:
            response = self.close(self.harbour_staff("reader"), self.month(1, self.harbour_books),
                                  self.harbour_books)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("Debits and credits differ", response.data["message"])
        self.assertEqual(response.data["error"]["detail"]["failures"], ["trial_balance_balanced"])

    def test_the_refusal_leaves_out_a_check_the_caller_may_not_read(self):
        user = self.harbour_staff("hidden", "trial_balance_balanced")
        with self.UNBALANCED:
            response = self.close(user, self.month(1, self.harbour_books), self.harbour_books)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertNotIn("Debits and credits", response.data["message"])
        self.assertNotIn("Trial balance", response.data["message"])
        self.assertIn("not ready to close", response.data["message"])
        self.assertEqual(response.data["error"]["detail"]["failures"], [])

    def test_a_branch_close_in_a_many_branch_school_is_worded_the_same_way(self):
        user = self.lagoon_staff("branchhidden", "trial_balance_balanced")
        with self.UNBALANCED:
            response = self.close(
                user, self.month(1), self.books, body={"branch": self.ikeja.pk},
            )
        self.assertEqual(response.status_code, 409, response.data)
        self.assertNotIn("Debits and credits", response.data["message"])
        self.assertEqual(response.data["error"]["detail"]["failures"], [])

    def test_redaction_rewords_the_refusal_by_what_the_caller_may_read(self):
        from vs_finance.close import ChecklistItem, failures_sentence
        from vs_finance.exceptions import PeriodCloseError

        items = [
            ChecklistItem(name="ar_reconciled", passed=False, detail="Customers owe N1.00"),
            ChecklistItem(name="depreciation_posted_for_year", passed=False,
                          detail="2 charges are not posted"),
        ]

        def refusal():
            return PeriodCloseError(
                f"FY2026 is not ready to close. {failures_sentence(items)}.",
                failures=[i.name for i in items], failure_items=items,
                lead="FY2026 is not ready to close.",
            )

        tenant = self.books.tenant
        some = redact_close_refusal(
            _request_for(self.lagoon_staff("some", "ar_reconciled"), tenant), refusal(),
        )
        self.assertNotIn("Customers owe", some.message)
        self.assertIn("2 charges are not posted", some.message)
        self.assertEqual(some.failures, ["depreciation_posted_for_year"])
        self.assertEqual(some.extra["failures"], ["depreciation_posted_for_year"])

        none = redact_close_refusal(
            _request_for(
                self.lagoon_staff("none", "ar_reconciled", "depreciation_posted_for_year"), tenant,
            ),
            refusal(),
        )
        self.assertEqual(none.message, "FY2026 is not ready to close.")
        self.assertEqual(none.failures, [])

        every = redact_close_refusal(_request_for(self.lagoon_staff("every"), tenant), refusal())
        self.assertIn("Customers owe", every.message)
        self.assertEqual(len(every.failures), 2)

    def test_redacting_without_failed_items_leaves_the_error_alone(self):
        from vs_finance.exceptions import PeriodCloseError

        error = PeriodCloseError("Nothing to redact.", failures=["later_periods_open"])
        request = _request_for(self.lagoon_staff("plain", "ar_reconciled"), self.books.tenant)
        self.assertIs(redact_close_refusal(request, error), error)
        self.assertEqual(error.message, "Nothing to redact.")


class CloseResponseFieldAccessTests(_CloseFixture):
    """The response to closing, reopening and locking a month."""

    def close_branch(self, user, branch):
        return TenantAPIClient(user=user).post(
            f"/v1/finance/periods/{self.month(1).pk}/close/?entity={self.books.code}",
            {"branch": branch.pk, "run_depreciation": False, "release_deferred": False},
            format="json",
        )

    def test_the_branch_state_and_checklist_come_back_by_default(self):
        response = self.close_branch(self.lagoon_staff("opener"), self.ikeja)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(set(data), {"period", "branch_period", "checklist"})
        self.assertEqual(data["branch_period"]["status"], "CLOSED")
        self.assertIn("is now closed at Ikeja Branch", response.data["message"])
        self.assertTrue(data["checklist"]["items"])

    def test_a_closed_state_switch_removes_it_everywhere_the_response_says_it(self):
        user = self.lagoon_staff("state", "close_state")
        response = self.close_branch(user, self.ikeja)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertNotIn("branch_period", data)
        self.assertNotIn("status", data["period"])
        self.assertNotIn("closed_at", data["period"])
        self.assertIn("close recorded", response.data["message"])
        self.assertNotIn(" is now ", response.data["message"])
        self.assertTrue(data["checklist"]["items"])

    def test_a_closed_check_is_missing_from_the_closing_checklist(self):
        user = self.lagoon_staff("check", "ar_reconciled", "checklist_progress")
        response = self.close_branch(user, self.lekki)
        self.assertEqual(response.status_code, 200, response.data)
        checklist = response.data["data"]["checklist"]
        self.assertNotIn("passed", checklist)
        self.assertNotIn("ar_reconciled", {i["name"] for i in checklist["items"]})

    def test_one_branch_school_close_has_no_branch_state_and_follows_the_same_switch(self):
        user = self.harbour_staff("solo", "close_state")
        response = TenantAPIClient(user=user).post(
            f"/v1/finance/periods/{self.month(1, self.harbour_books).pk}/close/"
            f"?entity={self.harbour_books.code}",
            {"run_depreciation": False, "release_deferred": False}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(set(response.data["data"]), {"period", "checklist"})
        self.assertNotIn("status", response.data["data"]["period"])
        self.assertIn("close recorded", response.data["message"])

    def test_reopen_and_lock_follow_the_close_state_switch(self):
        open_user = self.lagoon_staff("cycle")
        self.close_branch(open_user, self.ikeja)
        client = TenantAPIClient(user=self.lagoon_staff("hidestate", "close_state"))
        period = self.month(1)
        url = f"/v1/finance/periods/{period.pk}/%s/?entity={self.books.code}"
        reopened = client.post(
            url % "reopen", {"branch": self.ikeja.pk, "reason": "Fix a posting."}, format="json",
        )
        self.assertEqual(reopened.status_code, 200, reopened.data)
        self.assertEqual(set(reopened.data["data"]), {"period"})
        self.assertNotIn("status", reopened.data["data"]["period"])

        self.close_branch(open_user, self.ikeja)
        locked = client.post(url % "lock", {"branch": self.ikeja.pk}, format="json")
        self.assertEqual(locked.status_code, 200, locked.data)
        self.assertNotIn("branch_period", locked.data["data"])

    def test_the_period_list_follows_the_close_state_switch(self):
        base_url = f"/v1/finance/periods/?entity={self.books.code}&include_branches=true"
        opened = TenantAPIClient(user=self.lagoon_staff("list")).get(base_url)
        row = opened.data["data"][0]
        self.assertIn("status", row)
        self.assertIn("branch_states", row)

        closed = TenantAPIClient(user=self.lagoon_staff("liststate", "close_state")).get(base_url)
        row = closed.data["data"][0]
        for key in ("status", "closed_at", "branch_states"):
            self.assertNotIn(key, row)
        self.assertIn("label", row)

    def test_filtering_periods_by_a_hidden_state_is_refused(self):
        url = f"/v1/finance/periods/?entity={self.books.code}&status=OPEN"
        allowed = TenantAPIClient(user=self.lagoon_staff("filter")).get(url)
        self.assertEqual(allowed.status_code, 200)
        refused = TenantAPIClient(user=self.lagoon_staff("filterhidden", "close_state")).get(url)
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(refused.data["error"]["code"], "field_read_denied")


class SealFieldAccessTests(_CloseFixture):
    """The result of verifying the sealed figures."""

    def verify(self, user, books=None):
        books = books or self.books
        response = TenantAPIClient(user=user).get(f"/v1/finance/seals/verify/?entity={books.code}")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response

    def tamper(self):
        """Close January, then change a line behind the ledger so the seal no longer matches."""
        from vs_finance.close import close_period
        from vs_finance.models import JournalLine

        entry = self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)
        close_period(self.books, self.month(1), run_depreciation=False)
        line = entry.lines.get(account__code="1100")
        JournalLine.objects.filter(pk=line.pk).update(debit=90_000)
        JournalLine.objects.filter(entry=entry, account__code="4100").update(credit=90_000)

    def setUp(self):
        super().setUp()
        self.tamper()

    def seal_user(self, label, *fields):
        return self.staff(self.lagoon, self.ikeja, label, *fields, resource="finance.seal")

    def test_every_field_is_present_by_default(self):
        response = self.verify(self.seal_user("open"))
        data = response.data["data"]
        self.assertEqual(set(data), {"ok", "checked", "mismatches", "chain_breaks", "checks"})
        row = data["checks"][0]
        self.assertEqual(
            set(row),
            {"seal_id", "label", "kind", "fiscal_year", "period_id", "sealed_at",
             "seal_checksum", "line_count", "line_count_now", "ok", "seal_intact",
             "lines_match", "summary", "differences"},
        )
        self.assertFalse(data["ok"])
        self.assertEqual(response.data["message"], "Closed figures differ from the ledger.")

    def test_each_switch_removes_its_own_names(self):
        base = self.verify(self.seal_user("base")).data["data"]
        cases = {
            "verdict": ({"ok", "checked", "mismatches"}, {"ok"}),
            "chain_breaks": ({"chain_breaks"}, set()),
            "seal_name": (set(), {"seal_id", "label", "kind", "fiscal_year", "period_id"}),
            "sealed_at": (set(), {"sealed_at"}),
            "seal_checksum": (set(), {"seal_checksum"}),
            "line_counts": (set(), {"line_count", "line_count_now"}),
            "seal_agreement": (set(), {"seal_intact", "lines_match"}),
            "differences": (set(), {"differences", "summary"}),
        }
        for field, (top, row) in cases.items():
            with self.subTest(field=field):
                data = self.verify(self.seal_user(field, field)).data["data"]
                self.assertEqual(set(data), set(base) - top)
                self.assertEqual(set(data["checks"][0]), set(base["checks"][0]) - row)

    def test_closing_the_list_removes_every_row_and_keeps_the_verdict(self):
        data = self.verify(self.seal_user("nolist", "checks")).data["data"]
        self.assertNotIn("checks", data)
        self.assertIn("ok", data)

    def test_the_message_does_not_restate_a_hidden_verdict(self):
        response = self.verify(self.seal_user("quiet", "verdict"))
        self.assertEqual(response.data["message"], "Closed figures checked.")

    def test_a_one_branch_school_and_another_tenant_are_unaffected(self):
        self.seal_user("hider", "verdict", "differences")
        harbour = self.harbour_staff("seals", resource="finance.seal")
        response = self.verify(harbour, self.harbour_books)
        self.assertIn("ok", response.data["data"])
        self.assertEqual(response.data["data"]["checked"], 0)


class FiscalYearFieldAccessTests(_CloseFixture):
    """A year's close state, archive state and the result its close carried to equity."""

    LATER = datetime.date(2029, 2, 1)
    TODAY = "vs_config.clock.tenant_today"

    def year_user(self, label, *fields):
        return self.staff(self.lagoon, self.ikeja, label, *fields, resource="finance.fiscalyear")

    def years(self, user, query=""):
        return TenantAPIClient(user=user).get(
            f"/v1/finance/fiscal-years/?entity={self.books.code}{query}",
        )

    def close_year(self, books=None):
        from vs_finance.close import close_fiscal_year
        from vs_finance.constants import PeriodStatus

        FiscalPeriod.objects.filter(
            fiscal_year=self.year(books), is_closing=False,
        ).update(status=PeriodStatus.CLOSED)
        return close_fiscal_year(books or self.books, self.year(books))

    def test_the_year_list_carries_its_states_by_default(self):
        row = self.years(self.year_user("open"), "&include_branches=true").data["data"][0]
        for key in ("status", "is_archived", "archived_at", "branch_states"):
            self.assertIn(key, row)

    def test_the_close_state_switch_removes_status_and_branch_states(self):
        row = self.years(
            self.year_user("state", "close_state"), "&include_branches=true",
        ).data["data"][0]
        self.assertNotIn("status", row)
        self.assertNotIn("branch_states", row)
        self.assertIn("is_archived", row)

    def test_the_archive_switch_removes_the_archive_state_only(self):
        row = self.years(self.year_user("archive", "archive_state")).data["data"][0]
        self.assertNotIn("is_archived", row)
        self.assertNotIn("archived_at", row)
        self.assertIn("status", row)

    def test_filters_on_a_hidden_state_are_refused(self):
        hidden_state = self.year_user("fstate", "close_state")
        self.assertEqual(self.years(hidden_state, "&status=OPEN").status_code, 403)
        hidden_archive = self.year_user("farchive", "archive_state")
        refused = self.years(hidden_archive, "&include_archived=true")
        self.assertEqual(refused.status_code, 403, refused.data)
        self.assertEqual(self.years(hidden_archive).status_code, 200)

    def test_the_archive_response_follows_the_archive_switch(self):
        self.close_year()
        user = self.year_user("archiver", "archive_state")
        with mock.patch(self.TODAY, return_value=self.LATER):
            response = TenantAPIClient(user=user).post(
                f"/v1/finance/fiscal-years/{self.year().pk}/archive/?entity={self.books.code}",
                {"reason": "Audited and filed."}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("is_archived", response.data["data"])
        self.assertNotIn("archived_at", response.data["data"])
        self.assertIsNotNone(self.year().archived_at)

    def test_the_year_close_result_follows_the_net_result_switch(self):
        self.fees(self.ikeja, datetime.date(2026, 1, 15), 100_000)
        FiscalPeriod.objects.filter(
            fiscal_year=self.year(), is_closing=False,
        ).update(status="CLOSED")
        url = f"/v1/finance/fiscal-years/{self.year().pk}/close/?entity={self.books.code}"
        hidden = TenantAPIClient(user=self.year_user("netless", "net_income")).post(
            url, {"branch": self.ikeja.pk}, format="json",
        )
        self.assertEqual(hidden.status_code, 200, hidden.data)
        self.assertNotIn("net_income", hidden.data["data"])
        self.assertIn("fiscal_year", hidden.data["data"])
        self.assertIn("closing_journals", hidden.data["data"])

    def test_a_one_branch_school_archives_with_the_same_switch(self):
        books = self.harbour_books
        self.close_year(books)
        user = self.staff(
            self.harbour, self.harbour_main, "solo", "archive_state", resource="finance.fiscalyear",
        )
        with mock.patch(self.TODAY, return_value=self.LATER):
            response = TenantAPIClient(user=user).post(
                f"/v1/finance/fiscal-years/{self.year(books).pk}/archive/?entity={books.code}",
                {"reason": "Audited and filed."}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("is_archived", response.data["data"])
        self.assertIn("status", response.data["data"])
