"""No restricted finance field reaches a caller who may read none, at any depth.

Every declared surface of ``finance.bankaccount``, ``finance.payrollrun`` and
``finance.salary``, plus the inter-branch and receivables accrual surfaces
(``finance.deferredincome``, ``finance.provision``, ``finance.deposit`` and
``finance.settings``), is rendered on a real record whose registered fields are
filled in, as a caller whose role has every field of that resource switched
off, and the whole payload is walked.

Two tenants: one with two branches, whose rows are posted to a branch, and one
with a single branch, whose rows are shared across it.

An expense claim (``finance.expenseclaim``) is rendered by
:class:`ExpenseClaimDeepPayloadTests`, with a claimant picked from the staff list
in one school and a typed one in the other.

The dashboards, the month and year close, the sealed-figures verification and the
fiscal year (``finance.dashboard``, ``finance.period``, ``finance.seal`` and
``finance.fiscalyear``) are rendered by :class:`DashboardCloseSealDeepPayloadTests`.
"""
from __future__ import annotations

from vs_finance.account_mappings import account_mapping_snapshot
from vs_finance.banking_settings import (
    resolve_finance_banking_settings,
    serialize_finance_banking_settings,
)
from vs_finance.calendar_settings import (
    resolve_finance_calendar_settings,
    serialize_finance_calendar_settings,
)
from vs_finance.document_settings import (
    resolve_finance_document_settings,
    serialize_finance_document_settings,
)
from vs_finance.payroll_settings import (
    resolve_finance_payroll_settings,
    serialize_finance_payroll_settings,
)

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase

from vs_finance.models import (
    Account,
    BankAccount,
    BankTransaction,
    BankTransfer,
    CreditNote,
    CreditNoteLine,
    Concession,
    CustomerCreditTransfer,
    CustomerDeposit,
    DepositForfeiture,
    DoubtfulDebtProvision,
    DoubtfulDebtProvisionLine,
    Invoice,
    InvoiceLine,
    PaymentPlan,
    PaymentPlanInstallment,
    WriteOffRequest,
    EmployeeDeduction,
    EmployeeSalary,
    EmployeeSalaryVersion,
    PayrollLineItem,
    FiscalPeriod,
    FiscalYear,
    LedgerEntity,
    HeldForBranchReceipt,
    InterBranchRecharge,
    InterBranchRechargeLine,
    InterBranchTransfer,
    PayrollDeductionType,
    PayrollLine,
    PayrollRun,
    PayBroughtForward,
    PettyCashFund,
    PettyCashReturn,
    SharedCostRule,
    SharedCostRuleShare,
    StaffBankAccount,
)
from vs_finance.constants import (
    ChargeKind,
    InterBranchTransferKind,
    RechargeBasis,
    SharedCostTreatment,
)
from vs_finance.seed import seed_chart_of_accounts, seed_currencies
from vs_finance.receivables_policy import resolve_receivables_policy, serialize_receivables_policy
from vs_finance.tests_accruals import _AccrualFixture
from vs_finance.tests_bank_account_reach import _bank
from vs_finance.tests_bank_document_rework import _ReworkFixture
from vs_finance.tests_budget_branch import _BudgetFixture
from vs_finance.tests_inter_branch import JAN_15, _InterBranchFixture
from vs_finance.tests_payer_payments import _PayerFixture
from vs_finance.tests_petty_cash_returns import FLOAT, JAN_10, _PettyCashReturnFixture
from vs_rbac.tests.deep_payload import DeepPayloadChecks, Sample
from vs_rbac.tests.helpers import make_branch, make_school

_SURFACE = "vs_finance.serializers."


class BudgetReportDeepPayloadTests(DeepPayloadChecks, _BudgetFixture):
    """Budget records and report aliases hide every registered field when closed."""

    resources = frozenset({"finance.budget", "finance.report"})
    covers = frozenset({
        _SURFACE + "BudgetSerializer",
        _SURFACE + "BudgetLineSerializer",
        "vs_finance.budget_access.BudgetAccessSerializer",
        "vs_finance.report_access.ReportSectionsSerializer",
    })

    def samples(self):
        from vs_finance.budget_access import BUDGET_ALIASES
        from vs_finance.report_access import REPORT_SECTIONS

        return [
            Sample(_SURFACE + "BudgetSerializer", self.ikeja_plan),
            Sample(_SURFACE + "BudgetLineSerializer", self.ikeja_plan.lines.first()),
            Sample("vs_finance.budget_access.BudgetAccessSerializer",
                   {name: True for name in BUDGET_ALIASES}),
            Sample("vs_finance.report_access.ReportSectionsSerializer",
                   {name: True for name in REPORT_SECTIONS}),
        ]


