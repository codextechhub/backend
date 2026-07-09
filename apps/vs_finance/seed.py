"""Default reference data: currencies and a starter Chart of Accounts.

The CoA here is a deliberately small, domain-neutral skeleton — the five roots and
the handful of control accounts every entity needs (cash, AR, AP, VAT, share capital,
retained earnings, a generic income and expense). Product adapters (school fees,
payroll …) extend it; nothing here mentions students or schools, honouring the
horizontal-module rule.
"""
from __future__ import annotations  # Defer annotation evaluation during app import.

from .constants import AccountType, IFRSLine, TaxFilingFrequency, TaxObligationType  # Enums used by seed data.

#: ISO currencies the platform knows out of the box. NGN is the platform base.
DEFAULT_CURRENCIES = [  # Currency rows created by seed_currencies.
    {"code": "NGN", "name": "Nigerian Naira", "symbol": "₦", "minor_unit": 2},  # Nigerian naira.
    {"code": "USD", "name": "US Dollar", "symbol": "$", "minor_unit": 2},  # US dollar.
    {"code": "GBP", "name": "Pound Sterling", "symbol": "£", "minor_unit": 2},  # Pound sterling.
    {"code": "EUR", "name": "Euro", "symbol": "€", "minor_unit": 2},  # Euro.
]  # Close the grouped expression.

#: (code, name, type, is_postable, is_contra). Header rows (is_postable=False) give
#: the tree its sections; leaves take postings.
DEFAULT_CHART = [  # Starter chart tuples: code, name, type, postable, contra.
    # Assets  # Asset root and default asset accounts.
    ("1000", "Assets", AccountType.ASSET, False, False),  # Asset section header.
    ("1100", "Cash & Bank", AccountType.ASSET, True, False),  # Main cash/bank account.
    ("1110", "Petty Cash", AccountType.ASSET, True, False),  # Petty cash account.
    ("1200", "Accounts Receivable", AccountType.ASSET, True, False),  # AR control account.
    ("1300", "Input VAT (Recoverable)", AccountType.ASSET, True, False),  # Recoverable VAT account.
    ("1400", "Inventory", AccountType.ASSET, True, False),  # Inventory account.
    ("1500", "Property, Plant & Equipment", AccountType.ASSET, True, False),  # PPE cost account.
    ("1900", "Accumulated Depreciation", AccountType.ASSET, True, True),  # Contra-asset depreciation account.
    # Liabilities  # Liability root and default liability accounts.
    ("2000", "Liabilities", AccountType.LIABILITY, False, False),  # Liability section header.
    ("2100", "Accounts Payable", AccountType.LIABILITY, True, False),  # AP control account.
    ("2140", "Customer Credit Balances", AccountType.LIABILITY, True, False),  # Customer credits liability.
    ("2150", "GR/IR Clearing", AccountType.LIABILITY, True, False),  # Goods-received/invoice-received clearing.
    ("2200", "Output VAT (Payable)", AccountType.LIABILITY, True, False),  # Output VAT payable.
    ("2300", "WHT Payable", AccountType.LIABILITY, True, False),  # Withholding tax payable.
    ("2310", "PAYE Payable", AccountType.LIABILITY, True, False),  # PAYE payable.
    ("2320", "Pension Payable", AccountType.LIABILITY, True, False),  # Pension payable.
    ("2330", "Net Wages Payable", AccountType.LIABILITY, True, False),  # Net payroll payable.
    ("2400", "Accrued Reimbursements", AccountType.LIABILITY, True, False),  # Staff reimbursement liability.
    # Equity  # Equity root and default equity accounts.
    ("3000", "Equity", AccountType.EQUITY, False, False),  # Equity section header.
    ("3100", "Share Capital", AccountType.EQUITY, True, False),  # Share capital account.
    ("3200", "Retained Earnings", AccountType.EQUITY, True, False),  # Retained earnings account.
    # Income  # Income root and default revenue accounts.
    ("4000", "Income", AccountType.INCOME, False, False),  # Income section header.
    ("4100", "Operating Revenue", AccountType.INCOME, True, False),  # Primary operating revenue.
    ("4900", "Sales Returns & Allowances", AccountType.INCOME, True, True),  # Contra-revenue returns account.
    ("4910", "Discounts & Concessions Allowed", AccountType.INCOME, True, True),  # Contra-revenue discounts account.
    # Expenses  # Expense root and default expense accounts.
    ("5000", "Expenses", AccountType.EXPENSE, False, False),  # Expense section header.
    ("5100", "Cost of Sales", AccountType.EXPENSE, True, False),  # Cost of sales account.
    ("5150", "Inventory Adjustments", AccountType.EXPENSE, True, False),  # Inventory adjustment expense.
    ("5200", "Salaries & Wages", AccountType.EXPENSE, True, False),  # Payroll expense account.
    ("5300", "General & Administrative", AccountType.EXPENSE, True, False),  # General admin expense.
    ("5400", "Depreciation Expense", AccountType.EXPENSE, True, False),  # Depreciation expense.
    ("5500", "Bank Charges", AccountType.EXPENSE, True, False),  # Bank charges expense.
]  # Close the grouped expression.

