"""Default reference data: currencies and a starter Chart of Accounts.

The CoA here is a deliberately small, domain-neutral skeleton — the five roots and
the handful of control accounts every entity needs (cash, AR, AP, VAT, share capital,
retained earnings, a generic income and expense). Product adapters (school fees,
payroll …) extend it; nothing here mentions students or schools, honouring the
horizontal-module rule.
"""
from __future__ import annotations

from .constants import AccountType, IFRSLine, TaxFilingFrequency, TaxObligationType

#: ISO currencies the platform knows out of the box. NGN is the platform base.
DEFAULT_CURRENCIES = [
    {"code": "NGN", "name": "Nigerian Naira", "symbol": "₦", "minor_unit": 2},
    {"code": "USD", "name": "US Dollar", "symbol": "$", "minor_unit": 2},
    {"code": "GBP", "name": "Pound Sterling", "symbol": "£", "minor_unit": 2},
    {"code": "EUR", "name": "Euro", "symbol": "€", "minor_unit": 2},
]

#: (code, name, type, is_postable, is_contra). Header rows (is_postable=False) give
#: the tree its sections; leaves take postings.
DEFAULT_CHART = [
    # Assets
    ("1000", "Assets", AccountType.ASSET, False, False),
    ("1100", "Cash & Bank", AccountType.ASSET, True, False),
    ("1110", "Petty Cash", AccountType.ASSET, True, False),
    ("1200", "Accounts Receivable", AccountType.ASSET, True, False),
    ("1300", "Input VAT (Recoverable)", AccountType.ASSET, True, False),
    ("1400", "Inventory", AccountType.ASSET, True, False),
    ("1500", "Property, Plant & Equipment", AccountType.ASSET, True, False),
    ("1900", "Accumulated Depreciation", AccountType.ASSET, True, True),
    # Liabilities
    ("2000", "Liabilities", AccountType.LIABILITY, False, False),
    ("2100", "Accounts Payable", AccountType.LIABILITY, True, False),
    ("2140", "Customer Credit Balances", AccountType.LIABILITY, True, False),
    ("2150", "GR/IR Clearing", AccountType.LIABILITY, True, False),
    ("2200", "Output VAT (Payable)", AccountType.LIABILITY, True, False),
    ("2300", "WHT Payable", AccountType.LIABILITY, True, False),
    ("2310", "PAYE Payable", AccountType.LIABILITY, True, False),
    ("2320", "Pension Payable", AccountType.LIABILITY, True, False),
    ("2330", "Net Wages Payable", AccountType.LIABILITY, True, False),
    ("2400", "Accrued Reimbursements", AccountType.LIABILITY, True, False),
    # Equity
    ("3000", "Equity", AccountType.EQUITY, False, False),
    ("3100", "Share Capital", AccountType.EQUITY, True, False),
    ("3200", "Retained Earnings", AccountType.EQUITY, True, False),
    # Income
    ("4000", "Income", AccountType.INCOME, False, False),
    ("4100", "Operating Revenue", AccountType.INCOME, True, False),
    ("4900", "Sales Returns & Allowances", AccountType.INCOME, True, True),
    ("4910", "Discounts & Concessions Allowed", AccountType.INCOME, True, True),
    # Expenses
    ("5000", "Expenses", AccountType.EXPENSE, False, False),
    ("5100", "Cost of Sales", AccountType.EXPENSE, True, False),
    ("5150", "Inventory Adjustments", AccountType.EXPENSE, True, False),
    ("5200", "Salaries & Wages", AccountType.EXPENSE, True, False),
    ("5300", "General & Administrative", AccountType.EXPENSE, True, False),
    ("5400", "Depreciation Expense", AccountType.EXPENSE, True, False),
    ("5500", "Bank Charges", AccountType.EXPENSE, True, False),
]

