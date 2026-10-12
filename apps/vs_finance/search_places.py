"""The finance screens and settings the search bar can route to.

Every setting is built from the two tables its settings screen already serves,
so a setting renamed or added on the screen is renamed or added here with it:
the label from :mod:`vs_finance.settings_history` (the words the screen's
history uses) and the description from :mod:`vs_finance.settings_ownership`
(what the setting drives). The consumer's service name is carried as a keyword,
so "customer invoicing" finds the settings that drive it.

:data:`EXTRA_KEYWORDS` adds words only where a label would not match what a
person types: nobody looking for the invoice due date types "days until".

Every settings screen reads with ``finance.settings.view``. Finance, and the
payments half of the product that answers to the same capability, sit behind
the ``finance`` capability (:mod:`vs_rbac.capability_map`).

Some screens exist only sometimes, and name a condition
(:func:`vs_search.registry.register_condition`) that asks the same question
their endpoint or the console asks. The inter-branch screens (transfers,
balances, recharges, shared cost rules, held receipts) exist only at a tenant
with several branches (``several_branches``). The statutory pack exists only
for a reader whose transactions are not narrowed to some branches
(``finance.whole_tenant_reader``), which is the refusal its endpoint gives. The
finance overview opens to anybody holding any finance or payments key
(``finance.member``), as :class:`vs_rbac.permissions.HasAnyModuleAccess` lets
its endpoint.
"""
from __future__ import annotations

from vs_search.registry import ACTION, SETTING, Place, register, register_condition

from .constants import FinanceAuditAction
from .settings_history import finance_setting_fields
from .settings_ownership import (
    ACCOUNT_MAPPING_CONSUMERS,
    BANKING_SETTING_CONSUMERS,
    CALENDAR_SETTING_CONSUMERS,
    DOCUMENT_SETTING_CONSUMERS,
    PAYROLL_SETTING_CONSUMERS,
    RECEIVABLES_SETTING_CONSUMERS,
)

CAPABILITY = "finance"
SETTINGS_VIEW = "finance.settings.view"

SEVERAL_BRANCHES = "several_branches"
WHOLE_TENANT_READER = "finance.whole_tenant_reader"
FINANCE_MEMBER = "finance.member"

#: The modules whose keys open the finance overview (``FinanceDashboardView.rbac_modules``).
DASHBOARD_MODULES = ("finance", "payments")


def reads_whole_tenant(*, request, tenant, branch, user) -> bool:
    """Whether the statutory pack's endpoint would serve *user* rather than refuse them."""
    from vs_rbac.scoping import transaction_branch_scope_for_user

    return not transaction_branch_scope_for_user(user).is_narrowed


def is_finance_member(*, request, tenant, branch, user) -> bool:
    """Whether *user* holds any finance or payments key, as the overview's door asks."""
    from vs_rbac.permissions import holds_any_module_key

    permission_tenant = (
        getattr(request, "rbac_tenant", None) or getattr(request, "tenant", None)
        or getattr(user, "tenant", None)
    )
    return holds_any_module_key(user, permission_tenant, DASHBOARD_MODULES)


register_condition(WHOLE_TENANT_READER, reads_whole_tenant)
register_condition(FINANCE_MEMBER, is_finance_member)

#: Words a person types that a setting's label does not contain, by field.
EXTRA_KEYWORDS: dict[str, tuple[str, ...]] = {
    "default_invoice_due_days": ("due date", "invoice due", "payment terms"),
    "retention_years": ("retention", "archive"),
    "primary_collection_bank_account": ("payment details", "pay into"),
}

