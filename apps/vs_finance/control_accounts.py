"""Accounts a sub-ledger keeps, and the lock that keeps hand-typed journals off them.

A control account is only as true as the documents behind it. The AR control
equals the sum of open invoices because nothing else posts to it; once a person
can type a line straight into it, the control and its sub-ledger disagree for good
and the reconciliation at the close fails from then on. So a journal typed by hand
(a direct entry, or any journal whose source is ``MANUAL`` or ``OPENING``) may not
carry a line on an account a sub-ledger keeps. The documents that do move those
accounts post with their own source (sales, purchase, bank, payroll, tax, close,
system) and are untouched; one that has to post an ``OPENING`` journal to a control
account, an opening supplier bill, says so explicitly with ``allow_control_accounts``.

Which accounts are kept is read from the books, never from a list kept by hand:

* the entity's account mappings for the roles a sub-ledger owns (receivables,
  payables, customer credit, vendor advances, GR/IR, inventory, output VAT, WHT,
  gateway clearing, the inter-branch balances and the money held for other
  branches, and on the platform's books the client funds it holds and the
  provider balance behind them);
* the owning rows: every customer's receivable account, every bank account's and
  petty cash fund's ledger, and every tax obligation's payable and recoverable
  accounts;
* whatever other apps register through :func:`register_control_account_provider`
  (procurement adds each vendor's payable account and each stock item's inventory
  account), so finance never imports them.
"""
from __future__ import annotations

from .constants import AccountMappingKey, JournalSource
from .exceptions import PostingError

#: Journal sources a person types. Every sub-ledger document posts with another.
HAND_SOURCES = frozenset({JournalSource.MANUAL, JournalSource.OPENING})

#: Mapping role -> (who keeps it, the documents to use instead).
_MAPPED_ROLES = {
    AccountMappingKey.ACCOUNTS_RECEIVABLE: (
        "the receivables ledger", "an invoice, a receipt or a credit note"),
    AccountMappingKey.CUSTOMER_CREDIT: (
        "the receivables ledger", "a receipt, a credit note or a refund"),
    AccountMappingKey.ACCOUNTS_PAYABLE: (
        "the payables ledger",
        "a vendor bill, a vendor payment, a vendor credit note or an opening bill"),
    AccountMappingKey.VENDOR_ADVANCE: (
        "the payables ledger", "a vendor payment or a vendor credit note"),
    AccountMappingKey.GRIR_CLEARING: (
        "the purchasing ledger", "a goods receipt, a goods return or a vendor bill"),
    AccountMappingKey.INVENTORY_ASSET: (
        "the stock ledger", "a goods receipt, a stock issue or a stock adjustment"),
    AccountMappingKey.OUTPUT_VAT: (
        "the tax ledger", "the invoice or bill that carries the tax, or a tax filing"),
    AccountMappingKey.WHT_PAYABLE: (
        "the tax ledger", "the vendor payment that withholds it, or a tax filing"),
    AccountMappingKey.GATEWAY_CLEARING: (
        "the payment gateway", "an online collection or a gateway settlement"),
    AccountMappingKey.CLIENT_FUNDS_HELD: (
        "the held-funds ledger", "a client's online payment, settlement or payout"),
    AccountMappingKey.PROVIDER_BALANCE: (
        "the held-funds ledger", "a client's online payment, settlement or payout"),
    AccountMappingKey.CLIENT_FUNDS_OWED: (
        "the held-funds ledger", "a client's chargeback, online payment or settlement"),
    AccountMappingKey.INTER_BRANCH: (
        "the inter-branch ledger",
        "an inter-branch transfer, a recharge, a goods transfer or a receivable move"),
    AccountMappingKey.HELD_FOR_OTHER_BRANCHES: (
        "the inter-branch ledger",
        "a receipt held for another branch, or the transfer that forwards it"),
}

_BANK = ("its bank account", "a receipt, a vendor payment or a bank transaction")
_PETTY_CASH = (
    "its petty cash fund", "a petty cash voucher, a fund top-up or a return to the bank")
_TAX = ("the tax ledger", "the invoice, bill or payroll run that carries the tax, or a tax filing")
_CUSTOMER = _MAPPED_ROLES[AccountMappingKey.ACCOUNTS_RECEIVABLE]

_PROVIDERS: list = []


class ControlAccountLockedError(PostingError):
    """A hand-typed journal named an account a sub-ledger keeps."""

    error_code = "CONTROL_ACCOUNT_LOCKED"
    default_message = "A hand-typed journal cannot post to an account that documents keep."


def register_control_account_provider(fn):
    """Register ``fn(entity) -> {account_id: (kept_by, use_instead)}``. Idempotent."""
    if fn not in _PROVIDERS:
        _PROVIDERS.append(fn)
    return fn


def control_accounts(entity) -> dict:
    """``{account_id: (kept_by, use_instead)}`` for every account a sub-ledger keeps."""
    from .account_mappings import ACCOUNT_MAPPING_SPECS, mapping_keys_for
    from .models import (
        Account, BankAccount, Customer, FinanceAccountMapping, PettyCashFund, TaxObligation,
    )

    kept: dict = {}
    roles = {key: owner for key, owner in _MAPPED_ROLES.items() if key in mapping_keys_for(entity)}

    def keep(account_id, owner):
        if account_id is not None and account_id not in kept:
            kept[account_id] = owner

    overrides = dict(
        FinanceAccountMapping.objects.filter(entity=entity, key__in=list(roles))
        .values_list("key", "account_id")
    )
    defaults = dict(
        Account.objects.filter(
            entity=entity,
            code__in=[ACCOUNT_MAPPING_SPECS[key][0] for key in roles
                      if key not in overrides],
        ).values_list("code", "id")
    )
    for key, owner in roles.items():
        keep(overrides.get(key) or defaults.get(ACCOUNT_MAPPING_SPECS[key][0]), owner)
    for account_id in BankAccount.objects.filter(entity=entity).values_list("gl_account_id", flat=True):
        keep(account_id, _BANK)
    for account_id in PettyCashFund.objects.filter(entity=entity).values_list("gl_account_id", flat=True):
        keep(account_id, _PETTY_CASH)
    for liability, recoverable in TaxObligation.objects.filter(entity=entity).values_list(
            "liability_account_id", "recoverable_account_id"):
        keep(liability, _TAX)
        keep(recoverable, _TAX)
    for account_id in Customer.objects.filter(entity=entity).values_list(
            "receivable_account_id", flat=True).distinct():
        keep(account_id, _CUSTOMER)
    for provider in _PROVIDERS:
        for account_id, owner in (provider(entity) or {}).items():
            keep(account_id, owner)
    return kept


def ensure_no_control_lines(entity, accounts) -> None:
    """Refuse a hand-typed journal naming any account in ``accounts`` a sub-ledger keeps.

    The message names the account and the document that moves it, so the person is
    told what to do rather than only what they may not.
    """
    accounts = [account for account in accounts if account is not None]
    if not accounts:
        return
    kept = control_accounts(entity)
    for account in accounts:
        owner = kept.get(account.pk)
        if owner is None:
            continue
        kept_by, use_instead = owner
        raise ControlAccountLockedError(
            f"Account {account.code} {account.name} is kept by {kept_by}, so a journal "
            f"typed by hand cannot post to it. Record it with {use_instead} instead.",
            account_code=account.code,
        )


def ensure_hand_journal_allowed(entry) -> None:
    """Apply the lock to ``entry`` when its source says a person typed it."""
    if entry.source not in HAND_SOURCES:
        return
    ensure_no_control_lines(
        entry.entity, [line.account for line in entry.lines.select_related("account")],
    )
