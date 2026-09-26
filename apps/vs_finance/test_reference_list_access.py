"""Who may read the lists every finance report filters by.

Accounts, tax codes, currencies, fiscal years, fiscal periods, cost centres and
dimensions are the options behind the report filters, the pickers on invoice,
bill and budget lines, and the New budget form. Each is a name and a code
for books the caller is already entitled to, so reading them follows module
membership: anyone holding a finance key may read them, nobody else may, and
creating one still needs its own key. Account balances are the entity's whole
ledger and stay behind ``finance.account.view``.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from vs_finance.models import Account, CostCenter, Currency, TaxCode, Dimension, FiscalPeriod, FiscalYear, LedgerEntity
from vs_finance.views import AccountListCreateView, FiscalPeriodListView, FiscalYearListView
from vs_finance.views_ops.masterdata import (
    CostCenterListCreateView,
    CurrencyListCreateView,
    DimensionListCreateView,
    TaxCodeListCreateView,
)
from vs_rbac.tests.helpers import (
    make_assignment,
    make_permission,
    make_role,
    make_role_permission,
)
from vs_tenants.models import Tenant


class FinanceReferenceListAccessTests(TestCase):
    """A report reader can fill every report filter without the setup keys."""

    ENDPOINTS = (
        ("/v1/finance/accounts/", AccountListCreateView),
        ("/v1/finance/tax-codes/", TaxCodeListCreateView),
        ("/v1/finance/fiscal-years/", FiscalYearListView),
        ("/v1/finance/periods/", FiscalPeriodListView),
        ("/v1/finance/cost-centers/", CostCenterListCreateView),
        ("/v1/finance/dimensions/", DimensionListCreateView),
    )

    def setUp(self):
        self.tenant = Tenant.objects.get(slug="codex")
        self.entity = LedgerEntity.objects.create(
            name="Reference Access Books", code="REFACCESS",
            kind=LedgerEntity.Kind.TENANT, tenant=self.tenant,
        )
        year = FiscalYear.objects.create(
            entity=self.entity, year=2026,
            start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=self.entity, fiscal_year=year, period_no=1, name="2026-01",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 31),
        )
        CostCenter.objects.create(entity=self.entity, code="SCI", name="Science")
        Dimension.objects.create(entity=self.entity, code="FUND", name="Fund")
        vat = Account.objects.create(entity=self.entity, code="230000", name="VAT payable", account_type="LIABILITY")
        TaxCode.objects.create(entity=self.entity, code="VAT", name="VAT", rate_bps=750, collected_account=vat)
        self.factory = APIRequestFactory()

    def user_holding(self, *permission_keys, email):
        user = get_user_model().objects.create_user(
            email=email, password="x", status="ACTIVE",
            first_name="Reference", last_name="Reader", tenant=self.tenant,
        )
        role = make_role(self.tenant, name=f"Role {email}")
        for key in permission_keys:
            make_role_permission(role, make_permission(key))
        make_assignment(self.tenant, user, role)
        return user

    def call(self, view, path, user, *, method="get", data=None, entity=None, params=None):
        entity = entity or self.entity.code
        if method == "post":
            request = self.factory.post(f"{path}?entity={entity}", data or {}, format="json")
        else:
            request = self.factory.get(path, {"entity": entity, **(params or {})})
        force_authenticate(request, user=user)
        request.tenant = user.tenant
        request.rbac_tenant = user.tenant
        response = view.as_view()(request)
        response.render()
        return response

    def test_caller_without_a_finance_key_is_refused_every_list(self):
        user = self.user_holding("procurement.vendor.view", email="no-finance@test.com")
        for path, view in self.ENDPOINTS:
            with self.subTest(path=path):
                self.assertEqual(self.call(view, path, user).status_code, 403)

    def test_another_tenants_books_stay_unreachable(self):
        foreign = Tenant.objects.create(
            name="Foreign Reference Tenant", slug="foreign-reference-tenant",
            kind=Tenant.Kind.ORGANIZATION, status=Tenant.Status.ACTIVE,
        )
        foreign_entity = LedgerEntity.objects.create(
            name="Foreign Reference Books", code="REFFOREIGN",
            kind=LedgerEntity.Kind.TENANT, tenant=foreign,
        )
        CostCenter.objects.create(entity=foreign_entity, code="FOREIGN", name="Foreign")
        user = self.user_holding("finance.report.view", email="foreign-reader@test.com")
        for path, view in self.ENDPOINTS:
            with self.subTest(path=path):
                response = self.call(view, path, user, entity=foreign_entity.code)
                self.assertEqual(response.status_code, 404)

    def test_report_reader_reads_every_list(self):
        user = self.user_holding("finance.report.view", email="report-reader@test.com")
        for path, view in self.ENDPOINTS:
            with self.subTest(path=path):
                response = self.call(view, path, user)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.data["data"]), 1)

    def test_budget_author_reads_the_fiscal_years_to_file_a_budget_against(self):
        user = self.user_holding("finance.budget.create", email="budget-author@test.com")
        response = self.call(FiscalYearListView, "/v1/finance/fiscal-years/", user)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["year"] for row in response.data["data"]], [2026])

    def test_currencies_are_read_on_any_finance_key_and_created_on_their_own(self):
        path = "/v1/finance/currencies/"
        reader = self.user_holding("finance.invoice.create", email="currency-reader@test.com")
        self.assertEqual(self.call(CurrencyListCreateView, path, reader).status_code, 200)

        outsider = self.user_holding("procurement.vendor.view", email="currency-outsider@test.com")
        self.assertEqual(self.call(CurrencyListCreateView, path, outsider).status_code, 403)

        writer = self.call(CurrencyListCreateView, path, reader, method="post", data={"code": "XQX"})
        self.assertEqual(writer.status_code, 403)
        self.assertFalse(Currency.objects.filter(code="XQX").exists())

    def test_account_balances_still_need_the_chart_of_accounts_key(self):
        path = "/v1/finance/accounts/"
        reader = self.user_holding("finance.budget.create", email="balance-less@test.com")
        refused = self.call(AccountListCreateView, path, reader, params={"with_balance": "true"})
        self.assertEqual(refused.status_code, 403)

        chart = self.user_holding("finance.account.view", email="chart-reader@test.com")
        allowed = self.call(AccountListCreateView, path, chart, params={"with_balance": "true"})
        self.assertEqual(allowed.status_code, 200)

    def test_report_reader_still_cannot_create_an_account(self):
        user = self.user_holding("finance.report.view", email="account-writer@test.com")
        response = self.call(
            AccountListCreateView, "/v1/finance/accounts/", user, method="post",
            data={"code": "410001", "name": "New"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Account.objects.filter(entity=self.entity, code="410001").exists())

    def test_report_reader_still_cannot_open_a_fiscal_year(self):
        user = self.user_holding("finance.report.view", email="year-opener@test.com")
        response = self.call(
            FiscalYearListView, "/v1/finance/fiscal-years/", user, method="post", data={"year": 2027},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(FiscalYear.objects.filter(entity=self.entity, year=2027).exists())

    def test_report_reader_still_cannot_create_a_cost_centre_or_dimension(self):
        user = self.user_holding("finance.report.view", email="report-writer@test.com")
        writes = (
            ("/v1/finance/cost-centers/", CostCenterListCreateView, CostCenter),
            ("/v1/finance/dimensions/", DimensionListCreateView, Dimension),
        )
        for path, view, model in writes:
            with self.subTest(path=path):
                response = self.call(
                    view, path, user, method="post", data={"code": "NEW", "name": "New"},
                )
                self.assertEqual(response.status_code, 403)
                self.assertFalse(model.objects.filter(entity=self.entity, code="NEW").exists())
