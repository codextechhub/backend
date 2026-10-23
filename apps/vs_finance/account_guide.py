"""What each account is for, which the software keeps, and how a new one is added.

A chart of accounts that opens with fifty-odd rows is only readable when each
row explains itself and the ones the software posts to are told apart from the
ones a person works with. This module is the single home of that guidance. It
changes no account and no posting; it only describes the chart.

* :data:`ACCOUNT_PURPOSES` is a plain-language sentence for every account in the
  default chart, written for a reader who is not an accountant. The chart seed
  and the data migration that back-fills existing books both copy it onto
  ``Account.description``.
* :func:`managed_accounts` says which accounts the software keeps for you. A
  locked entry is a control account a sub-ledger owns
  (:func:`vs_finance.control_accounts.control_accounts`), which a hand-typed
  journal may not post to. An unlocked entry is posted by the software but is
  not locked: a year-end close moves retained earnings, a payroll run moves net
  wages payable, yet an accountant may still correct either with a journal. A
  bank account's ledger and a petty cash fund's ledger are the organisation's
  own money and stay in the everyday view, so they are never listed.
* :func:`reserved_account_codes` lists the codes the software names itself (every
  code in the default chart and every default code of an account role), and
  :func:`ensure_code_not_reserved` refuses a new account that takes one the books
  do not already hold, so a code a later feature needs is never taken by a
  look-alike.
* :func:`suggest_account_code` picks the next free code in an account type's
  thousand, so adding an account needs no knowledge of four-digit codes.
* :func:`account_suggestions` is a catalogue of common accounts any
  organisation can add in one step. The generic ones live here; a product
  adds its own through :func:`register_account_suggestion_provider`, so this
  engine never names a product's vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from rest_framework.exceptions import ValidationError

from .constants import (
    ACCOUNT_CODE_LENGTH,
    ACCOUNT_TYPE_BY_CODE_PREFIX,
    ACCUM_DEPRECIATION_CODE,
    ACCRUED_REIMBURSEMENT_CODE,
    EMPLOYER_PENSION_EXPENSE_CODE,
    ITF_EXPENSE_CODE,
    NET_WAGES_PAYABLE_CODE,
    NSITF_EXPENSE_CODE,
    SALES_RETURNS_CODE,
    AccountMappingKey,
    AccountType,
    IFRSLine,
    account_type_from_code,
)

#: One plain sentence or two for each account of the default chart, by code. The
#: words are a customer, a payer, a service period and staff, never a product's
#: own vocabulary, because this engine serves more than one product.
ACCOUNT_PURPOSES: dict[str, str] = {
    "1000": (
        "The heading for everything the organisation owns. You cannot post to it; "
        "its figure is the total of the accounts under it."
    ),
    "1100": (
        "The money held in your bank accounts and as cash. It goes up when payers "
        "pay you and down when you pay vendors or staff."
    ),
    "1110": (
        "Small cash kept on hand for day-to-day spending. It goes down as vouchers "
        "are paid out and up when the float is topped up from the bank."
    ),
    "1125": (
        "Online payments the payment provider has confirmed but not yet paid into "
        "your bank. It empties when the provider's settlement reaches the bank."
    ),
    "1200": (
        "What customers and payers owe you for invoices not yet paid. It goes up "
        "when you issue an invoice and down when a payment or credit note is applied."
    ),
    "1290": (
        "An estimate of the money owed to you that you do not expect to collect. "
        "It sits against Accounts receivable and lowers it, so you do not overstate "
        "what you are owed."
    ),
    "1240": (
        "Money paid to a vendor before their bill arrived. It empties when the "
        "bill is recorded and the advance is set against it."
    ),
    "1260": (
        "What each branch owes to, or is owed by, the other branches. Across the "
        "whole organisation it always adds up to zero."
    ),
    "1300": (
        "VAT you paid on purchases that you can claim back against the VAT you "
        "collect. It grows with each vendor bill that carries VAT."
    ),
    "1400": (
        "The value of stock you hold. It goes up when goods are received and down "
        "when stock is issued or written off."
    ),
    "1500": (
        "The cost of long-lasting items you own, such as buildings, vehicles, "
        "furniture and equipment. It goes up when you buy one and down when you "
        "dispose of one."
    ),
    "1900": (
        "The total wear and tear written off your long-lasting items so far. It "
        "sits against Property, Plant & Equipment and grows each time depreciation "
        "is run."
    ),
    "2000": (
        "The heading for everything the organisation owes. You cannot post to it; "
        "its figure is the total of the accounts under it."
    ),
    "2100": (
        "What you owe vendors for bills you have received but not yet paid. It goes "
        "up when a vendor bill is recorded and down when you pay it."
    ),
    "2140": (
        "Money a customer has paid ahead of an invoice, such as an overpayment. It "
        "is used up when it is set against a later invoice, or refunded."
    ),
    "2150": (
        "Goods that have arrived but whose supplier bill has not been entered yet. "
        "It empties when the bill is recorded."
    ),
    "2160": (
        "Fees billed before the service period they cover begins. Each month of the "
        "period moves its share from here into income."
    ),
    "2170": (
        "Refundable deposits you are holding for customers. This is their money, "
        "not your income; it leaves when the deposit is returned, set against a "
        "bill, or forfeited."
    ),
    "2190": (
        "Money one branch received that belongs to another branch. It empties when "
        "the money is passed on to the branch it belongs to."
    ),
    "2195": (
        "Payments that reached a branch's payment terminal before anyone knew which "
        "customer paid. Each one leaves when you match it to the customer."
    ),
    "2200": (
        "VAT you have collected on sales and owe to the tax authority. It grows with "
        "each invoice that carries VAT and empties when you file and pay."
    ),
    "2300": (
        "WHT (withholding tax) held back from vendor payments on the tax authority's "
        "behalf. You owe it until you remit it."
    ),
    "2310": (
        "PAYE (income tax) taken from staff pay that you owe the tax authority. It "
        "builds up with each payroll run and empties when you remit it."
    ),
    "2320": (
        "Pension contributions, from staff pay and from the employer, that you owe "
        "the pension fund administrators. It empties when you pay them."
    ),
    "2330": (
        "Take-home pay you owe staff once a payroll run is approved. It empties "
        "when the salaries are paid."
    ),
    "2340": (
        "NHF (National Housing Fund) contributions taken from staff pay. You owe "
        "them until you remit them."
    ),
    "2350": (
        "NSITF (employee compensation) contributions you owe the Nigeria Social "
        "Insurance Trust Fund. It builds up with each payroll run and empties when "
        "you pay."
    ),
    "2360": (
        "The ITF (Industrial Training Fund) levy you owe. It builds up with payroll "
        "and empties when you pay it."
    ),
    "2400": (
        "What you owe staff for approved expense claims they paid out of their own "
        "pocket. It empties when you reimburse them."
    ),
    "3000": (
        "The heading for the owners' stake in the organisation: money put in and "
        "profit kept. You cannot post to it."
    ),
    "3100": (
        "Money the owners put into the organisation. It changes only when capital "
        "is put in or taken out."
    ),
    "3200": (
        "Profits kept in the organisation over the years. At each year-end the "
        "year's profit or loss is moved here for you."
    ),
    "4000": (
        "The heading for everything you earn. You cannot post to it; its figure is "
        "the total of the accounts under it."
    ),
    "4100": (
        "Your main earnings from what you charge customers. Each invoice you issue "
        "adds to it, unless the charge is for a later service period."
    ),
    "4810": (
        "Money received later on a bill you had already written off as lost."
    ),
    "4820": (
        "Deposits customers did not claim before the deadline, which become your "
        "income once they are forfeited."
    ),
    "4900": (
        "Income you give back when you issue a credit note. It lowers your total "
        "income instead of being shown as an expense."
    ),
    "4910": (
        "Fee reductions and concessions you grant customers. It lowers your total "
        "income and is shown separately so you can see how much you gave away."
    ),
    "5000": (
        "The heading for everything you spend. You cannot post to it; its figure is "
        "the total of the accounts under it."
    ),
    "5100": (
        "The direct cost of what you sell, such as stock used up. It goes up when "
        "stock is issued or a cost is charged to a sale."
    ),
    "5150": (
        "Stock lost, damaged or found when a count differs from the records. A "
        "write-down is a cost here; a gain lowers it."
    ),
    "5160": (
        "The difference between the price on a vendor's bill and the price expected "
        "when the goods arrived. It is a cost when the bill is higher."
    ),
    "5200": (
        "What you spend on staff pay. Each approved payroll run charges the gross "
        "pay here."
    ),
    "5210": (
        "The employer's own pension contribution, charged with each payroll run on "
        "top of staff pay."
    ),
    "5220": (
        "The employer's NSITF contribution, charged with each payroll run."
    ),
    "5230": (
        "The ITF (Industrial Training Fund) levy, charged as a cost with each "
        "payroll run."
    ),
    "5300": (
        "Everyday running costs that have no account of their own, such as office "
        "supplies and small repairs. It is where general purchases go by default."
    ),
    "5350": (
        "Money owed to you that you have given up on collecting, and the amounts "
        "set aside for debts you doubt. Write-offs and provisions charge it."
    ),
    "5400": (
        "The wear and tear on your long-lasting items, charged to each period. "
        "Every depreciation run adds to it."
    ),
    "5500": (
        "Fees your bank charges you, such as transfer fees and account upkeep. They "
        "are recorded from your bank transactions."
    ),
    "5520": (
        "Online payments that the payer's bank took back. It records the loss when "
        "a chargeback is accepted."
    ),
    "5530": (
        "Differences found when petty cash is counted. A shortage is a cost here; "
        "an overage lowers it."
    ),
}


class ManagedBy(NamedTuple):
    """Who keeps an account for you and what to use to move it.

    ``keeper`` names the part of the software that posts to the account and
    ``moved_by`` the documents that move it, both worded for a reader who is not
    an accountant. ``hand_journals`` is True when a journal typed by hand may
    still post to the account; a locked control account is False.
    """

    keeper: str
    moved_by: str
    hand_journals: bool


#: Roles the software posts to that a journal typed by hand may still reach:
#: mapping role -> (who keeps it, the documents that move it).
_UNLOCKED_ROLES = {
    AccountMappingKey.RETAINED_EARNINGS: (
        "the year-end close", "closing the financial year"),
    AccountMappingKey.DEFERRED_INCOME: (
        "the receivables ledger", "an invoice billed ahead of its service period, or the monthly release"),
    AccountMappingKey.DEPOSITS_HELD: (
        "the receivables ledger", "a customer deposit, or its return, set-off or forfeit"),
    AccountMappingKey.DOUBTFUL_DEBT_ALLOWANCE: (
        "the receivables ledger", "a doubtful-debt provision or a write-off"),
    AccountMappingKey.BAD_DEBT_RECOVERED: (
        "the receivables ledger", "a receipt on an invoice that was written off"),
    AccountMappingKey.FORFEITED_DEPOSIT_INCOME: (
        "the receivables ledger", "forfeiting a deposit nobody claimed"),
    AccountMappingKey.INVENTORY_ADJUSTMENT: (
        "the stock ledger", "a stock adjustment"),
    AccountMappingKey.PURCHASE_PRICE_VARIANCE: (
        "the purchasing ledger", "a vendor bill priced differently from the goods received"),
    AccountMappingKey.CHARGEBACKS: (
        "the payment gateway", "a chargeback on an online payment"),
    AccountMappingKey.CASH_OVER_SHORT: (
        "its petty cash fund", "a petty cash count"),
    AccountMappingKey.BAD_DEBT_EXPENSE: (
        "the receivables ledger", "a write-off or a doubtful-debt provision"),
}

#: Accounts the software posts to that carry no mapping role, by default code:
#: code -> (who keeps it, the documents that move it).
_UNLOCKED_CODES = {
    ACCUM_DEPRECIATION_CODE: (
        "the fixed assets register", "a depreciation run or an asset disposal"),
    NET_WAGES_PAYABLE_CODE: (
        "payroll", "a payroll run and the payment of its salaries"),
    ACCRUED_REIMBURSEMENT_CODE: (
        "expense claims", "an approved expense claim and its reimbursement"),
    EMPLOYER_PENSION_EXPENSE_CODE: ("payroll", "a payroll run"),
    NSITF_EXPENSE_CODE: ("payroll", "a payroll run"),
    ITF_EXPENSE_CODE: ("payroll", "a payroll run"),
    SALES_RETURNS_CODE: ("the receivables ledger", "a credit note"),
}


def managed_accounts(entity) -> dict[int, ManagedBy]:
    """``{account_id: ManagedBy}`` for every account the software keeps for you.

    The locked accounts are those of
    :func:`vs_finance.control_accounts.control_accounts`, less every bank
    account's and petty cash fund's ledger, which hold the organisation's own
    money. The unlocked ones are the roles and default codes the software posts
    to without locking them (:data:`_UNLOCKED_ROLES`, :data:`_UNLOCKED_CODES`);
    a role resolves to the account the books map it to, else to its default
    code. An account that is both is reported locked.

    The cost is a fixed handful of queries however large the chart is.
    """
    from .account_mappings import ACCOUNT_MAPPING_SPECS, mapping_keys_for
    from .control_accounts import _BANK, _PETTY_CASH, control_accounts
    from .models import Account, FinanceAccountMapping

    managed: dict[int, ManagedBy] = {}
    for account_id, (keeper, moved_by) in control_accounts(entity).items():
        if (keeper, moved_by) in (_BANK, _PETTY_CASH):
            continue
        managed[account_id] = ManagedBy(keeper, moved_by, False)

    roles = {key: owner for key, owner in _UNLOCKED_ROLES.items()
             if key in mapping_keys_for(entity)}
    overrides = dict(
        FinanceAccountMapping.objects.filter(entity=entity, key__in=list(roles))
        .values_list("key", "account_id")
    )
    default_codes = {ACCOUNT_MAPPING_SPECS[key][0] for key in roles if key not in overrides}
    default_codes |= set(_UNLOCKED_CODES)
    ids_by_code = dict(
        Account.objects.filter(entity=entity, code__in=default_codes)
        .values_list("code", "id")
    )

    def add(account_id, owner):
        if account_id is not None and account_id not in managed:
            managed[account_id] = ManagedBy(owner[0], owner[1], True)

    for key, owner in roles.items():
        add(overrides.get(key) or ids_by_code.get(ACCOUNT_MAPPING_SPECS[key][0]), owner)
    for code, owner in _UNLOCKED_CODES.items():
        add(ids_by_code.get(code), owner)
    return managed


def managed_by_payload(managed: ManagedBy | None) -> dict | None:
    """The API shape of a :class:`ManagedBy`, or None for an everyday account."""
    return None if managed is None else dict(managed._asdict())


def reserved_account_codes() -> dict[str, str]:
    """``{code: name}`` for every code the software names itself.

    Derived, never listed by hand: every code of the default chart and every
    default code of an account role (the roles only the platform's books carry
    among them), so a role added later reserves its code without an edit here.
    A role's code absent from the chart is named from the role's label.
    """
    from .account_mappings import ACCOUNT_MAPPING_SPECS
    from .seed import DEFAULT_CHART

    reserved = {code: name for code, name, *_ in DEFAULT_CHART}
    for key, (code, _account_type) in ACCOUNT_MAPPING_SPECS.items():
        reserved.setdefault(code, AccountMappingKey(key).label)
    return reserved


def _type_bounds(account_type) -> tuple[int, int]:
    """The first and last code of ``account_type``'s thousand."""
    try:
        account_type = AccountType(account_type)
    except ValueError:
        raise ValidationError({"account_type": (
            "Choose an account type: " + ", ".join(AccountType.values) + "."
        )}) from None
    lead = next(prefix for prefix, kind in ACCOUNT_TYPE_BY_CODE_PREFIX.items()
                if kind == account_type)
    span = 10 ** (ACCOUNT_CODE_LENGTH - 1)
    return int(lead) * span, int(lead) * span + span - 1