#: Starter statutory tax obligations for a Nigerian entity. Each row maps a tax to
#: the liability control account it drains (and, for VAT, the recoverable input
#: account it nets against). ``code`` is the stable key for idempotent seeding.
#: (code, name, type, liability_code, recoverable_code, authority, frequency, filing_day)
DEFAULT_TAX_OBLIGATIONS = [
    ("VAT", "Value Added Tax", TaxObligationType.VAT, "2200", "1300",
     "Federal Inland Revenue Service", TaxFilingFrequency.MONTHLY, 21),
    ("WHT", "Withholding Tax", TaxObligationType.WHT, "2300", None,
     "Federal Inland Revenue Service", TaxFilingFrequency.MONTHLY, 21),
    ("PAYE", "Pay As You Earn", TaxObligationType.PAYE, "2310", None,
     "State Internal Revenue Service", TaxFilingFrequency.MONTHLY, 10),
    ("PENSION", "Pension Contributions", TaxObligationType.PENSION, "2320", None,
     "Pension Fund Administrator", TaxFilingFrequency.MONTHLY, 7),
]

#: IFRS-for-SMEs presentation line for each default-chart account code. Lets the
#: statutory export pack regroup the raw chart into the lines a FIRS / CAC filing
#: expects. Codes absent here fall back to the type default at read time.
DEFAULT_IFRS_LINE_BY_CODE = {
    # Assets
    "1100": IFRSLine.CASH, "1110": IFRSLine.CASH,
    "1200": IFRSLine.TRADE_RECEIVABLES,
    "1300": IFRSLine.CURRENT_TAX_ASSET,
    "1400": IFRSLine.INVENTORIES,
    "1500": IFRSLine.PPE, "1900": IFRSLine.PPE,
    # Liabilities
    "2100": IFRSLine.TRADE_PAYABLES, "2140": IFRSLine.TRADE_PAYABLES, "2150": IFRSLine.TRADE_PAYABLES,
    "2200": IFRSLine.CURRENT_TAX_PAYABLE, "2300": IFRSLine.CURRENT_TAX_PAYABLE,
    "2310": IFRSLine.EMPLOYEE_PAYABLES, "2320": IFRSLine.EMPLOYEE_PAYABLES,
    "2330": IFRSLine.EMPLOYEE_PAYABLES, "2400": IFRSLine.TRADE_PAYABLES,
    # Equity
    "3100": IFRSLine.SHARE_CAPITAL, "3200": IFRSLine.RETAINED_EARNINGS,
    # Income
    "4100": IFRSLine.REVENUE, "4900": IFRSLine.REVENUE, "4910": IFRSLine.REVENUE,
    # Expenses
    "5100": IFRSLine.COST_OF_SALES, "5150": IFRSLine.COST_OF_SALES,
    "5200": IFRSLine.ADMIN_EXPENSES, "5300": IFRSLine.ADMIN_EXPENSES,
    "5400": IFRSLine.ADMIN_EXPENSES, "5500": IFRSLine.FINANCE_COSTS,
}

#: parent_code by child_code — wires the tree after the flat create.
_PARENTS = {
    "1100": "1000", "1110": "1000", "1200": "1000", "1300": "1000", "1400": "1000",
    "1500": "1000", "1900": "1000",
    "2100": "2000", "2140": "2000", "2150": "2000", "2200": "2000", "2300": "2000",
    "2310": "2000", "2320": "2000", "2330": "2000", "2400": "2000",
    "3100": "3000", "3200": "3000",
    "4100": "4000", "4900": "4000", "4910": "4000",
    "5100": "5000", "5150": "5000", "5200": "5000", "5300": "5000",
    "5400": "5000", "5500": "5000",
}


def seed_currencies():
    """Create the default currencies (idempotent). Returns the count touched."""
    from .models import Currency

    for spec in DEFAULT_CURRENCIES:
        Currency.objects.update_or_create(code=spec["code"], defaults=spec)
    return len(DEFAULT_CURRENCIES)