#: (screen suffix, screen label, screen description, audit family, consumers).
SETTINGS_SCREENS = (
    ("documents", "Document settings",
     "Defaults for customer invoices, credit and concessions.",
     FinanceAuditAction.FINANCE_DOCUMENT_SETTINGS_UPDATED, DOCUMENT_SETTING_CONSUMERS),
    ("banking", "Banking settings",
     "Bank reconciliation, receipt allocation and petty cash alerts.",
     FinanceAuditAction.FINANCE_BANKING_SETTINGS_UPDATED, BANKING_SETTING_CONSUMERS),
    ("receivables", "Receivables settings",
     "Deferred income, doubtful debts, deposits and payments from a payer.",
     FinanceAuditAction.FINANCE_RECEIVABLES_SETTINGS_UPDATED, RECEIVABLES_SETTING_CONSUMERS),
    ("payroll", "Payroll settings",
     "PAYE, pension and statutory deductions, and payslip delivery.",
     FinanceAuditAction.PAYROLL_SETTINGS_UPDATED, PAYROLL_SETTING_CONSUMERS),
    ("records", "Record retention settings",
     "How long financial records are kept and when they may be archived.",
     FinanceAuditAction.RETENTION_SETTINGS_UPDATED, {}),
    ("calendar", "Fiscal calendar settings",
     "How the next fiscal year opens and the order months close in.",
     FinanceAuditAction.FINANCE_CALENDAR_SETTINGS_UPDATED, CALENDAR_SETTING_CONSUMERS),
    ("account_mappings", "Account mappings",
     "Which ledger account each kind of posting uses.",
     FinanceAuditAction.FINANCE_SETTINGS_UPDATED, ACCOUNT_MAPPING_CONSUMERS),
)

#: (resource, label, description, permission) for each top-level finance list screen.
LIST_SCREENS = (
    ("invoices", "Customer invoices", "Bills raised to customers.", "finance.invoice.view"),
    ("receipts", "Customer receipts", "Payments received from customers.", "finance.payment.view"),
    ("customers", "Customers", "Everyone you bill.", "finance.customer.view"),
    ("credit_notes", "Credit notes", "", "finance.creditnote.view"),
    ("refunds", "Refunds and write-offs", "Money returned to customers and debts written off.",
     ("finance.refund.view", "finance.writeoff.view")),
    ("fee_structures", "Fee structures", "", "finance.feestructure.view"),
    ("concessions", "Concessions", "Discounts granted on bills.", "finance.concession.view"),
    ("payment_plans", "Payment plans", "Instalments agreed with a customer.", "finance.paymentplan.view"),
    ("deposits", "Customer deposits", "", "finance.deposit.view"),
    ("journals", "Journal entries", "", "finance.journal.view"),
    ("accounts", "Chart of accounts", "", "finance.account.view"),
    ("bank_accounts", "Bank accounts", "", "finance.bankaccount.view"),
    ("bank_transactions", "Bank transactions", "Statement lines and reconciliation.",
     "finance.banktransaction.view"),
    ("expense_claims", "Expense claims", "", "finance.expenseclaim.view"),
    ("petty_cash", "Petty cash funds", "", "finance.pettycash.view"),
    ("payroll_runs", "Payroll runs", "", "finance.payrollrun.view"),
    ("salary_structures", "Salary structures", "", "finance.salary.view"),
    ("budgets", "Budgets", "", "finance.budget.view"),
    ("fixed_assets", "Fixed assets", "", "finance.fixedasset.view"),
    ("tax", "Tax obligations", "Taxes owed and their filings.", "finance.tax.view"),
    ("audit_log", "Finance audit log", "", "finance.audit.view"),
    ("my_payslips", "My payslips", "Your own payslips and tax summary.", None),
    ("entities", "Ledger entities", "The organisations each set of books belongs to.",
     "finance.entity.view"),
    ("fiscal_periods", "Fiscal periods", "Financial years and the months open for posting.",
     "finance.period.view"),
    ("cost_centres", "Cost centres", "Where spending is charged.", "finance.costcenter.view"),
    ("dimensions", "Dimensions", "Extra tags postings are analysed by.", "finance.dimension.view"),
    ("currencies", "Currencies and exchange rates", "",
     ("finance.currency.view", "finance.fxrate.view")),
    ("tax_codes", "Tax codes", "VAT and withholding rates applied to lines.", "finance.taxcode.view"),
    ("tax_tables", "Tax tables", "PAYE bands, states and pension fund administrators.",
     ("finance.statutory.create", "finance.statutory.update")),
    ("payer_payments", "Payer payments", "One payment from a payer covering several bills.",
     "finance.payment.view"),
    ("credit_transfers", "Credit transfers", "Credit moved from one customer to another.",
     "finance.credittransfer.view"),
    ("dunning", "Dunning", "Reminders for overdue bills.", "finance.dunning.view"),
    ("doubtful_debts", "Doubtful debts", "Provisions for bills unlikely to be paid.",
     "finance.provision.view"),
    ("deferred_income", "Deferred income", "Fees received for a term not yet taught.",
     "finance.deferredincome.view"),
    ("trial_balance", "Trial balance", "", "finance.report.view"),
    ("income_statement", "Income statement", "Income and expenses for a period.",
     "finance.report.view"),
    ("balance_sheet", "Balance sheet", "What the school owns and owes.", "finance.report.view"),
    ("cash_flow", "Cash flow statement", "", "finance.report.view"),
    ("changes_in_equity", "Changes in equity", "", "finance.report.view"),
    ("analytics", "Cost and dimension analysis", "", "finance.report.view"),
    ("closed_figures", "Closed figures", "Sealed periods and whether their figures still match.",
     "finance.seal.view"),
)


