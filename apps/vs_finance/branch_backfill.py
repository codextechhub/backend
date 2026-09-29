"""Finance's transaction models, as the branch backfill derives them.

Discovered by :mod:`vs_finance.branch_derivation` on first use. Each target's
sources are tried in the order written here and the first answer wins; see that
module for what happens when none answers. The order numbers put a model after
every model it reads: bank accounts first, because payments and refunds read
them; invoices before everything that reads an invoice; journals last, because
they read the documents that raised them.

Where a model has no source at all, ``no_source_note`` is the reason an
administrator is shown, and a one-branch tenant still files the row under its
only branch.
"""
from __future__ import annotations

from .branch_derivation import (
    JournalOwner,
    Target,
    agreeing,
    bank_behind,
    banks_on_journal,
    customer,
    journal_owner,
    register_journal_owner,
    register_target,
    user_branch,
    via,
)

INVOICE = "vs_finance.Invoice"
PAYMENT = "vs_finance.Payment"
CREDIT_NOTE = "vs_finance.CreditNote"
BANK_ACCOUNT = "vs_finance.BankAccount"
FIXED_ASSET = "vs_finance.FixedAsset"
JOURNAL = "vs_finance.JournalEntry"

_TARGETS = (
    Target(
        BANK_ACCOUNT, (), order=5,
        no_source_note=(
            "a bank account shared by several branches becomes one record per "
            "branch; an administrator decides which branch this record is"
        ),
    ),
    Target(INVOICE, (customer(),), order=10),
    Target(
        PAYMENT,
        (
            agreeing("the allocated invoices", "vs_finance.PaymentAllocation", "payment", ("invoice", INVOICE)),
            bank_behind("the deposit bank account", "deposit_account"),
            customer(),
        ),
        order=20,
    ),
    Target(CREDIT_NOTE, (via("the invoice", "invoice", INVOICE), customer()), order=30),
    Target(
        "vs_finance.WriteOffRequest",
        (via("the invoice", "invoice", INVOICE), customer("invoice__customer")),
        order=30,
    ),
    Target("vs_finance.Concession", (via("the invoice", "invoice", INVOICE), customer()), order=30),
    Target("vs_finance.PaymentPlan", (via("the invoice", "invoice", INVOICE), customer()), order=30),
    Target("vs_finance.DunningNotice", (via("the invoice", "invoice", INVOICE), customer()), order=30),
    Target(
        "vs_finance.Refund",
        (
            agreeing(
                "the refunded notes and payments", "vs_finance.RefundAllocation", "refund",
                ("payment", PAYMENT), ("note", CREDIT_NOTE),
            ),
            via("the bank account", "bank_account", BANK_ACCOUNT),
            customer(),
        ),
        order=40,
    ),
    Target(
        "vs_finance.PettyCashFund",
        (
            user_branch("the custodian", "custodian"),
            agreeing("the fund's vouchers", "vs_finance.PettyCashVoucher", "fund", ("id", "vs_finance.PettyCashVoucher")),
        ),
        order=50,
    ),
    Target("vs_finance.PettyCashVoucher", (via("the fund", "fund", "vs_finance.PettyCashFund"),), order=55),
    Target("vs_finance.ExpenseClaim", (user_branch("the claimant", "claimant"),), order=50),
    Target(FIXED_ASSET, (banks_on_journal("the funding bank account", "acquisition_journal"),), order=50),
    Target(
        "vs_finance.PayrollRun", (), order=50,
        no_source_note=(
            "a payroll run covers staff of several branches, with a journal per "
            "branch; an administrator decides which branch the run itself is"
        ),
    ),
    Target(
        "vs_finance.TaxFiling", (), order=50,
        no_source_note="a return is filed per branch; an administrator decides which branch this one is",
    ),
    Target(
        "vs_finance.Budget", (), order=50,
        no_source_note="every budget belongs to a branch; an administrator decides which",
    ),
    Target(
        JOURNAL,
        (
            journal_owner(),
            via("the entry it reverses", "reverses", JOURNAL),
            banks_on_journal("the bank accounts on its lines", "pk"),
        ),
        order=900,
    ),
)

#: Every finance model holding a foreign key to the journal it raised.
_JOURNAL_OWNERS = (
    JournalOwner(INVOICE, "journal"),
    JournalOwner(PAYMENT, "journal"),
    JournalOwner(CREDIT_NOTE, "journal"),
    JournalOwner("vs_finance.Refund", "journal"),
    JournalOwner("vs_finance.WriteOffRequest", "journal"),
    JournalOwner("vs_finance.Concession", "journal"),
    JournalOwner("vs_finance.ExpenseClaim", "journal"),
    JournalOwner("vs_finance.PettyCashVoucher", "journal"),
    JournalOwner("vs_finance.TaxFiling", "filing_journal"),
    JournalOwner("vs_finance.TaxFilingShare", "filing_journal"),
    JournalOwner("vs_finance.TaxRemittance", "journal"),
    JournalOwner("vs_finance.TaxRemittance", "reversal_journal"),
    JournalOwner("vs_finance.PayrollRun", "journal"),
    JournalOwner("vs_finance.PayrollRun", "disbursement_journal"),
    JournalOwner(FIXED_ASSET, "acquisition_journal"),
    JournalOwner(FIXED_ASSET, "disposal_journal"),
    JournalOwner("vs_finance.DepreciationSchedule", "journal", via="asset", via_label=FIXED_ASSET),
    JournalOwner("vs_finance.BankStatementLine", "adjusting_journal", via="bank_account", via_label=BANK_ACCOUNT),
    JournalOwner("vs_finance.CustomerCreditAllocationJournal", "journal", via="payment", via_label=PAYMENT),
    JournalOwner("vs_finance.CustomerCreditAllocationJournal", "journal", via="note", via_label=CREDIT_NOTE),
)

for _target in _TARGETS:
    register_target(_target)
for _owner in _JOURNAL_OWNERS:
    register_journal_owner(_owner)
