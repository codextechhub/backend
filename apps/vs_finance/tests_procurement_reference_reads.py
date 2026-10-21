"""The finance reference lists every procurement screen reads.

A procurement screen opens by resolving the school's books through the entity
list, and its forms then pick from the chart of accounts, the fiscal years and
periods, the tax codes and the currencies. Each of those is names, codes and
dates with no amounts, so anyone working in procurement reads them exactly as
anyone working in finance does. The bank accounts carry more, so they open only
to a caller who may raise the supplier payment that pays from one.

Bright Star's head of sciences holds the requisition keys alone, and its
storekeeper-buyer holds the supplier payment keys alone; neither holds a
finance key. A member of staff holding only a school key reads none of them.
"""
import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from vs_finance.models import Account, BankAccount, FiscalPeriod, FiscalYear, LedgerEntity
from vs_finance.constants import PeriodStatus


class ProcurementReadsFinanceReferenceListsTests(TestCase):
    def setUp(self):
        from vs_rbac.tests.helpers import make_school

        self.tenant = make_school(slug="bright-ref", name="Bright Star").tenant
        self.entity = LedgerEntity.objects.create(
            name="Bright Star Books", code="BRIGHTREF", kind=LedgerEntity.Kind.TENANT,
            tenant=self.tenant,
        )
        year = FiscalYear.objects.create(
            entity=self.entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=self.entity, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
            status=PeriodStatus.OPEN,
        )
        cash = Account.objects.create(
            entity=self.entity, code="1100", name="Cash & Bank", account_type="ASSET",
            is_postable=True,
        )
        BankAccount.objects.create(
            entity=self.entity, name="Main account", bank_name="Demo Bank",
            account_number="2034517786", gl_account=cash, is_active=True,
        )

    def user_holding(self, *keys, email):
        """An active user whose only permissions are ``keys``."""
        from vs_rbac.tests.helpers import (
            make_assignment, make_permission, make_role, make_role_permission,
        )

        user = get_user_model().objects.create_user(
            email=email, password="x", status="ACTIVE",
            first_name="Ref", last_name="Test", tenant=self.tenant,
        )
        role = make_role(self.tenant, name=f"Role {email}")
        for key in keys:
            make_role_permission(role, make_permission(key))
        make_assignment(self.tenant, user, role)
        return user

    def status_of(self, view_class, path, user):
        req = APIRequestFactory().get(path, {"entity": self.entity.code})
        force_authenticate(req, user=user)
        req.tenant = user.tenant
        resp = view_class.as_view()(req)
        resp.render()
        return resp.status_code

    def reference_lists(self):
        from vs_finance.views import (
            AccountListCreateView, EntityListCreateView, FiscalPeriodListView,
            FiscalYearListView,
        )
        from vs_finance.views_ops.masterdata import CurrencyListCreateView, TaxCodeListCreateView

        return (
            (EntityListCreateView, "/v1/finance/entities/"),
            (AccountListCreateView, "/v1/finance/accounts/"),
            (FiscalPeriodListView, "/v1/finance/periods/"),
            (FiscalYearListView, "/v1/finance/fiscal-years/"),
            (TaxCodeListCreateView, "/v1/finance/tax-codes/"),
            (CurrencyListCreateView, "/v1/finance/currencies/"),
        )

    def bank_accounts(self):
        from vs_finance.views_ops.banking import BankAccountListCreateView

        return BankAccountListCreateView, "/v1/finance/bank-accounts/"

    def test_a_requisition_writer_reads_every_reference_list(self):
        head = self.user_holding(
            "procurement.requisition.create", "procurement.requisition.submit",
            email="head-of-sciences@test.com",
        )
        for view, path in self.reference_lists():
            self.assertEqual(self.status_of(view, path, head), 200, path)

    def test_a_requisition_writer_does_not_read_the_bank_accounts(self):
        head = self.user_holding("procurement.requisition.create", email="requester@test.com")
        self.assertEqual(self.status_of(*self.bank_accounts(), head), 403)

    def test_a_supplier_payer_reads_the_bank_accounts_to_pay_from(self):
        buyer = self.user_holding("procurement.vendor_payment.create", email="buyer@test.com")
        self.assertEqual(self.status_of(*self.bank_accounts(), buyer), 200)

    def test_a_finance_reader_still_reads_the_bank_accounts(self):
        finance_admin = self.user_holding("finance.bankaccount.view", email="finance-admin@test.com")
        self.assertEqual(self.status_of(*self.bank_accounts(), finance_admin), 200)

    def test_a_school_only_member_of_staff_reads_none_of_them(self):
        teacher = self.user_holding("school.students.view", email="teacher@test.com")
        for view, path in (*self.reference_lists(), self.bank_accounts()):
            self.assertEqual(self.status_of(view, path, teacher), 403, path)