class FinanceDeepPayloadTests(DeepPayloadChecks, TestCase):
    resources = frozenset({
        "finance.bankaccount", "finance.payrollrun", "finance.salary", "finance.staff_bank",
    })
    covers = frozenset({
        _SURFACE + "BankAccountSerializer",
        _SURFACE + "PayrollRunSerializer",
        _SURFACE + "PayrollLineSerializer",
        _SURFACE + "PayrollLinePaymentSerializer",
        _SURFACE + "StaffBankAccountSerializer",
        _SURFACE + "StaffWithoutBankAccountSerializer",
        _SURFACE + "EmployeeSalarySerializer",
        _SURFACE + "EmployeeSalaryNextTermsSerializer",
        _SURFACE + "EmployeeSalaryVersionSerializer",
        _SURFACE + "EmployeeDeductionSerializer",
        _SURFACE + "PayBroughtForwardSerializer",
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
            line_no=1, other_deductions_amount=6_250_00, employer_contributions_amount=30_000_00,
            taxable_pay=250_000_00, tax_basis={"tax_to_date": 20_000_00},
            tax_id="TIN-1", pension_pin="PEN-1",
        )
        PayrollLineItem.objects.create(
            line=line, kind="DEDUCTION", code="NHF", amount=6_250_00,
        )
        salary = EmployeeSalary.objects.create(
            entity=entity, branch=branch, name="Ngozi Okafor",
            gross_amount=250_000_00, paye_amount=20_000_00,
            pension_amount=20_000_00, tax_id="TIN-1", pension_pin="PEN-1",
            annual_rent=1_200_000_00, paye_override=15_000_00, paye_override_reason="Bureau",
        )
        version = EmployeeSalaryVersion.objects.create(
            salary=salary, effective_from=datetime.date(2026, 1, 1), branch=branch,
            gross_amount=250_000_00, paye_amount=20_000_00, pension_amount=20_000_00,
        )
        # A raise not in force yet, so the row carries its next terms too.
        EmployeeSalaryVersion.objects.create(
            salary=salary, effective_from=datetime.date(2099, 1, 1), branch=branch,
            gross_amount=275_000_00, paye_amount=22_000_00, pension_amount=22_000_00,
        )
        deduction = EmployeeDeduction.objects.create(
            salary=salary, amount=5_000_00, total_limit=60_000_00,
            deduction_type=PayrollDeductionType.objects.create(
                entity=entity, code="LOAN", name="Staff loan",
                liability_account=Account.objects.get(entity=entity, code="2400"),
            ),
        )
        person = get_user_model().objects.create_user(
            email=f"ngozi-{code.lower()}@deep.test", password="pw", tenant=tenant, branch=None,
            status="ACTIVE", first_name="Ngozi", last_name="Okafor",
        )
        account = StaffBankAccount.objects.create(
            entity=entity, employee=person, bank_code="058", bank_name="GTBank",
            account_number="0123456789", resolved_account_name="OKAFOR NGOZI",
            verification_state="CONFIRMED", changed_by=person, confirmed_by=person,
        )
        line.pay_state, line.pay_note, line.payout_reference = "FAILED", "Bank said no.", "CXP-1"
        line.save(update_fields=["pay_state", "pay_note", "payout_reference"])
        previous = PayBroughtForward.objects.create(
            salary=salary, tax_year=2026, employer_name="Greenfield Academy",
            gross_amount=900_000_00, taxable_pay=900_000_00, paye_amount=45_000_00,
            pension_amount=72_000_00, nhf_amount=5_000_00,
        )
        return bank, line, salary, version, deduction, previous, account, run

    def samples(self):
        samples = []
        for tenant, (bank, line, salary, version, deduction, previous, account, run) in self.records:
            samples += [
                Sample(_SURFACE + "BankAccountSerializer", bank, tenant=tenant),
                Sample(_SURFACE + "PayrollRunSerializer", run, tenant=tenant),
                Sample(_SURFACE + "PayrollLineSerializer", line, tenant=tenant),
                Sample(_SURFACE + "PayrollLinePaymentSerializer", line, tenant=tenant),
                Sample(_SURFACE + "StaffBankAccountSerializer", account, tenant=tenant),
                Sample(_SURFACE + "StaffWithoutBankAccountSerializer",
                       EmployeeSalary.objects.filter(pk=salary.pk)
                       .with_branch_on(datetime.date(2026, 1, 1)).get(), tenant=tenant),
                Sample(_SURFACE + "EmployeeSalarySerializer", salary, tenant=tenant),
                Sample(_SURFACE + "EmployeeSalaryNextTermsSerializer", {
                    "effective_from": datetime.date(2099, 1, 1), "branch_id": None,
                    "branch_name": None, "gross_amount": 275_000_00,
                    "paye_amount": 22_000_00, "pension_amount": 22_000_00,
                }, tenant=tenant),
                Sample(_SURFACE + "EmployeeSalaryVersionSerializer", version, tenant=tenant),
                Sample(_SURFACE + "EmployeeDeductionSerializer", deduction, tenant=tenant),
                Sample(_SURFACE + "PayBroughtForwardSerializer", previous, tenant=tenant),
            ]
        return samples


_INTERBRANCH_SURFACE = "vs_finance.views_ops.interbranch."


class InterBranchDeepPayloadTests(DeepPayloadChecks, _InterBranchFixture):
    resources = frozenset({"finance.interbranch"})
    covers = frozenset({
        _INTERBRANCH_SURFACE + "InterBranchTransferSerializer",
        _INTERBRANCH_SURFACE + "HeldReceiptSerializer",
        _INTERBRANCH_SURFACE + "RechargeSerializer",
        _INTERBRANCH_SURFACE + "SharedCostRuleSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        customer = cls.customer(cls.books, "IB-DEEP", cls.lekki)
        cls.transfer = InterBranchTransfer.objects.create(
            entity=cls.books, kind=InterBranchTransferKind.CASH, branch=cls.ikeja,
            to_branch=cls.lekki, from_bank_account=cls.ikeja_bank,
            to_bank_account=cls.lekki_bank, amount=100_000_00,
            transfer_date=JAN_15, purpose="Generator fuel", reference="IB-DEEP",
            customer=customer,
        )
        cls.held = HeldForBranchReceipt.objects.create(
            entity=cls.books, branch=cls.ikeja, for_branch=cls.lekki,
            bank_account=cls.ikeja_bank, customer=customer, amount=25_000_00,
            receipt_date=JAN_15, reference="HELD-DEEP", narration="Lekki fees",
        )
        cls.rule = SharedCostRule.objects.create(
            entity=cls.books, name="Group audit", treatment=SharedCostTreatment.RECHARGE,
            basis=RechargeBasis.PERCENTAGES, expense_account=cls.audit_fee,
        )
        SharedCostRuleShare.objects.create(
            rule=cls.rule, branch=cls.lekki, percent_bps=10000,
        )
        cls.recharge = InterBranchRecharge.objects.create(
            entity=cls.books, branch=cls.ikeja, rule=cls.rule,
            expense_account=cls.audit_fee, amount=300_000_00,
            recharge_date=JAN_15, basis=RechargeBasis.PERCENTAGES,
            narration="Annual audit", reference="RECHARGE-DEEP",
        )
        InterBranchRechargeLine.objects.create(
            recharge=cls.recharge, branch=cls.lekki, weight=10000,
            amount=300_000_00, transfer=cls.transfer,
        )

    def samples(self):
        return [
            Sample(_INTERBRANCH_SURFACE + "InterBranchTransferSerializer",
                   self.transfer, tenant=self.tenant),
            Sample(_INTERBRANCH_SURFACE + "HeldReceiptSerializer",
                   self.held, tenant=self.tenant),
            Sample(_INTERBRANCH_SURFACE + "RechargeSerializer",
                   self.recharge, context={"reach": None}, tenant=self.tenant),
            Sample(_INTERBRANCH_SURFACE + "SharedCostRuleSerializer",
                   self.rule, tenant=self.tenant),
        ]


_ACCRUALS = "vs_finance.views_accruals."


class AccrualsDeepPayloadTests(DeepPayloadChecks, _AccrualFixture):
    """The deferred income, provision, deposit and receivables policy surfaces.

    Corona runs two branches and Harbour one; each is rendered by a caller of
    its own tenant who may read none of the four resources.
    """

    resources = frozenset({
        "finance.deferredincome", "finance.provision", "finance.deposit", "finance.settings",
    })
    covers = frozenset({
        _ACCRUALS + "DeferredIncomeSummarySerializer",
        _ACCRUALS + "DeferredIncomeReleaseRowSerializer",
        _ACCRUALS + "DeferredIncomeRunSerializer",
        _ACCRUALS + "DeferredIncomePostedReleaseSerializer",
        _ACCRUALS + "DeferredIncomeReversalSerializer",
        _SURFACE + "DoubtfulDebtProvisionSerializer",
        _SURFACE + "DoubtfulDebtProvisionLineSerializer",
        _SURFACE + "CustomerDepositSerializer",
        _ACCRUALS + "DepositReleaseNoteSerializer",
        _ACCRUALS + "DepositForfeitureSerializer",
        _ACCRUALS + "DepositSkippedSerializer",
        _ACCRUALS + "ReceivablesPolicySerializer",
        "vs_finance.views_settings.BankingSettingsSerializer",
        "vs_finance.views_settings.DocumentSettingsSerializer",
        "vs_finance.views_settings.CalendarSettingsSerializer",
        "vs_finance.views_settings.PayrollSettingsSerializer",
        "vs_finance.views_settings.AccountMappingsSerializer",
        "vs_finance.views_records.RetentionSettingsSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.shapes = [
            (cls.tenant, cls._records(cls.books, cls.eze, cls.ikeja, "EZE")),
            (cls.solo_tenant, cls._records(cls.solo_books, cls.ada, cls.solo_main, "ADA")),
        ]

    @classmethod
    def _records(cls, books, customer, branch, code):
        provision = DoubtfulDebtProvision.objects.create(
            entity=books, as_of=datetime.date(2026, 12, 31), narration="Year end",
            required_total=90_000, movement_total=90_000,
            policy_snapshot=[{"over_days": 90, "rate_bps": 2500}],
        )
        line = DoubtfulDebtProvisionLine.objects.create(
            provision=provision, branch=branch, required=90_000, current=0,
            movement=90_000, bands={"90": 90_000},
        )
        invoice = cls._deposit_bill(books, customer)
        deposit = CustomerDeposit.objects.get(invoice=invoice)
        forfeiture = DepositForfeiture.objects.create(
            entity=books, branch=branch, journal=invoice.journal, amount=50_000,
        )
        note = CreditNote.objects.create(
            entity=books, customer=customer, branch=branch, note_date=datetime.date(2026, 6, 1),
            reason="Deposit returned",
        )
        CreditNoteLine.objects.create(
            note=note, revenue_account=Account.objects.get(entity=books, code="4900"),
            quantity=1, unit_price=50_000, line_no=1,
        )
        note.refresh_from_db()
        return {
            "provision": provision, "line": line, "deposit": deposit,
            "forfeiture": forfeiture, "note": note, "code": code,
            "policy": serialize_receivables_policy(resolve_receivables_policy(books)),
            "banking": serialize_finance_banking_settings(
                resolve_finance_banking_settings(books)),
            "documents": serialize_finance_document_settings(
                resolve_finance_document_settings(books)),
            "calendar": serialize_finance_calendar_settings(
                resolve_finance_calendar_settings(books)),
            "payroll": serialize_finance_payroll_settings(
                resolve_finance_payroll_settings(books)),
            "mappings": {row["key"]: row for row in account_mapping_snapshot(books)},
            "retention": {"statutory_years": 6, "retention_years": 8,
                          "effective_retention_years": 8, "archive_min_age_years": 9},
        }

    @classmethod
    def _deposit_bill(cls, books, customer):
        from vs_finance.receivables import post_invoice

        invoice = Invoice.objects.create(
            entity=books, customer=customer, branch=customer.branch,
            invoice_date=datetime.date(2026, 1, 10), due_date=datetime.date(2026, 1, 25),
        )
        InvoiceLine.objects.create(
            invoice=invoice, line_no=1, quantity=1, unit_price=50_000, kind=ChargeKind.DEPOSIT,
            revenue_account=Account.objects.get(entity=books, code="4100"),
        )
        post_invoice(invoice)
        invoice.refresh_from_db()
        return invoice

    def samples(self):
        samples = []
        for tenant, rec in self.shapes:
            samples += [
                Sample(_ACCRUALS + "DeferredIncomeSummarySerializer", {
                    "pending": 150_000, "released": 37_500,
                    "by_month": [{"month": "2027-01", "amount": 37_500}],
                }, tenant=tenant),
                Sample(_ACCRUALS + "DeferredIncomeReleaseRowSerializer", [{
                    "id": 1, "branch_id": 7, "branch_name": "Ikeja Branch",
                    "date": "2027-01-31", "month": "2027-01", "period_id": 3,
                    "period_name": "2027-01", "period_label": "January 2027",
                    "period_status": "OPEN", "branch_period_status": "OPEN",
                    "amount": 37_500, "journal_id": 9, "journal_number": "JV-0009",
                    "reversed": False, "reversed_at": None, "can_reverse": True,
                    "reverse_blocked_reason": None,
                }], many=True, tenant=tenant),
                Sample(_ACCRUALS + "DeferredIncomeRunSerializer", {
                    "up_to": "2027-01-31",
                    "releases": [{"id": 1, "branch_id": 7, "date": "2027-01-31",
                                  "amount": 37_500, "journal_id": 9}],
                }, tenant=tenant),
                Sample(_ACCRUALS + "DeferredIncomePostedReleaseSerializer", [{
                    "id": 1, "branch_id": 7, "date": "2027-01-31", "amount": 37_500,
                    "journal_id": 9,
                }], many=True, tenant=tenant),
                Sample(_ACCRUALS + "DeferredIncomeReversalSerializer",
                       {"period": 3, "reversed": 2}, tenant=tenant),
                Sample(_SURFACE + "DoubtfulDebtProvisionSerializer", rec["provision"],
                       context={"branch_ids": None}, tenant=tenant),
                Sample(_SURFACE + "DoubtfulDebtProvisionSerializer", rec["provision"],
                       context={"branch_ids": frozenset({rec["line"].branch_id})},
                       tenant=tenant),
                Sample(_SURFACE + "DoubtfulDebtProvisionLineSerializer", rec["line"],
                       tenant=tenant),
                Sample(_SURFACE + "CustomerDepositSerializer", rec["deposit"], tenant=tenant),
                Sample(_ACCRUALS + "DepositReleaseNoteSerializer", [rec["note"]], many=True,
                       tenant=tenant),
                Sample(_ACCRUALS + "DepositForfeitureSerializer", [rec["forfeiture"]],
                       many=True, tenant=tenant),
                Sample(_ACCRUALS + "DepositSkippedSerializer", [{
                    "deposit": rec["deposit"].pk, "customer": rec["code"],
                    "invoice": "INV-0001", "reason": "Its bill is not fully paid.",
                }], many=True, tenant=tenant),
                Sample(_ACCRUALS + "ReceivablesPolicySerializer", rec["policy"],
                       tenant=tenant),
                Sample("vs_finance.views_settings.BankingSettingsSerializer",
                       rec["banking"], tenant=tenant),
                Sample("vs_finance.views_settings.DocumentSettingsSerializer",
                       rec["documents"], tenant=tenant),
                Sample("vs_finance.views_settings.CalendarSettingsSerializer",
                       rec["calendar"], tenant=tenant),
                Sample("vs_finance.views_settings.PayrollSettingsSerializer",
                       rec["payroll"], tenant=tenant),
                Sample("vs_finance.views_settings.AccountMappingsSerializer",
                       rec["mappings"], tenant=tenant),
                Sample("vs_finance.views_records.RetentionSettingsSerializer",
                       rec["retention"], tenant=tenant),
            ]
        return samples


_BANKING = "vs_finance.views_ops.banking."


class BankDocumentsDeepPayloadTests(DeepPayloadChecks, _ReworkFixture):
    """Bank transactions, transfers between own accounts and the branch split of a shared account.

    Corona runs Ikeja, Lekki and Yaba; Single Site runs one branch. Each tenant's
    documents are rendered by a caller of its own tenant who may read none of the
    three resources.
    """

    resources = frozenset({
        "finance.banktransaction", "finance.banktransfer", "finance.bankaccount",
    })
    covers = frozenset({
        _BANKING + "BankTransactionSerializer",
        _BANKING + "BankTransferSerializer",
        _BANKING + "_BankAccountBranchSplitSerializer",
        _BANKING + "_BankBranchAllocationSerializer",
        _BANKING + "BankSplitPreviewSerializer",
        _BANKING + "BankSplitResultSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        solo_ledger = Account.objects.create(
            entity=cls.solo_books, code="1152", name="Savings",
            account_type=cls.acc("1100").account_type, is_postable=True,
        )
        solo_savings = BankAccount.objects.create(
            entity=cls.solo_books, name="Savings", branch=cls.solo_main, gl_account=solo_ledger,
        )
        capital = Account.objects.get(entity=cls.books, code="3100")
        solo_capital = Account.objects.get(entity=cls.solo_books, code="3100")
        cls.documents = []
        for tenant, books, branch, bank, other, counter in (
            (cls.tenant, cls.books, cls.ikeja, cls.ikeja_bank, cls.ikeja_zenith, capital),
            (cls.solo_tenant, cls.solo_books, cls.solo_main, cls.solo_bank, solo_savings,
             solo_capital),
        ):
            transaction = BankTransaction.objects.create(
                entity=books, branch=branch, bank_account=bank, direction="IN", amount=5_000_000,
                counter_account=counter, transaction_date=datetime.date(2026, 1, 15),
                narration="Owner capital", reference="CAP-1",
            )
            transfer = BankTransfer.objects.create(
                entity=books, branch=branch, from_account=other, to_account=bank,
                amount=2_000_000, transfer_date=datetime.date(2026, 1, 15),
                narration="Fund GTBank", reference="TR-1",
            )
            cls.documents.append((tenant, transaction, transfer))

    def samples(self):
        allocation = {
            "branch": 7, "opening_balance": 5_000_000, "bank_account_name": "Ikeja GTBank",
            "ledger_account_code": "1161", "ledger_account_name": "Ikeja GTBank ledger",
            "is_primary": True, "is_primary_collection": False,
        }
        samples = []
        for tenant, transaction, transfer in self.documents:
            samples += [
                Sample(_BANKING + "BankTransactionSerializer", transaction, tenant=tenant),
                Sample(_BANKING + "BankTransactionSerializer", [transaction], many=True,
                       tenant=tenant),
                Sample(_BANKING + "BankTransferSerializer", transfer, tenant=tenant),
                Sample(_BANKING + "BankTransferSerializer", [transfer], many=True, tenant=tenant),
                Sample(_BANKING + "_BankBranchAllocationSerializer", allocation, tenant=tenant),
                Sample(_BANKING + "_BankAccountBranchSplitSerializer", {
                    "split_date": "2026-01-31", "agreement_reference": "AGR-2026-01",
                    "difference_treatment": "DEBT", "allocations": [allocation, allocation],
                }, tenant=tenant),
                Sample(_BANKING + "BankSplitPreviewSerializer", {
                    "bank_account_id": 3, "legacy_balance": 4_000, "unbranched_balance": 0,
                    "split_date": "2026-01-31",
                    "branches": [{"branch_id": 7, "branch_name": "Ikeja Branch",
                                  "book_balance": 5_000}],
                }, tenant=tenant),
                Sample(_BANKING + "BankSplitResultSerializer", {
                    "legacy_bank_account_id": 3, "legacy_balance": 4_000,
                    "bank_accounts": [{"id": 8, "name": "Ikeja GTBank", "branch_id": 7,
                                       "gl_account_id": 5, "gl_account_code": "1161",
                                       "opening_balance": 5_000, "is_primary": True,
                                       "is_primary_collection": False}],
                    "journal_ids": [11, 12], "difference_treatment": "DEBT",
                    "inter_branch_transfers": [{"id": 4, "document_number": "IB-1",
                                                "from_branch_id": 7, "to_branch_id": 8,
                                                "amount": 1_000}],
                }, tenant=tenant),
            ]
        return samples


_PAYERS = "vs_finance.views_payers."


class PayerDeepPayloadTests(DeepPayloadChecks, _PayerFixture):
    """Payer links and payments from a payer, as they are served, in both school shapes."""

    resources = frozenset({"finance.payer_link", "finance.payer_payment"})
    covers = frozenset({
        _PAYERS + "PayerLinkSerializer",
        _PAYERS + "PayerPaymentSerializer",
        _PAYERS + "PayerPaymentShareSerializer",
    })

    @staticmethod
    def _person(code, name):
        return {"id": 1, "code": code, "name": name}

    def _payment(self, branch_id, branch_name):
        receipt = {
            "id": 1, "customer": self._person("ADA", "Ada Okafor"), "branch_id": branch_id,
            "branch_name": branch_name, "kind": "RECEIPT", "amount": 180_000_00, "credit": 0,
            "document": {"id": 5, "document_number": "RC-1", "status": "POSTED"},
            "bills": [{"invoice_id": 9, "document_number": "IV-1", "applied": 180_000_00}],
        }
        held = {
            "id": 2, "customer": self._person("CHIDI", "Chidi Okafor"), "branch_id": branch_id,
            "branch_name": branch_name, "kind": "HELD", "amount": 120_000_00, "credit": 0,
            "document": {"id": 6, "document_number": "HR-1", "status": "POSTED"},
            "forwarded_by": {"id": 3, "document_number": "IB-1", "status": "POSTED"},
        }
        return {
            "id": 1, "document_number": "PP-1", "status": "POSTED",
            "payer": self._person("OKAFOR", "Mr Okafor"), "branch_id": branch_id,
            "branch_name": branch_name,
            "bank_account": {"id": 2, "name": "Ikeja GTBank"}, "amount": 300_000_00,
            "payment_date": "2026-01-15", "method": "BANK_TRANSFER", "split": "OLDEST_FIRST",
            "split_label": "Oldest bill first", "reference": "TRF-1", "narration": "Fees",
            "shares": [receipt, held],
        }

    def _plan(self, branch_id, branch_name):
        row = {
            "customer": self._person("ADA", "Ada Okafor"), "branch_id": branch_id,
            "branch_name": branch_name, "kind": "RECEIPT", "amount": 180_000_00, "credit": 0,
            "outstanding": 180_000_00,
            "bills": [{"id": 9, "document_number": "IV-1", "kind": "INVOICE",
                       "balance": 180_000_00, "applied": 180_000_00}],
        }
        return {
            "payer": self._person("OKAFOR", "Mr Okafor"),
            "bank_account": {"id": 2, "name": "Ikeja GTBank"}, "branch_id": branch_id,
            "branch_name": branch_name, "amount": 180_000_00, "payment_date": "2026-01-15",
            "split": "OLDEST_FIRST", "split_label": "Oldest bill first", "shares": [row],
        }

    def samples(self):
        samples = []
        for tenant, branch_id, branch_name in (
            (self.tenant, self.ikeja.pk, "Ikeja Branch"),
            (self.solo_tenant, self.solo_main.pk, "Main Branch"),
        ):
            payment = self._payment(branch_id, branch_name)
            samples += [
                Sample(_PAYERS + "PayerLinkSerializer", {
                    "id": 1, "payer": self._person("OKAFOR", "Mr Okafor"),
                    "customer": self._person("ADA", "Ada Okafor"), "is_active": True,
                    "source_type": "IMPORT", "source_id": "row-4",
                }, tenant=tenant),
                Sample(_PAYERS + "PayerPaymentSerializer", payment, tenant=tenant),
                Sample(_PAYERS + "PayerPaymentSerializer", [payment], many=True, tenant=tenant),
                Sample(_PAYERS + "PayerPaymentSerializer", self._plan(branch_id, branch_name),
                       tenant=tenant),
                Sample(_PAYERS + "PayerPaymentShareSerializer", payment["shares"], many=True,
                       tenant=tenant),
            ]
        return samples


class PettyCashReturnDeepPayloadTests(DeepPayloadChecks, _PettyCashReturnFixture):
    """A petty cash return in a school with several branches and in one with a single branch."""

    resources = frozenset({"finance.pettycash_return"})
    covers = frozenset({"vs_finance.serializers.PettyCashReturnSerializer"})

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        solo_bank = _bank(cls.solo_books, "Main Operations", None, "50")
        solo_fund = PettyCashFund.objects.create(
            entity=cls.solo_books, name="Front desk", float_amount=FLOAT,
            gl_account=Account.objects.get(entity=cls.solo_books, code="1110"),
        )
        cls.returns = []
        for tenant, books, fund, bank in (
            (cls.tenant, cls.books, cls.fund, cls.ikeja_bank),
            (cls.solo_tenant, cls.solo_books, solo_fund, solo_bank),
        ):
            cls.returns.append((tenant, PettyCashReturn.objects.create(
                entity=books, branch=fund.branch, fund=fund, kind="REDUCE", bank_account=bank,
                return_date=JAN_10, counted_amount=FLOAT, book_balance=FLOAT + 500,
                amount=50_000_00, previous_float_amount=FLOAT, new_float_amount=50_000_00,
                counted_by=cls.custodian if tenant == cls.tenant else None,
                created_by=cls.custodian if tenant == cls.tenant else None,
                difference_reason="Two notes missing", narration="Cut the float",
                reference="PCR-1",
            )))

    def samples(self):
        samples = []
        for tenant, ret in self.returns:
            samples += [
                Sample("vs_finance.serializers.PettyCashReturnSerializer", ret, tenant=tenant),
                Sample("vs_finance.serializers.PettyCashReturnSerializer", [ret], many=True,
                       tenant=tenant),
            ]
        return samples


_DASHBOARD = "vs_finance.dashboard_serializers."
_CLOSE = "vs_finance.close_access."
_SEAL = "vs_finance.seal_serializers."

#: Every close check a payload can carry: finance's own, the year-end one and two another module adds.
_CHECK_NAMES = (
    "trial_balance_balanced", "no_draft_journals", "ar_reconciled", "depreciation_posted",
    "earlier_periods_closed", "deferred_income_released", "inter_branch_balanced",
    "sealed_figures_unchanged", "depreciation_posted_for_year", "ap_reconciled",
    "grir_explained",
)


def _check_item(name):
    return {
        "name": name, "label": name.replace("_", " ").capitalize(), "passed": False,
        "blocking": True, "done_by_close": False, "detail": f"{name} found a difference",
    }


class DashboardCloseSealDeepPayloadTests(DeepPayloadChecks, TestCase):
    """The dashboards, the close, the sealed figures and the fiscal year, in both school shapes.

    Rendered as a caller who may read none of ``finance.dashboard``,
    ``finance.period``, ``finance.seal`` or ``finance.fiscalyear``. The closing
    journal a year close returns carries its own ``status``, the journal's, which
    shares a name with the year's and belongs to another record.
    """

    resources = frozenset({
        "finance.dashboard", "finance.period", "finance.seal", "finance.fiscalyear",
    })
    covers = frozenset({
        _DASHBOARD + "FinanceKpisSerializer",
        _DASHBOARD + "FinanceDashboardSerializer",
        _DASHBOARD + "ReceivablesDashboardSerializer",
        _DASHBOARD + "SpendDashboardSerializer",
        "vs_finance.serializers.FiscalPeriodSerializer",
        "vs_finance.serializers.FiscalYearSerializer",
        _CLOSE + "BranchPeriodStateSerializer",
        _CLOSE + "CloseChecksSerializer",
        _CLOSE + "CloseChecklistSerializer",
        _CLOSE + "PeriodChecklistPreviewSerializer",
        _CLOSE + "PeriodCloseResultSerializer",
        _CLOSE + "FiscalYearCloseResultSerializer",
        _SEAL + "SealCheckSerializer",
        _SEAL + "SealVerificationSerializer",
    })
    allowlist = {
        (_CLOSE + "FiscalYearCloseResultSerializer", "closing_journals[].status"):
            "The closing journal's own status, not the year's.",
        (_CLOSE + "FiscalYearCloseResultSerializer", "closing_journal.status"):
            "The closing journal's own status, not the year's.",
    }

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import BranchFiscalPeriod
        from vs_finance.tests_branch_scope import _FinanceBranchFixture

        school = make_school(slug="deep-dash", name="Deep Dashboard School", status="ACTIVE")
        cls.tenant = school.tenant
        cls.ikeja = make_branch(school, name="Ikeja Branch")
        cls.lekki = make_branch(school, name="Lekki Branch", is_main=False)
        cls.books = _FinanceBranchFixture.build_books("DEEPDASH", cls.tenant)
        solo = make_school(slug="deep-dash-solo", name="Deep Single Site", status="ACTIVE")
        cls.solo_tenant = solo.tenant
        cls.solo_main = make_branch(solo, name="Main Branch")
        cls.solo_books = _FinanceBranchFixture.build_books("DEEPSOLO", cls.solo_tenant)
        cls.shapes = []
        for tenant, books, branches in (
            (cls.tenant, cls.books, [cls.ikeja, cls.lekki]),
            (cls.solo_tenant, cls.solo_books, [cls.solo_main]),
        ):
            period = FiscalPeriod.objects.get(entity=books, period_no=1)
            state = BranchFiscalPeriod.objects.create(
                period=period, branch=branches[0], status="CLOSED",
                closed_at=datetime.datetime(2026, 2, 3, tzinfo=datetime.timezone.utc),
            )
            cls.shapes.append((tenant, books, period, state, branches))

    def _journal(self):
        return {"id": 9, "document_number": "JV-9", "status": "POSTED"}

    def samples(self):
        from vs_finance.dashboard import EVERY_BLOCK, finance_dashboard
        from vs_finance.dashboard_receivables import receivables_view
        from vs_finance.dashboard_spend import spend_view

        items = [_check_item(name) for name in _CHECK_NAMES]
        samples = []
        for tenant, books, period, state, branches in self.shapes:
            overview = finance_dashboard(books, reader=EVERY_BLOCK)
            states = {period.pk: [{
                "branch": b.pk, "branch_name": b.name, "status": "CLOSED", "closed_at": None,
            } for b in branches]}
            year = period.fiscal_year
            seal_row = {
                "seal_id": 4, "label": "January 2026", "kind": "PERIOD", "fiscal_year": 2026,
                "period_id": period.pk, "sealed_at": datetime.datetime(2026, 2, 3),
                "seal_checksum": "abc", "line_count": 3, "line_count_now": 4, "ok": False,
                "seal_intact": True, "lines_match": False, "summary": "January 2026: differs.",
                "differences": [{
                    "branch_id": branches[0].pk, "branch_name": branches[0].name,
                    "account_id": 1, "account_code": "1100", "account_name": "Cash",
                    "sealed": {"balance": {"kobo": 1, "naira": "0.01"}},
                    "now": {"balance": {"kobo": 2, "naira": "0.02"}},
                }],
            }
            samples += [
                Sample(_DASHBOARD + "FinanceDashboardSerializer", overview, tenant=tenant),
                Sample(_DASHBOARD + "FinanceKpisSerializer", overview["kpis"], tenant=tenant),
                Sample(_DASHBOARD + "ReceivablesDashboardSerializer",
                       receivables_view(books, reader=EVERY_BLOCK), tenant=tenant),
                Sample(_DASHBOARD + "SpendDashboardSerializer",
                       spend_view(books, reader=EVERY_BLOCK), tenant=tenant),
                Sample("vs_finance.serializers.FiscalPeriodSerializer", period, tenant=tenant,
                       context={"branch_states": states}),
                Sample("vs_finance.serializers.FiscalYearSerializer", year, tenant=tenant,
                       context={"branch_states": {year.pk: states[period.pk]}}),
                Sample(_CLOSE + "BranchPeriodStateSerializer", state, tenant=tenant),
                Sample(_CLOSE + "CloseChecksSerializer",
                       {item["name"]: item for item in items} | {"extra_check": _check_item("extra_check")},
                       tenant=tenant),
                Sample(_CLOSE + "CloseChecklistSerializer",
                       {"passed": False, "items": items}, tenant=tenant),
                Sample(_CLOSE + "PeriodChecklistPreviewSerializer", {
                    "period": period, "passed": False, "done": 0, "total": len(items),
                    "items": items, "branch": branches[0].pk,
                }, tenant=tenant),
                Sample(_CLOSE + "PeriodCloseResultSerializer", {
                    "period": period, "branch_period": state,
                    "checklist": {"passed": False, "items": items},
                }, tenant=tenant),
                Sample(_CLOSE + "FiscalYearCloseResultSerializer", {
                    "fiscal_year": year, "closing_journals": [self._journal()],
                    "closing_journal": self._journal(), "reversals": [],
                    "net_income": {"kobo": 5, "naira": "0.05"}, "branch": branches[0].pk,
                }, tenant=tenant),
                Sample(_SEAL + "SealCheckSerializer", seal_row, tenant=tenant),
                Sample(_SEAL + "SealVerificationSerializer", {
                    "ok": False, "checked": 1, "mismatches": 1, "chain_breaks": [7],
                    "checks": [seal_row],
                }, tenant=tenant),
            ]
        return samples


class CustomerAndBankAccountDeepPayloadTests(DeepPayloadChecks, _ReworkFixture):
    """The customer master and the bank account master, with every block their screens carry.

    Corona runs Ikeja, Lekki and Yaba; Single Site runs one branch. Each tenant's
    customer and account are rendered by a caller of its own tenant who may read
    none of the fields of ``finance.customer`` or ``finance.bankaccount``.
    """

    resources = frozenset({"finance.customer", "finance.bankaccount"})
    covers = frozenset({
        _SURFACE + "CustomerSerializer",
        _SURFACE + "CustomerListSerializer",
        _SURFACE + "CustomerAccountSerializer",
        _SURFACE + "CustomerStandingSerializer",
        _SURFACE + "CustomerSummaryTotalsSerializer",
        _SURFACE + "FinanceDocumentDeliverySerializer",
        _SURFACE + "DocumentEmailPreviewSerializer",
        _SURFACE + "BankAccountDetailSerializer",
        _SURFACE + "BankAccountMetricsSerializer",
        _SURFACE + "BankAccountActivitySerializer",
        _SURFACE + "BankReconciliationSerializer",
        _SURFACE + "BankStatementSerializer",
        _SURFACE + "BankStatementDetailSerializer",
        _SURFACE + "BankStatementLineSerializer",
        _SURFACE + "BankBookLinesSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import (
            BankReconciliation, BankStatement, BankStatementLine, Customer,
            FinanceDocumentDelivery,
        )

        super().setUpTestData()
        cls.records = []
        for tenant, books, branch, bank in (
            (cls.tenant, cls.books, cls.ikeja, cls.ikeja_bank),
            (cls.solo_tenant, cls.solo_books, cls.solo_main, cls.solo_bank),
        ):
            customer = Customer.objects.create(
                entity=books, code="DP-1", name="Parent DP-1", branch=branch,
                billing_email="dp@family.test", billing_phone="08030000009",
                billing_address="3 Deep Road", source_type="manual", source_id="DP-SRC",
                receivable_account=Account.objects.get(entity=books, code="1200"),
                opening_balance=1_000,
            )
            delivery = FinanceDocumentDelivery.objects.create(
                entity=books, customer=customer, document_type="STATEMENT", source="MANUAL",
                status="SENT", recipients=["dp@family.test"], bcc=["finance@deep.test"],
            )
            reconciliation = BankReconciliation.objects.create(
                bank_account=bank, as_of_date=datetime.date(2026, 1, 31),
                book_balance=5_000, statement_balance=4_000, difference=1_000, matched_count=2,
            )
            statement = BankStatement.objects.create(
                bank_account=bank, statement_date=datetime.date(2026, 1, 31),
                period_label="January 2026", opening_balance=1_000,
                closing_balance=2_000,
            )
            line = BankStatementLine.objects.create(
                bank_account=bank, statement=statement,
                txn_date=datetime.date(2026, 1, 20), description="Deposit", amount=1_000,
            )
            cls.records.append((tenant, customer, delivery, bank, reconciliation,
                                statement, line))

    def samples(self):
        samples = []
        for tenant, customer, delivery, bank, reconciliation, statement, line in self.records:
            money = {"kobo": 1_000, "naira": "10.00"}
            standing = {
                "current_balance": money, "lifetime_paid": money, "open_invoice_count": 1,
                "account_status": "ACTIVE",
            }
            bank.detail_metrics = {
                "book_balance": 5_000, "statement_balance": 4_000, "unreconciled_diff": 1_000,
                "unreconciled_count": 2,
            }
            bank.detail_transactions = [{
                "id": 1, "date": datetime.date(2026, 1, 15), "description": "Owner capital",
                "reference": "CAP-1", "debit": 5_000, "credit": 0, "running_balance": 5_000,
                "matched": False,
            }]
            bank.detail_statements = [statement]
            bank.detail_reconciliations = [reconciliation]
            samples += [
                Sample(_SURFACE + "CustomerSerializer", customer, tenant=tenant),
                Sample(_SURFACE + "CustomerListSerializer", [customer], many=True, tenant=tenant,
                       context={
                           "standing": {customer.pk: {"outstanding": 1_000, "credit": 0}},
                           "pays_for": {customer.pk: 2},
                       }),
                Sample(_SURFACE + "CustomerStandingSerializer", standing, tenant=tenant),
                Sample(_SURFACE + "CustomerAccountSerializer", {
                    "customer": customer, "summary": standing,
                    "open_invoices": [{"document_number": "INV-1", "total": money,
                                       "balance": money, "status": "ISSUED"}],
                    "open_debit_notes": [],
                    "transactions": [{"date": "2026-01-10", "type": "INVOICE",
                                      "reference": "INV-1", "amount": money,
                                      "status": "ISSUED"}],
                    "statement": [{"date": "2026-01-10", "description": "Invoice INV-1",
                                   "debit": money, "credit": {"kobo": 0, "naira": "0.00"},
                                   "balance": money}],
                }, tenant=tenant),
                Sample(_SURFACE + "CustomerSummaryTotalsSerializer", {
                    "total": 3, "receivable": money, "on_credit": 1, "overdue": 1,
                    "status_counts": {"ACTIVE": 1, "CREDIT": 1, "OVERDUE": 1, "INACTIVE": 0},
                }, tenant=tenant),
                Sample(_SURFACE + "FinanceDocumentDeliverySerializer", delivery, tenant=tenant),
                Sample(_SURFACE + "DocumentEmailPreviewSerializer", {
                    "recipients": ["dp@family.test"], "bcc": ["finance@deep.test"],
                    "subject": "Statement of account", "can_send": True,
                    "blocked_reason": "", "deliveries": [delivery],
                }, tenant=tenant),
                Sample(_SURFACE + "BankAccountDetailSerializer", bank, tenant=tenant),
                Sample(_SURFACE + "BankAccountMetricsSerializer", bank.detail_metrics,
                       tenant=tenant),
                Sample(_SURFACE + "BankAccountActivitySerializer", bank.detail_transactions[0],
                       tenant=tenant),
                Sample(_SURFACE + "BankReconciliationSerializer", reconciliation,
                       tenant=tenant),
                Sample(_SURFACE + "BankStatementSerializer", statement, tenant=tenant),
                Sample(_SURFACE + "BankStatementDetailSerializer", statement, tenant=tenant),
                Sample(_SURFACE + "BankStatementLineSerializer", line, tenant=tenant),
                Sample(_SURFACE + "BankBookLinesSerializer", {
                    "book_lines": [{"id": 1, "date": "2026-01-15",
                                    "description": "Owner capital", "reference": "CAP-1",
                                    "amount": 1_000}]}, tenant=tenant),
            ]
        return samples


class JournalDeepPayloadTests(DeepPayloadChecks, _ReworkFixture):
    """Journal list, detail, lines and summary honor one resource's switches."""

    resources = frozenset({"finance.journal"})
    covers = frozenset({
        _SURFACE + "JournalEntryListSerializer",
        _SURFACE + "JournalEntryDetailSerializer",
        _SURFACE + "JournalLineSerializer",
        _SURFACE + "JournalSummarySerializer",
    })

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import JournalEntry, JournalLine

        super().setUpTestData()
        cls.records = []
        for tenant, books, branch in (
            (cls.tenant, cls.books, cls.ikeja),
            (cls.solo_tenant, cls.solo_books, cls.solo_main),
        ):
            entry = JournalEntry.objects.create(
                entity=books, branch=branch, date=datetime.date(2026, 1, 15),
                narration="Opening capital", reference="CAP-1",
            )
            line = JournalLine.objects.create(
                entry=entry, account=Account.objects.get(entity=books, code="3100"),
                line_no=1, debit=1_000,
            )
            cls.records.append((tenant, entry, line))

    def samples(self):
        samples = []
        for tenant, entry, line in self.records:
            samples += [
                Sample(_SURFACE + "JournalEntryListSerializer", [entry], many=True,
                       tenant=tenant),
                Sample(_SURFACE + "JournalEntryDetailSerializer", entry, tenant=tenant),
                Sample(_SURFACE + "JournalLineSerializer", line, tenant=tenant),
                Sample(_SURFACE + "JournalSummarySerializer", {
                    "total": 1, "by_status": {"POSTED": 1}, "sent_back": 0,
                    "posted_total": {"kobo": 1_000, "naira": "10.00"},
                    "reversed_total": {"kobo": 0, "naira": "0.00"},
                }, tenant=tenant),
            ]
        return samples


class AccountAndCostCenterDeepPayloadTests(DeepPayloadChecks, _ReworkFixture):
    """Chart rows, detail blocks and cost centres hide their registered fields."""

    resources = frozenset({"finance.account", "finance.costcenter"})
    covers = frozenset({
        _SURFACE + "AccountSerializer",
        _SURFACE + "AccountSummarySerializer",
        _SURFACE + "AccountActivityRowSerializer",
        _SURFACE + "AccountActivityTotalsSerializer",
        _SURFACE + "AccountDetailEnvelopeSerializer",
        _SURFACE + "CostCenterSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        from vs_finance.account_guide import managed_accounts
        from vs_finance.models import CostCenter

        super().setUpTestData()
        cls.records = []
        for tenant, books in ((cls.tenant, cls.books),
                              (cls.solo_tenant, cls.solo_books)):
            account = Account.objects.get(entity=books, code="3100")
            receivable = Account.objects.get(entity=books, code="1200")
            center = CostCenter.objects.create(entity=books, code="ADMIN", name="Admin")
            cls.records.append((tenant, account, receivable, managed_accounts(books), center))

    def samples(self):
        samples = []
        for tenant, account, receivable, managed, center in self.records:
            amount = {"kobo": 1_000, "naira": "10.00"}
            summary = {
                "current_balance": amount, "opening_balance": amount,
                "line_count": 1, "journal_count": 1,
                "fiscal_year_start": "2026-01-01", "as_of": "2026-01-31",
            }
            activity = {
                "date": "2026-01-15", "journal_no": "JN-1", "source": "MANUAL",
                "status": "POSTED", "description": "Capital", "cost_center": "ADMIN",
                "dimensions": {}, "debit": amount, "credit": amount,
                "running_balance": amount,
            }
            samples += [
                Sample(_SURFACE + "AccountSerializer", account, tenant=tenant),
                Sample(_SURFACE + "AccountSerializer", [receivable], many=True,
                       tenant=tenant, context={"managed_accounts": managed}),
                Sample(_SURFACE + "AccountSummarySerializer", summary, tenant=tenant),
                Sample(_SURFACE + "AccountActivityRowSerializer", activity, tenant=tenant),
                Sample(_SURFACE + "AccountActivityTotalsSerializer", {
                    "debit": amount, "credit": amount, "net_movement": amount,
                }, tenant=tenant),
                Sample(_SURFACE + "AccountDetailEnvelopeSerializer", {
                    "account": account, "type_label": "Equity", "summary": summary,
                    "activity": [activity],
                }, tenant=tenant),
                Sample(_SURFACE + "CostCenterSerializer", center, tenant=tenant),
            ]
        return samples


class FinanceSetupDeepPayloadTests(DeepPayloadChecks, _ReworkFixture):
    """Finance setup rows hide their configurable values in both tenant shapes."""

    resources = frozenset({
        "finance.currency", "finance.fxrate", "finance.taxcode", "finance.dimension",
    })
    covers = frozenset({
        _SURFACE + "CurrencySerializer", _SURFACE + "FxRateSerializer",
        _SURFACE + "TaxCodeSerializer", _SURFACE + "DimensionSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        from decimal import Decimal
        from vs_finance.models import Currency, Dimension, FxRate, TaxCode

        super().setUpTestData()
        base, _ = Currency.objects.get_or_create(
            code="NGN", defaults={"name": "Naira", "symbol": "₦"})
        quote, _ = Currency.objects.get_or_create(
            code="USD", defaults={"name": "US Dollar", "symbol": "$"})
        cls.currency = quote
        cls.fx = FxRate.objects.create(base=base, quote=quote,
                                       rate=Decimal("0.0007"), as_of=datetime.date(2026, 1, 15),
                                       source="CBN")
        cls.records = []
        for tenant, books in ((cls.tenant, cls.books),
                              (cls.solo_tenant, cls.solo_books)):
            tax = TaxCode.objects.create(entity=books, code="VAT", name="VAT", rate_bps=750)
            dimension = Dimension.objects.create(entity=books, code="FUND", name="Fund",
                                                 allowed_values=["GRANT-A"])
            cls.records.append((tenant, tax, dimension))

    def samples(self):
        samples = []
        for tenant, tax, dimension in self.records:
            samples.extend((
                Sample(_SURFACE + "CurrencySerializer", self.currency, tenant=tenant),
                Sample(_SURFACE + "FxRateSerializer", self.fx, tenant=tenant),
                Sample(_SURFACE + "TaxCodeSerializer", tax, tenant=tenant),
                Sample(_SURFACE + "DimensionSerializer", dimension, tenant=tenant),
            ))
        return samples


class InvoiceDeepPayloadTests(DeepPayloadChecks, _ReworkFixture):
    """Invoice rows and nested drawer blocks hide every registered field."""

    resources = frozenset({"finance.invoice"})
    covers = frozenset({
        _SURFACE + "InvoiceSerializer",
        _SURFACE + "InvoiceDetailSummarySerializer",
        _SURFACE + "InvoiceDetailLineSerializer",
        _SURFACE + "InvoiceDetailEnvelopeSerializer",
        _SURFACE + "InvoiceSummarySerializer",
    })

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import Customer

        super().setUpTestData()
        cls.records = []
        for tenant, books, branch in (
            (cls.tenant, cls.books, cls.ikeja),
            (cls.solo_tenant, cls.solo_books, cls.solo_main),
        ):
            customer = Customer.objects.create(
                entity=books, branch=branch, code="INV-DEEP", name="Ada Family",
                receivable_account=Account.objects.get(entity=books, code="1200"),
            )
            invoice = Invoice.objects.create(
                entity=books, branch=branch, customer=customer,
                invoice_date=datetime.date(2026, 1, 15),
                due_date=datetime.date(2026, 2, 15), subtotal=1_000,
                tax_total=0, total=1_000, reference="TERM-1", narration="Tuition",
            )
            cls.records.append((tenant, invoice))

    def samples(self):
        samples = []
        for tenant, invoice in self.records:
            amount = {"kobo": 1_000, "naira": "10.00"}
            detail_summary = {
                "subtotal": amount, "tax": amount, "total": amount,
                "paid": amount, "credited": amount, "settled": amount,
                "balance": amount, "due_date": "2026-02-15",
            }
            line = {
                "description": "Tuition", "account_code": "4100",
                "account_name": "Income", "quantity": "1", "unit_price": amount,
                "tax_code": None, "tax_amount": amount, "line_total": amount,
                "kind": "CHARGE", "service_start": None, "service_end": None,
            }
            samples += [
                Sample(_SURFACE + "InvoiceSerializer", invoice, tenant=tenant),
                Sample(_SURFACE + "InvoiceDetailSummarySerializer", detail_summary,
                       tenant=tenant),
                Sample(_SURFACE + "InvoiceDetailLineSerializer", line, tenant=tenant),
                Sample(_SURFACE + "InvoiceDetailEnvelopeSerializer", {
                    "invoice": invoice, "summary": detail_summary, "lines": [line],
                    "deferred_income": {}, "payments": [], "settlements": [],
                    "gl_postings": [], "gl_journals": [], "reminders": [],
                    "activity": [],
                }, tenant=tenant),
                Sample(_SURFACE + "InvoiceSummarySerializer", {
                    "kpis": {"total_invoiced": amount},
                    "by_status": {"paid": 1}, "totals": {"total": amount},
                    "monthly": [{"label": "January", "invoiced": amount}],
                }, tenant=tenant),
            ]
        return samples


class RefundDeepPayloadTests(DeepPayloadChecks, _ReworkFixture):
    """Refund money and details stay hidden across tenant and branch shapes."""

    resources = frozenset({"finance.refund"})
    covers = frozenset({_SURFACE + "RefundSerializer"})

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import Customer, Refund

        super().setUpTestData()
        cls.records = []
        for tenant, books, branch, bank in (
            (cls.tenant, cls.books, cls.ikeja, cls.ikeja_bank),
            (cls.solo_tenant, cls.solo_books, cls.solo_main, cls.solo_bank),
        ):
            customer = Customer.objects.create(
                entity=books, branch=branch, code="REF-DEEP", name="Ada Family",
                receivable_account=Account.objects.get(entity=books, code="1200"),
            )
            refund = Refund.objects.create(
                entity=books, branch=branch, customer=customer, bank_account=bank,
                refund_date=datetime.date(2026, 1, 15), amount=1_000,
                reference="RET-1", narration="Duplicate payment",
            )
            cls.records.append((tenant, refund))

    def samples(self):
        return [Sample(_SURFACE + "RefundSerializer", refund, tenant=tenant)
                for tenant, refund in self.records]


class CreditNoteDeepPayloadTests(DeepPayloadChecks, _ReworkFixture):
    """Credit and debit note lines follow the note's nested switches."""

    resources = frozenset({"finance.creditnote"})
    covers = frozenset({
        _SURFACE + "CreditNoteSerializer",
        _SURFACE + "CreditNoteLineSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import Customer

        super().setUpTestData()
        cls.records = []
        for tenant, books, branch in (
            (cls.tenant, cls.books, cls.ikeja),
            (cls.solo_tenant, cls.solo_books, cls.solo_main),
        ):
            customer = Customer.objects.create(
                entity=books, branch=branch, code="NOTE-DEEP", name="Ada Family",
                receivable_account=Account.objects.get(entity=books, code="1200"),
            )
            note = CreditNote.objects.create(
                entity=books, branch=branch, customer=customer,
                note_date=datetime.date(2026, 1, 15), reason="Returned goods",
                reference="RET-1", total=1_000,
            )
            line = CreditNoteLine.objects.create(
                note=note, line_no=1,
                revenue_account=Account.objects.get(entity=books, code="4000"),
                quantity=1, unit_price=1_000, net_amount=1_000,
            )
            cls.records.append((tenant, note, line))

    def samples(self):
        samples = []
        for tenant, note, line in self.records:
            samples += [
                Sample(_SURFACE + "CreditNoteSerializer", note, tenant=tenant),
                Sample(_SURFACE + "CreditNoteLineSerializer", line, tenant=tenant),
            ]
        return samples


class ReceivableAdjustmentDeepPayloadTests(DeepPayloadChecks, _ReworkFixture):
    """Adjustment documents hide their own fields in either branch shape."""

    resources = frozenset({
        "finance.concession", "finance.writeoff", "finance.credittransfer",
        "finance.paymentplan",
    })
    covers = frozenset({
        _SURFACE + "ConcessionSerializer",
        _SURFACE + "WriteOffRequestSerializer",
        _SURFACE + "CustomerCreditTransferSerializer",
        _SURFACE + "PaymentPlanSerializer",
        _SURFACE + "PaymentPlanInstallmentSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import Customer

        super().setUpTestData()
        cls.records = []
        for tenant, books, branch in (
            (cls.tenant, cls.books, cls.ikeja),
            (cls.solo_tenant, cls.solo_books, cls.solo_main),
        ):
            receivable = Account.objects.get(entity=books, code="1200")
            first = Customer.objects.create(
                entity=books, branch=branch, code="ADJ-DEEP-1", name="Ada Family",
                receivable_account=receivable,
            )
            second = Customer.objects.create(
                entity=books, branch=branch, code="ADJ-DEEP-2", name="Tunde Family",
                receivable_account=receivable,
            )
            invoice = Invoice.objects.create(
                entity=books, branch=branch, customer=first,
                invoice_date=datetime.date(2026, 1, 15),
                due_date=datetime.date(2026, 2, 15), subtotal=1_000, total=1_000,
            )
            concession = Concession.objects.create(
                entity=books, branch=branch, customer=first, invoice=invoice,
                concession_date=datetime.date(2026, 1, 20), amount=100,
                reason="Aid", reference="AID-1",
            )
            writeoff = WriteOffRequest.objects.create(
                entity=books, branch=branch, invoice=invoice, amount=100,
                narration="Bad debt", reason="Unpaid",
            )
            transfer = CustomerCreditTransfer.objects.create(
                entity=books, branch=branch, from_customer=first, to_customer=second,
                transfer_date=datetime.date(2026, 1, 20), amount=100,
                reason="Family request",
            )
            plan = PaymentPlan.objects.create(
                entity=books, branch=branch, customer=first, invoice=invoice,
                start_date=datetime.date(2026, 2, 1), installment_count=1,
                total_amount=1_000, notes="Monthly",
            )
            installment = PaymentPlanInstallment.objects.create(
                plan=plan, seq_no=1, due_date=datetime.date(2026, 2, 1),
                amount=1_000,
            )
            cls.records.append((tenant, concession, writeoff, transfer, plan, installment))

    def samples(self):
        samples = []
        for tenant, concession, writeoff, transfer, plan, installment in self.records:
            samples.extend((
                Sample(_SURFACE + "ConcessionSerializer", concession, tenant=tenant),
                Sample(_SURFACE + "WriteOffRequestSerializer", writeoff, tenant=tenant),
                Sample(_SURFACE + "CustomerCreditTransferSerializer", transfer,
                       tenant=tenant),
                Sample(_SURFACE + "PaymentPlanSerializer", plan, tenant=tenant),
                Sample(_SURFACE + "PaymentPlanInstallmentSerializer", installment,
                       tenant=tenant),
            ))
        return samples


class ExpenseClaimDeepPayloadTests(DeepPayloadChecks, TestCase):
    """A claim for a member of staff in a school with several branches, and a typed one in a school with one."""

    resources = frozenset({"finance.expenseclaim"})
    covers = frozenset({_SURFACE + "ExpenseClaimSerializer"})

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import ExpenseClaim, ExpenseClaimLine
        from vs_finance.tests_branch_scope import _FinanceBranchFixture

        seed_currencies()
        cls.school = make_school(slug="deep-claims", name="Deep Claims", status="ACTIVE")
        cls.tenant = cls.school.tenant
        ikeja = make_branch(cls.school, name="Ikeja Branch")
        make_branch(cls.school, name="Lekki Branch", is_main=False)
        cls.solo_school = make_school(slug="deep-claims-solo", name="Deep Claims Solo",
                                      status="ACTIVE")
        cls.solo_tenant = cls.solo_school.tenant
        solo_main = make_branch(cls.solo_school, name="Main Branch")
        tolu = get_user_model().objects.create_user(
            email="tolu@deep-claims.test", password="pw", tenant=cls.tenant,
            status="ACTIVE", first_name="Tolu", last_name="Fashola",
        )
        cls.claims = []
        for tenant, code, branch, claimant, name in (
            (cls.tenant, "DEEPCLM", ikeja, tolu, "Tolu Fashola"),
            (cls.solo_tenant, "DEEPCLMS", solo_main, None, "Mrs Okafor"),
        ):
            books = _FinanceBranchFixture.build_books(code, tenant)
            claim = ExpenseClaim.objects.create(
                entity=books, branch=branch, claimant=claimant, claimant_name=name,
                claim_date=datetime.date(2026, 1, 10), title="Lab chemicals",
            )
            ExpenseClaimLine.objects.create(
                claim=claim, line_no=1, description="Reagents", unit_price=12_500_00,
                net_amount=12_500_00,
                expense_account=Account.objects.get(entity=books, code="5300"),
            )
            cls.claims.append((tenant, claim))

    def samples(self):
        samples = []
        for tenant, claim in self.claims:
            samples += [
                Sample(_SURFACE + "ExpenseClaimSerializer", claim, tenant=tenant),
                Sample(_SURFACE + "ExpenseClaimSerializer", [claim], many=True, tenant=tenant),
            ]
        return samples


class ReceiptAndPettyCashDeepPayloadTests(DeepPayloadChecks, TestCase):
    """Receipt, fund and voucher fields close in single and multi-branch schools."""

    resources = frozenset({
        "finance.payment", "finance.pettycash", "finance.pettycashvoucher",
    })
    covers = frozenset({
        _SURFACE + "PaymentSerializer",
        _SURFACE + "PettyCashFundSerializer",
        _SURFACE + "PettyCashVoucherSerializer",
        _SURFACE + "PettyCashVoucherLineSerializer",
    })

    @classmethod
    def setUpTestData(cls):
        from vs_finance.models import Payment, PettyCashVoucher, PettyCashVoucherLine
        from vs_finance.tests_branch_scope import _FinanceBranchFixture

        seed_currencies()
        cls.samples_by_tenant = []
        for slug, code, multi in (
            ("deep-receipts", "DEEPREC", True),
            ("deep-receipts-solo", "DEEPRECS", False),
        ):
            school = make_school(slug=slug, name=slug, status="ACTIVE")
            branch = make_branch(school, name="Ikeja Branch" if multi else "Main Branch")
            if multi:
                make_branch(school, name="Lekki Branch", is_main=False)
            books = _FinanceBranchFixture.build_books(code, school.tenant)
            customer = _FinanceBranchFixture.customer(books, f"{code}-C", branch)
            account = Account.objects.get(entity=books, code="1100")
            payment = Payment.objects.create(
                entity=books, branch=branch, customer=customer,
                payment_date=datetime.date(2026, 1, 10), amount=125_000,
                deposit_account=account, reference="REC-DEEP", narration="Lab fees",
            )
            fund = PettyCashFund.objects.create(
                entity=books, branch=branch, name="Front desk",
                gl_account=Account.objects.get(entity=books, code="1110"),
                float_amount=150_000,
            )
            voucher = PettyCashVoucher.objects.create(
                entity=books, branch=branch, fund=fund,
                voucher_date=datetime.date(2026, 1, 10), payee="Mama Tunde",
                narration="Chalk", reference="PET-DEEP",
            )
            line = PettyCashVoucherLine.objects.create(
                voucher=voucher, line_no=1, description="Chalk",
                expense_account=Account.objects.get(entity=books, code="5300"),
                quantity=1, unit_price=4_500,
            )
            cls.samples_by_tenant.append((school.tenant, payment, fund, voucher, line))

    def samples(self):
        rows = []
        for tenant, payment, fund, voucher, line in self.samples_by_tenant:
            rows.extend((
                Sample(_SURFACE + "PaymentSerializer", payment, tenant=tenant),
                Sample(_SURFACE + "PettyCashFundSerializer", fund, tenant=tenant),
                Sample(_SURFACE + "PettyCashVoucherSerializer", voucher, tenant=tenant),
                Sample(_SURFACE + "PettyCashVoucherLineSerializer", line, tenant=tenant),
            ))
        return rows