#: Starter statutory tax obligations for a Nigerian entity. Each row maps a tax to
#: the liability control account it drains (and, for VAT, the recoverable input
#: account it nets against). ``code`` is the stable key for idempotent seeding.
#: (code, name, type, liability_code, recoverable_code, authority, frequency, filing_day)
DEFAULT_TAX_OBLIGATIONS = [  # Starter statutory obligations.
    ("VAT", "Value Added Tax", TaxObligationType.VAT, "2200", "1300",  # VAT payable and recoverable accounts.
     "Federal Inland Revenue Service", TaxFilingFrequency.MONTHLY, 21),  # VAT authority and due day.
    ("WHT", "Withholding Tax", TaxObligationType.WHT, "2300", None,  # WHT payable account.
     "Federal Inland Revenue Service", TaxFilingFrequency.MONTHLY, 21),  # WHT authority and due day.
    ("PAYE", "Pay As You Earn", TaxObligationType.PAYE, "2310", None,  # PAYE payable account.
     "State Internal Revenue Service", TaxFilingFrequency.MONTHLY, 10),  # PAYE authority and due day.
    ("PENSION", "Pension Contributions", TaxObligationType.PENSION, "2320", None,  # Pension payable account.
     "Pension Fund Administrator", TaxFilingFrequency.MONTHLY, 7),  # Pension authority and due day.
]  # Close the grouped expression.

#: IFRS-for-SMEs presentation line for each default-chart account code. Lets the
#: statutory export pack regroup the raw chart into the lines a FIRS / CAC filing
#: expects. Codes absent here fall back to the type default at read time.
DEFAULT_IFRS_LINE_BY_CODE = {  # Maps default account codes to statutory presentation lines.
    # Assets  # Default asset presentation mappings.
    "1100": IFRSLine.CASH, "1110": IFRSLine.CASH,  # Cash and petty cash.
    "1200": IFRSLine.TRADE_RECEIVABLES,  # Accounts receivable.
    "1300": IFRSLine.CURRENT_TAX_ASSET,  # Recoverable input VAT.
    "1400": IFRSLine.INVENTORIES,  # Inventory.
    "1500": IFRSLine.PPE, "1900": IFRSLine.PPE,  # PPE and accumulated depreciation.
    # Liabilities  # Default liability presentation mappings.
    "2100": IFRSLine.TRADE_PAYABLES, "2140": IFRSLine.TRADE_PAYABLES, "2150": IFRSLine.TRADE_PAYABLES,  # AP-like balances.
    "2200": IFRSLine.CURRENT_TAX_PAYABLE, "2300": IFRSLine.CURRENT_TAX_PAYABLE,  # Tax payables.
    "2310": IFRSLine.EMPLOYEE_PAYABLES, "2320": IFRSLine.EMPLOYEE_PAYABLES,  # Employee statutory payables.
    "2330": IFRSLine.EMPLOYEE_PAYABLES, "2400": IFRSLine.TRADE_PAYABLES,  # Wages and reimbursements.
    # Equity  # Default equity presentation mappings.
    "3100": IFRSLine.SHARE_CAPITAL, "3200": IFRSLine.RETAINED_EARNINGS,  # Equity accounts.
    # Income  # Default revenue presentation mappings.
    "4100": IFRSLine.REVENUE, "4900": IFRSLine.REVENUE, "4910": IFRSLine.REVENUE,  # Revenue and contra-revenue.
    # Expenses  # Default expense presentation mappings.
    "5100": IFRSLine.COST_OF_SALES, "5150": IFRSLine.COST_OF_SALES,  # Cost of sales lines.
    "5200": IFRSLine.ADMIN_EXPENSES, "5300": IFRSLine.ADMIN_EXPENSES,  # Admin expenses.
    "5400": IFRSLine.ADMIN_EXPENSES, "5500": IFRSLine.FINANCE_COSTS,  # Depreciation and finance costs.
}  # Close the grouped expression.

