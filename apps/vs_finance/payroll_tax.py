"""PAYE worked out from the national tax table, cumulatively over the tax year.

**One table per tax year.** A payroll month is priced on the
:class:`~vs_finance.models.PayeTaxTable` of the calendar year its payroll date
falls in (:func:`table_for`), and a year with no table refuses to compute: the
platform adds each year's table before the first payroll of that year.

**Cumulative, so a raise is handled.** Month ``m`` of the tax year works out the
tax due on everything earned so far this year, against the annual bands and
reliefs scaled to ``m/12`` of a year, and takes off the PAYE already withheld this
year. A person on N100,000 a month who is raised to N300,000 in July pays, by
December, exactly the tax on what they earned in the year: the months before the
raise are not re-taxed at the new rate, and the months after it are not taxed as
if the whole year had been N100,000. A month in which the year's tax to date is
below what was already withheld deducts nothing (no refund through payroll); the
next month's figure accounts for the excess automatically.

**Reliefs are data.** Each :class:`~vs_finance.models.PayeTaxRelief` of the table
is applied by kind (:class:`~vs_finance.constants.PayeReliefKind`): pension and
NHF contributions are deducted as actually made to date; a percentage relief
(rent relief, or an older regime's consolidated relief) is worked on its basis,
floored and capped as an annual figure, then scaled to ``m/12``.

Every figure here is integer kobo. Intermediate arithmetic is exact
(:class:`fractions.Fraction`); the year's tax to date is rounded half up to the
kobo once, so the twelve monthly deductions always sum to the year's tax.

:func:`compute_paye` returns the amount and the full working, which the payroll
line stores (``tax_basis``) together with the table it used, so the figure can
always be explained and recomputed.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .constants import PayeReliefBasis, PayeReliefKind
from .exceptions import PayrollError


@dataclass
class YearToDate:
    """What a person was paid and withheld earlier in the tax year, before this month."""

    gross: int = 0
    taxable_pay: int = 0
    paye: int = 0
    pension: int = 0
    nhf: int = 0


@dataclass
class PayeResult:
    """This month's PAYE and how it was reached."""

    amount: int
    working: dict


def table_for(country, payroll_date):
    """The active :class:`PayeTaxTable` of ``payroll_date``'s tax year, or a refusal.

    Raises :class:`PayrollError` naming the year when there is none, so a payroll
    is never priced on another year's bands.
    """
    from .models import PayeTaxTable

    table = (
        PayeTaxTable.objects.filter(country=country, tax_year=payroll_date.year, is_active=True)
        .prefetch_related("bands", "reliefs")
        .first()
    )
    if table is None:
        raise PayrollError(
            f"There is no {country} PAYE tax table for {payroll_date.year}, so PAYE for "
            f"this payroll cannot be worked out. The platform adds each year's table; "
            f"until it does, PAYE can be supplied on the salary roster instead.",
        )
    return table


def _round(value: Fraction) -> int:
    """Round a non-negative amount half up to the kobo."""
    if value <= 0:
        return 0
    return int(value + Fraction(1, 2))


def band_tax(income, bands, fraction) -> Fraction:
    """Tax on ``income`` across ``bands`` whose bounds are scaled by ``fraction`` of a year.

    ``bands`` are mappings with ``lower``, ``upper`` (None on the top band) and
    ``rate_bps``.
    """
    income = Fraction(income)
    tax = Fraction(0)
    for band in bands:
        lower = Fraction(band["lower"]) * fraction
        if income <= lower:
            continue
        upper = None if band["upper"] is None else Fraction(band["upper"]) * fraction
        top = income if upper is None else min(income, upper)
        tax += (top - lower) * band["rate_bps"] / 10000
    return tax