def suggest_account_code(entity, account_type) -> str:
    """The next free code for a new ``account_type`` account of ``entity``.

    Taken codes are the books' own four-digit codes together with every
    reserved code, so a suggestion never lands on a code the software needs.
    The suggestion is ten above the highest taken code in the type's range,
    stepping by ten until free. When that runs off the end of the range, the
    lowest free code above the range's first is used; a range with none free
    raises a validation error.
    """
    from .models import Account

    low, high = _type_bounds(account_type)
    taken = {
        int(code) for code in Account.objects.filter(
            entity=entity, code__startswith=str(low)[0],
        ).values_list("code", flat=True)
        if code.isdigit() and len(code) == ACCOUNT_CODE_LENGTH
    }
    taken |= {int(code) for code in reserved_account_codes()
              if code.isdigit() and len(code) == ACCOUNT_CODE_LENGTH}
    in_range = [code for code in taken if low <= code <= high]
    candidate = (max(in_range, default=low) // 10) * 10 + 10
    while candidate <= high and candidate in taken:
        candidate += 10
    if candidate <= high:
        return str(candidate)
    for candidate in range(low + 1, high + 1):
        if candidate not in taken:
            return str(candidate)
    raise ValidationError({"account_type": (
        f"Every code from {low} to {high} is taken. Remove an unused account "
        f"before adding another."
    )})


def ensure_code_not_reserved(entity, code, *, error=None) -> None:
    """Refuse ``code`` for a new account when the software keeps it for itself.

    A code the books already hold is not this check's concern (the caller's own
    "already exists" refusal says so), and the platform's own seeding of its
    default accounts does not pass through here. The message names the system
    account and offers the next free code of the same type. ``error`` is the
    exception class a caller raises for a refusal (default: a field error on
    ``code``), so a service with its own error type keeps it.
    """
    from .models import Account

    code = str(code).strip()
    name = reserved_account_codes().get(code)
    if name is None or Account.objects.filter(entity=entity, code=code).exists():
        return
    account_type = account_type_from_code(code)
    message = f"Code {code} is kept for the system account {name}."
    if account_type is not None:
        message += f" Use {suggest_account_code(entity, account_type)} instead."
    if error is not None:
        raise error(message)
    raise ValidationError({"code": message})


@dataclass(frozen=True)
class AccountSuggestion:
    """A common account a person can add in one step.

    ``key`` is a stable slug the client can remember; ``ifrs_line`` is an
    :class:`~vs_finance.constants.IFRSLine` value, or blank to let the account
    take its type's default presentation line.
    """

    key: str
    name: str
    account_type: str
    description: str
    ifrs_line: str = ""


def _suggestion(key, name, account_type, description, ifrs_line=""):
    return AccountSuggestion(key, name, str(account_type), description, str(ifrs_line))


#: Domain-neutral accounts almost every organisation ends up adding.
_GENERIC_SUGGESTIONS = (
    _suggestion("utilities", "Utilities (electricity and water)", AccountType.EXPENSE,
                "Electricity and water bills.", IFRSLine.ADMIN_EXPENSES),
    _suggestion("fuel-generator", "Fuel and generator", AccountType.EXPENSE,
                "Diesel, petrol and generator servicing.", IFRSLine.ADMIN_EXPENSES),
    _suggestion("repairs-maintenance", "Repairs and maintenance", AccountType.EXPENSE,
                "Fixing and servicing buildings, furniture and equipment.",
                IFRSLine.ADMIN_EXPENSES),
    _suggestion("internet-phone", "Internet and phone", AccountType.EXPENSE,
                "Internet, airtime and telephone bills.", IFRSLine.ADMIN_EXPENSES),
    _suggestion("cleaning-sanitation", "Cleaning and sanitation", AccountType.EXPENSE,
                "Cleaning materials, refuse collection and sanitation services.",
                IFRSLine.ADMIN_EXPENSES),
    _suggestion("security-services", "Security services", AccountType.EXPENSE,
                "Guards, alarms and other security services.", IFRSLine.ADMIN_EXPENSES),
    _suggestion("rent", "Rent", AccountType.EXPENSE,
                "Rent paid for premises you do not own.", IFRSLine.ADMIN_EXPENSES),
    _suggestion("insurance", "Insurance", AccountType.EXPENSE,
                "Premiums for property, vehicle and liability cover.",
                IFRSLine.ADMIN_EXPENSES),
    _suggestion("printing-stationery", "Printing and stationery", AccountType.EXPENSE,
                "Paper, printing and office stationery.", IFRSLine.ADMIN_EXPENSES),
    _suggestion("staff-training", "Staff training", AccountType.EXPENSE,
                "Courses and workshops for staff.", IFRSLine.ADMIN_EXPENSES),
    _suggestion("professional-fees", "Professional fees (audit, legal)", AccountType.EXPENSE,
                "Fees paid to auditors, lawyers and other professionals.",
                IFRSLine.ADMIN_EXPENSES),
    _suggestion("advertising-marketing", "Advertising and marketing", AccountType.EXPENSE,
                "Adverts, banners and printing for promotions and events.",
                IFRSLine.DISTRIBUTION_COSTS),
    _suggestion("vehicle-running", "Vehicle running costs", AccountType.EXPENSE,
                "Fuel, servicing, licences and repairs for your vehicles.",
                IFRSLine.ADMIN_EXPENSES),
    _suggestion("meals-catering", "Meals and catering", AccountType.EXPENSE,
                "Food and drink bought for staff, visitors and events.",
                IFRSLine.ADMIN_EXPENSES),
    _suggestion("donations-grants", "Donations and grants received", AccountType.INCOME,
                "Gifts and grants given to you that are not payment for a service.",
                IFRSLine.OTHER_INCOME),
    _suggestion("interest-income", "Interest income", AccountType.INCOME,
                "Interest your bank or investments pay you.", IFRSLine.OTHER_INCOME),
    _suggestion("furniture-fittings", "Furniture and fittings", AccountType.ASSET,
                "The cost of desks, chairs, shelves and fittings you own.", IFRSLine.PPE),
    _suggestion("vehicles", "Vehicles", AccountType.ASSET,
                "The cost of cars, buses and other vehicles you own.", IFRSLine.PPE),
    _suggestion("computers-equipment", "Computers and equipment", AccountType.ASSET,
                "The cost of computers, printers and other equipment you own.", IFRSLine.PPE),
    _suggestion("buildings", "Buildings", AccountType.ASSET,
                "The cost of buildings you own.", IFRSLine.PPE),
    _suggestion("prepaid-expenses", "Prepaid expenses", AccountType.ASSET,
                "Costs paid in advance, such as a year's insurance, used up month by month.",
                IFRSLine.OTHER_CURRENT_ASSETS),
    _suggestion("staff-loans-advances", "Staff loans and advances", AccountType.ASSET,
                "Money lent to staff or paid to them ahead of pay, to be repaid.",
                IFRSLine.TRADE_RECEIVABLES),
    _suggestion("loans-payable", "Loans payable", AccountType.LIABILITY,
                "Money you have borrowed and still have to repay.",
                IFRSLine.SHORT_TERM_BORROWINGS),
    _suggestion("accrued-expenses", "Accrued expenses", AccountType.LIABILITY,
                "Costs you have incurred but not yet been billed for.",
                IFRSLine.TRADE_PAYABLES),
)

_SUGGESTION_PROVIDERS: list = []


def register_account_suggestion_provider(fn):
    """Register ``fn(entity) -> list[AccountSuggestion]``. Idempotent.

    A product registers the accounts that belong to its own trade, returning an
    empty list for books it does not serve, so this engine names none of them.
    """
    if fn not in _SUGGESTION_PROVIDERS:
        _SUGGESTION_PROVIDERS.append(fn)
    return fn


def account_suggestions(entity) -> list[AccountSuggestion]:
    """The generic suggestions followed by every provider's, one per key."""
    seen: dict[str, AccountSuggestion] = {}
    for suggestion in (*_GENERIC_SUGGESTIONS,
                       *(item for fn in _SUGGESTION_PROVIDERS for item in (fn(entity) or ()))):
        seen.setdefault(suggestion.key, suggestion)
    return list(seen.values())


def suggestions_payload(entity) -> list[dict]:
    """:func:`account_suggestions` as API rows, each saying if the books have it.

    A suggestion is already added when the books hold an account of that name,
    compared without regard to case or surrounding spaces.
    """
    from .models import Account

    held = {name.strip().lower() for name in
            Account.objects.filter(entity=entity).values_list("name", flat=True)}
    return [
        {
            "key": item.key,
            "name": item.name,
            "account_type": item.account_type,
            "description": item.description,
            "ifrs_line": item.ifrs_line,
            "already_added": item.name.strip().lower() in held,
        }
        for item in account_suggestions(entity)
    ]