#: parent_code by child_code — wires the tree after the flat create.
_PARENTS = {  # Parent account code by child account code.
    "1100": "1000", "1110": "1000", "1200": "1000", "1300": "1000", "1400": "1000",  # Asset children.
    "1500": "1000", "1900": "1000",  # More asset children.
    "2100": "2000", "2140": "2000", "2150": "2000", "2200": "2000", "2300": "2000",  # Liability children.
    "2310": "2000", "2320": "2000", "2330": "2000", "2400": "2000",  # More liability children.
    "3100": "3000", "3200": "3000",  # Equity children.
    "4100": "4000", "4900": "4000", "4910": "4000",  # Income children.
    "5100": "5000", "5150": "5000", "5200": "5000", "5300": "5000",  # Expense children.
    "5400": "5000", "5500": "5000",  # More expense children.
}  # Close the grouped expression.


def seed_currencies():  # Create or update platform default currencies.
    """Create the default currencies (idempotent). Returns the count touched."""
    from .models import Currency  # Local import avoids model import cycles.

    for spec in DEFAULT_CURRENCIES:  # Upsert each default currency.
        Currency.objects.update_or_create(code=spec["code"], defaults=spec)  # Key currency by ISO code.
    return len(DEFAULT_CURRENCIES)  # Return number of seed rows touched.


def seed_chart_of_accounts(entity):  # Create or update the starter chart for one entity.
    """Create the default Chart of Accounts for ``entity`` (idempotent per code).

    Safe to re-run: accounts are keyed by ``(entity, code)`` and only created when
    absent. Returns the list of :class:`~vs_finance.models.Account` rows for the
    entity after seeding.
    """
    from .models import Account  # Local import avoids model import cycles.

    created: dict[str, Account] = {}  # Account objects keyed by code for parent linking.
    for code, name, acc_type, postable, contra in DEFAULT_CHART:  # Create each default account.
        # ``normal_balance`` is left for Account.save() to derive from type + contra.  # Avoid duplicating model logic.
        ifrs_line = DEFAULT_IFRS_LINE_BY_CODE.get(code, "")  # Optional statutory presentation mapping.
        account, was_created = Account.objects.get_or_create(  # Idempotently create account by entity/code.
            entity=entity, code=code,  # Unique account identity within an entity.
            defaults={  # Defaults used only on first create.
                "name": name,  # Account name.
                "account_type": acc_type,  # Account type.
                "is_postable": postable,  # Whether journals may post directly here.
                "is_contra": contra,  # Whether normal balance is contra to account type.
                "ifrs_line": ifrs_line,  # Statutory presentation line.
            },  # Close the grouped value.
        )  # Close the grouped expression.
        # Backfill the IFRS line on a pre-existing account that hasn't been mapped yet
        # (e.g. a chart seeded before statutory packs existed); never override a line
        # an operator has set deliberately.  # Preserve manual chart customization.
        if not was_created and ifrs_line and not account.ifrs_line:  # Backfill only unmapped old accounts.
            account.ifrs_line = ifrs_line  # Set missing statutory line.
            account.save(update_fields=["ifrs_line", "updated_at"])  # Persist only mapping fields.
        created[code] = account  # Store account for parent pass.

    # Second pass: link parents now that every node exists.  # Parent rows may be created later in the first pass.
    for child_code, parent_code in _PARENTS.items():  # Wire chart hierarchy.
        child = created.get(child_code) or Account.objects.filter(entity=entity, code=child_code).first()  # Resolve child account.
        parent = created.get(parent_code) or Account.objects.filter(entity=entity, code=parent_code).first()  # Resolve parent account.
        if child and parent and child.parent_id != parent.id:  # Update only when link differs.
            child.parent = parent  # Assign chart parent.
            child.save(update_fields=["parent", "updated_at"])  # Persist parent link.

    seed_tax_obligations(entity)  # Seed statutory obligations after control accounts exist.
    return list(Account.objects.filter(entity=entity).order_by("code"))  # Return complete chart in code order.