#: (resource, label, description, permission, condition, keywords) for each
#: finance screen that exists only sometimes.
CONDITIONAL_SCREENS = (
    ("inter_branch_transfers", "Inter-branch transfers",
     "Money, stock and balances moved from one branch to another.",
     "finance.interbranch.view", SEVERAL_BRANCHES, ("transfer between branches",)),
    ("inter_branch_balances", "Inter-branch balances",
     "What each branch owes the others.",
     "finance.interbranch.view", SEVERAL_BRANCHES, ("owed between branches",)),
    ("recharges", "Recharges between branches",
     "Costs one branch paid on another's behalf, charged back.",
     "finance.interbranch.view", SEVERAL_BRANCHES, ("recharge", "inter-branch")),
    ("shared_cost_rules", "Shared cost rules",
     "How a cost paid by one branch is split across the branches it serves.",
     "finance.interbranch.view", SEVERAL_BRANCHES, ("allocation", "inter-branch")),
    ("held_receipts", "Held receipts",
     "Money received at one branch for another branch's bills.",
     "finance.payment.view", SEVERAL_BRANCHES, ("inter-branch",)),
    ("statutory_pack", "Statutory reporting pack",
     "The whole school's statements for its filings.",
     "finance.report.view", WHOLE_TENANT_READER, ("ifrs", "filing", "annual accounts")),
    ("dashboard", "Finance overview",
     "The finance console's landing page.",
     None, FINANCE_MEMBER, ("dashboard", "finance home")),
)


def _setting_places(suffix, group, family, consumers):
    screen = f"finance.settings.{suffix}"
    for raw_name, spec in finance_setting_fields(family).items():
        name = str(raw_name)
        consumer = consumers.get(raw_name) or {}
        keywords = tuple(filter(None, (consumer.get("service"),))) + EXTRA_KEYWORDS.get(name, ())
        yield Place(
            kind=SETTING,
            key=f"{screen}.{name}",
            label=spec.label,
            description=consumer.get("impact", ""),
            keywords=keywords,
            group=group,
            screen=screen,
            anchor=name,
            permission=SETTINGS_VIEW,
            capability=CAPABILITY,
        )


def places():
    """Every finance place, screens first."""
    for suffix, label, description, family, consumers in SETTINGS_SCREENS:
        yield Place(
            kind=ACTION, key=f"finance.settings.{suffix}", label=label,
            description=description, keywords=("finance settings",),
            screen=f"finance.settings.{suffix}",
            permission=SETTINGS_VIEW, capability=CAPABILITY,
        )
        yield from _setting_places(suffix, label, family, consumers)
    for resource, label, description, permission in LIST_SCREENS:
        yield Place(
            kind=ACTION, key=f"finance.{resource}", label=label,
            description=description, screen=f"finance.{resource}",
            permission=permission, capability=CAPABILITY,
        )
    for resource, label, description, permission, condition, keywords in CONDITIONAL_SCREENS:
        yield Place(
            kind=ACTION, key=f"finance.{resource}", label=label,
            description=description, keywords=keywords, screen=f"finance.{resource}",
            permission=permission, capability=CAPABILITY, condition=condition,
        )


register(*places())
