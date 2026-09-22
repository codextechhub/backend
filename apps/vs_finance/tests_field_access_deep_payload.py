"""No bank number or pay figure reaches a caller who may read none, at any depth.

Every declared surface of ``finance.bankaccount``, ``finance.payrollrun`` and
``finance.salary`` is rendered on a real record whose registered fields are
filled in, as a caller whose role has every field of that resource switched
off, and the whole payload is walked.

Two tenants: one with two branches, whose rows are posted to a branch, and one
with a single branch, whose rows are shared across it.
"""
from __future__ import annotations

import datetime

from django.test import TestCase

from vs_finance.models import (
    Account,
    BankAccount,
    EmployeeSalary,
    FiscalPeriod,
    FiscalYear,
    LedgerEntity,
    PayrollLine,
    PayrollRun,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import make_branch, make_school

_SURFACE = "vs_finance.serializers."


class FinanceDeepPayloadTests(DeepPayloadChecks, TestCase):
    resources = frozenset({"finance.bankaccount", "finance.payrollrun", "finance.salary"})
    covers = frozenset({
        _SURFACE + "BankAccountSerializer",
        _SURFACE + "PayrollLineSerializer",
        _SURFACE + "EmployeeSalarySerializer",
    })

    @classmethod
    def setUpTestData(cls):
        seed_currencies()
        multi = make_school(slug="deep-fin-multi", name="Corona Group").tenant
        ikeja = make_branch(multi, name="Ikeja Branch")
        make_branch(multi, name="Lekki Branch", is_main=False)
        solo = make_school(slug="deep-fin-solo", name="Single Site").tenant
        make_branch(solo, name="Main Branch")
        cls.tenant = multi
        cls.records = [
            (multi, cls._books(multi, "DPMULTI", branch=ikeja)),
            (solo, cls._books(solo, "DPSOLO", branch=None)),
        ]

    @classmethod
    def _books(cls, tenant, code, *, branch):
        entity = LedgerEntity.objects.create(
            name=f"{code} Books", code=code, kind=LedgerEntity.Kind.TENANT, tenant=tenant,
        )
        seed_chart_of_accounts(entity)
        year = FiscalYear.objects.create(
            entity=entity, year=2026,
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 12, 31),
        )
        FiscalPeriod.objects.create(
            entity=entity, fiscal_year=year, period_no=1, name="Jan 2026",
            start_date=datetime.date(2026, 1, 1), end_date=datetime.date(2026, 1, 31),
        )
        bank = BankAccount.objects.create(
            entity=entity, branch=branch, name="GTBank Operations",
            bank_name="GTBank", account_number="0123456789",
            gl_account=Account.objects.get(entity=entity, code="1100"),
        )
        run = PayrollRun.objects.create(
            entity=entity, branch=branch, pay_date=datetime.date(2026, 1, 28),
        )
        line = PayrollLine.objects.create(
            run=run, employee_name="Ngozi Okafor", gross_amount=250_000_00,
            paye_amount=20_000_00, pension_amount=20_000_00, net_amount=210_000_00,
            components=[{"name": "Basic", "kind": "EARNING", "amount": 250_000_00}],
            line_no=1,
        )
        salary = EmployeeSalary.objects.create(
            entity=entity, branch=branch, name="Ngozi Okafor",
            gross_amount=250_000_00, paye_amount=20_000_00,
            pension_amount=20_000_00,
        )
        return bank, line, salary

    def samples(self):
        samples = []
        for tenant, (bank, line, salary) in self.records:
            samples += [
                Sample(_SURFACE + "BankAccountSerializer", bank, tenant=tenant),
                Sample(_SURFACE + "PayrollLineSerializer", line, tenant=tenant),
                Sample(_SURFACE + "EmployeeSalarySerializer", salary, tenant=tenant),
            ]
        return samples