def seed_fiscal_year(entity, year=None, start_month=1):  # Create a fiscal year and 12 monthly periods.
    """Open a fiscal year for ``entity`` with twelve monthly OPEN periods (idempotent).

    ``year`` is the label used in document numbers (defaults to the current calendar
    year). ``start_month`` (1–12) is the opening month: ``1`` gives a calendar-year
    Jan–Dec book, while e.g. ``9`` gives a school year that runs Sept of ``year``
    through Aug of ``year + 1`` — the twelve periods roll across the calendar boundary.

    Returns ``(fiscal_year, [periods])``. Safe to re-run: the year is keyed by
    ``(entity, year)`` and each period by ``(fiscal_year, period_no)``, so an existing
    set of books is left untouched.
    """
    import datetime  # Local import keeps module import light.

    from django.utils import timezone  # Supplies current year default.

    from .models import FiscalPeriod, FiscalYear  # Fiscal year and period models.

    if year is None:  # Default to current calendar year.
        year = timezone.now().year  # Use timezone-aware current date source.
    if not 1 <= start_month <= 12:  # Month must be valid.
        raise ValueError("start_month must be between 1 and 12.")

    def _month(offset):  # Calculate period month at offset from fiscal start.
        """Calendar (year, month) for the period ``offset`` months after the start."""
        index = (start_month - 1) + offset           # 0-based month index from the epoch  # Allows rollover across years.
        return year + index // 12, index % 12 + 1  # Return calendar year and month.

    first_y, first_m = _month(0)  # Fiscal year start month.
    last_y, last_m = _month(11)  # Last fiscal period month.
    # End of the last period = day before the first of the month after it.  # Handles month length automatically.
    after_y, after_m = _month(12)  # First month after fiscal year.

    fiscal_year, _ = FiscalYear.objects.get_or_create(  # Idempotently create fiscal year.
        entity=entity, year=year,  # Unique fiscal year identity.
        defaults={  # Dates used only on first creation.
            "start_date": datetime.date(first_y, first_m, 1),  # First fiscal period start.
            "end_date": datetime.date(after_y, after_m, 1) - datetime.timedelta(days=1),  # Last fiscal period end.
        },  # Close the grouped value.
    )  # Close the grouped expression.

    periods = []  # Periods returned to caller.
    for i in range(12):  # Create twelve monthly periods.
        py, pm = _month(i)  # Current period year/month.
        ny, nm = _month(i + 1)  # Next period year/month.
        start = datetime.date(py, pm, 1)  # Period starts on first day of month.
        end = datetime.date(ny, nm, 1) - datetime.timedelta(days=1)  # Period ends day before next month.
        period, _ = FiscalPeriod.objects.get_or_create(  # Idempotently create monthly period.
            fiscal_year=fiscal_year, period_no=i + 1,  # Unique period within fiscal year.
            defaults={  # Fields used only on first creation.
                "entity": entity,  # Duplicate entity for faster scoped queries.
                "name": f"{py}-{pm:02d}",  # Period display name.
                "start_date": start,  # Period start date.
                "end_date": end,  # Period end date.
            },  # Close the grouped value.
        )  # Close the grouped expression.
        periods.append(period)  # Preserve period order.
    return fiscal_year, periods  # Return fiscal year and its periods.


def seed_tax_obligations(entity):  # Create statutory tax obligations for one entity.
    """Create the default statutory tax obligations for ``entity`` (idempotent).

    Keyed by ``(entity, code)`` so re-running is safe. Each obligation points at the
    liability control account it drains; VAT additionally references the recoverable
    input-VAT account it nets against at filing time. Returns the list of
    :class:`~vs_finance.models.TaxObligation` rows for the entity after seeding.
    """
    from .models import Account, TaxObligation  # Local import avoids model import cycles.

    accounts = {a.code: a for a in Account.objects.filter(entity=entity)}  # Cache chart accounts by code.
    for code, name, obtype, liab_code, recov_code, authority, freq, day in DEFAULT_TAX_OBLIGATIONS:  # Seed each obligation.
        liability = accounts.get(liab_code)  # Resolve required liability control account.
        if liability is None:  # Skip obligations missing mandatory posting account.
            # Without its control account the obligation can't post; skip rather
            # than create an orphan that would fail at filing time.  # Avoid broken seed rows.
            continue  # Skip to the next loop iteration.
        TaxObligation.objects.get_or_create(  # Idempotently create tax obligation.
            entity=entity, code=code,  # Unique obligation identity.
            defaults={  # Fields used only on first creation.
                "name": name,  # Display name.
                "obligation_type": obtype,  # VAT/WHT/PAYE/etc.
                "liability_account": liability,  # Payable account drained at filing.
                "recoverable_account": accounts.get(recov_code) if recov_code else None,  # Optional recoverable account.
                "authority_name": authority,  # Filing authority.
                "frequency": freq,  # Filing frequency.
                "filing_day": day,  # Day of month due.
            },  # Close the grouped value.
        )  # Close the grouped expression.

    return list(TaxObligation.objects.filter(entity=entity).order_by("code"))  # Return seeded obligations in code order.