def reliefs_to_date(reliefs, *, month, gross_to_date, pension_to_date, nhf_to_date,
                    annual_rent) -> list:
    """Each relief rule's amount for the year to date, as ``[(code, name, Fraction)]``."""
    fraction = Fraction(month, 12)
    contributions = {
        PayeReliefBasis.PENSION: pension_to_date,
        PayeReliefBasis.NHF: nhf_to_date,
    }
    out = []
    for rule in reliefs:
        kind, basis = rule["kind"], rule["basis"]
        if kind == PayeReliefKind.CONTRIBUTION:
            amount = Fraction(contributions.get(basis, 0))
        elif kind == PayeReliefKind.PERCENT_CAPPED:
            if basis == PayeReliefBasis.ANNUAL_RENT:
                annual_basis = Fraction(annual_rent)
            elif basis == PayeReliefBasis.ANNUAL_GROSS:
                annual_basis = Fraction(gross_to_date) * 12 / month
            else:
                annual_basis = Fraction(0)
            annual = annual_basis * rule["rate_bps"] / 10000
            if annual_basis > 0:
                annual = max(annual, Fraction(rule["floor_amount"] or 0))
            if rule["cap_amount"] is not None:
                annual = min(annual, Fraction(rule["cap_amount"]))
            amount = annual * fraction
        elif kind == PayeReliefKind.FIXED:
            amount = Fraction(rule["cap_amount"] or 0) * fraction
        else:
            amount = Fraction(0)
        out.append((rule["code"], rule["name"], max(amount, Fraction(0))))
    return out


def compute_paye(snapshot, *, month, prior, gross_now, taxable_now, pension_now, nhf_now,
                 annual_rent) -> PayeResult:
    """This month's PAYE under the table ``snapshot`` (:meth:`PayeTaxTable.snapshot`).

    ``month`` is the payroll month's number in the tax year (1 to 12); ``prior``
    is a :class:`YearToDate` of the earlier months. Returns the amount to withhold
    this month and the working: income, reliefs and tax to date, what was already
    withheld, and the table it was priced on.
    """
    fraction = Fraction(month, 12)
    gross_to_date = prior.gross + int(gross_now)
    taxable_to_date = prior.taxable_pay + int(taxable_now)
    pension_to_date = prior.pension + int(pension_now)
    nhf_to_date = prior.nhf + int(nhf_now)

    reliefs = reliefs_to_date(
        snapshot["reliefs"], month=month, gross_to_date=gross_to_date,
        pension_to_date=pension_to_date, nhf_to_date=nhf_to_date, annual_rent=int(annual_rent or 0),
    )
    relief_total = sum((amount for _, _, amount in reliefs), Fraction(0))
    chargeable = max(Fraction(taxable_to_date) - relief_total, Fraction(0))

    annualised_gross = Fraction(gross_to_date) * 12 / month
    threshold = snapshot.get("exempt_income_threshold") or 0
    exempt = bool(threshold) and annualised_gross <= threshold
    minimum_applied = False
    if exempt:
        tax_to_date = Fraction(0)
    else:
        tax_to_date = band_tax(chargeable, snapshot["bands"], fraction)
        minimum = Fraction(gross_to_date) * (snapshot.get("minimum_tax_rate_bps") or 0) / 10000
        if tax_to_date < minimum:
            tax_to_date, minimum_applied = minimum, True

    tax_to_date_kobo = _round(tax_to_date)
    amount = max(tax_to_date_kobo - prior.paye, 0)
    working = {
        "method": "cumulative",
        "tax_year": snapshot["tax_year"],
        "month": month,
        "table": {k: snapshot[k] for k in ("id", "country", "tax_year", "revision", "name")},
        "bands": snapshot["bands"],
        "relief_rules": snapshot["reliefs"],
        "minimum_tax_rate_bps": snapshot.get("minimum_tax_rate_bps") or 0,
        "exempt_income_threshold": threshold,
        "inputs": {
            "gross_this_month": int(gross_now),
            "taxable_this_month": int(taxable_now),
            "pension_this_month": int(pension_now),
            "nhf_this_month": int(nhf_now),
            "annual_rent": int(annual_rent or 0),
            "gross_before": prior.gross,
            "taxable_before": prior.taxable_pay,
            "pension_before": prior.pension,
            "nhf_before": prior.nhf,
            "paye_before": prior.paye,
        },
        "reliefs": [
            {"code": code, "name": name, "amount": _round(amount)} for code, name, amount in reliefs
        ],
        "taxable_to_date": taxable_to_date,
        "relief_to_date": _round(relief_total),
        "chargeable_to_date": _round(chargeable),
        "exempt": exempt,
        "minimum_tax_applied": minimum_applied,
        "tax_to_date": tax_to_date_kobo,
        "paye_this_month": amount,
    }
    return PayeResult(amount=amount, working=working)