def seed_chart_of_accounts(entity):
    """Create the default Chart of Accounts for ``entity`` (idempotent per code).

    Safe to re-run: accounts are keyed by ``(entity, code)`` and only created when
    absent. Returns the list of :class:`~vs_finance.models.Account` rows for the
    entity after seeding.
    """
    from .models import Account

    created: dict[str, Account] = {}
    for code, name, acc_type, postable, contra in DEFAULT_CHART:
        # ``normal_balance`` is left for Account.save() to derive from type + contra.
        ifrs_line = DEFAULT_IFRS_LINE_BY_CODE.get(code, "")
        account, was_created = Account.objects.get_or_create(
            entity=entity, code=code,
            defaults={
                "name": name,
                "account_type": acc_type,
                "is_postable": postable,
                "is_contra": contra,
                "ifrs_line": ifrs_line,
            },
        )
        # Backfill the IFRS line on a pre-existing account that hasn't been mapped yet
        # (e.g. a chart seeded before statutory packs existed); never override a line
        # an operator has set deliberately.
        if not was_created and ifrs_line and not account.ifrs_line:
            account.ifrs_line = ifrs_line
            account.save(update_fields=["ifrs_line", "updated_at"])
        created[code] = account

    # Second pass: link parents now that every node exists.
    for child_code, parent_code in _PARENTS.items():
        child = created.get(child_code) or Account.objects.filter(entity=entity, code=child_code).first()
        parent = created.get(parent_code) or Account.objects.filter(entity=entity, code=parent_code).first()
        if child and parent and child.parent_id != parent.id:
            child.parent = parent
            child.save(update_fields=["parent", "updated_at"])

    seed_tax_obligations(entity)
    return list(Account.objects.filter(entity=entity).order_by("code"))


def seed_fiscal_year(entity, year=None, start_month=1):
    """Open a fiscal year for ``entity`` with twelve monthly OPEN periods (idempotent).

    ``year`` is the label used in document numbers (defaults to the current calendar
    year). ``start_month`` (1–12) is the opening month: ``1`` gives a calendar-year
    Jan–Dec book, while e.g. ``9`` gives a school year that runs Sept of ``year``
    through Aug of ``year + 1`` — the twelve periods roll across the calendar boundary.

    Returns ``(fiscal_year, [periods])``. Safe to re-run: the year is keyed by
    ``(entity, year)`` and each period by ``(fiscal_year, period_no)``, so an existing
    set of books is left untouched.
    """
    import datetime

    from django.utils import timezone

    from .models import FiscalPeriod, FiscalYear

    if year is None:
        year = timezone.now().year
    if not 1 <= start_month <= 12:
        raise ValueError("start_month must be between 1 and 12.")

    def _month(offset):
        """Calendar (year, month) for the period ``offset`` months after the start."""
        index = (start_month - 1) + offset           # 0-based month index from the epoch
        return year + index // 12, index % 12 + 1

    first_y, first_m = _month(0)
    last_y, last_m = _month(11)
    # End of the last period = day before the first of the month after it.
    after_y, after_m = _month(12)

    fiscal_year, _ = FiscalYear.objects.get_or_create(
        entity=entity, year=year,
        defaults={
            "start_date": datetime.date(first_y, first_m, 1),
            "end_date": datetime.date(after_y, after_m, 1) - datetime.timedelta(days=1),
        },
    )

    periods = []
    for i in range(12):
        py, pm = _month(i)
        ny, nm = _month(i + 1)
        start = datetime.date(py, pm, 1)
        end = datetime.date(ny, nm, 1) - datetime.timedelta(days=1)
        period, _ = FiscalPeriod.objects.get_or_create(
            fiscal_year=fiscal_year, period_no=i + 1,
            defaults={
                "entity": entity,
                "name": f"{py}-{pm:02d}",
                "start_date": start,
                "end_date": end,
            },
        )
        periods.append(period)
    return fiscal_year, periods


def seed_tax_obligations(entity):
    """Create the default statutory tax obligations for ``entity`` (idempotent).

    Keyed by ``(entity, code)`` so re-running is safe. Each obligation points at the
    liability control account it drains; VAT additionally references the recoverable
    input-VAT account it nets against at filing time. Returns the list of
    :class:`~vs_finance.models.TaxObligation` rows for the entity after seeding.
    """
    from .models import Account, TaxObligation

    accounts = {a.code: a for a in Account.objects.filter(entity=entity)}
    for code, name, obtype, liab_code, recov_code, authority, freq, day in DEFAULT_TAX_OBLIGATIONS:
        liability = accounts.get(liab_code)
        if liability is None:
            # Without its control account the obligation can't post; skip rather
            # than create an orphan that would fail at filing time.
            continue
        TaxObligation.objects.get_or_create(
            entity=entity, code=code,
            defaults={
                "name": name,
                "obligation_type": obtype,
                "liability_account": liability,
                "recoverable_account": accounts.get(recov_code) if recov_code else None,
                "authority_name": authority,
                "frequency": freq,
                "filing_day": day,
            },
        )

    return list(TaxObligation.objects.filter(entity=entity).order_by("code"))
