"""Statutory payroll: national tax tables, the tenant's payroll policy, and what a line carries.

Two kinds of data live here, kept apart on purpose:

* **National data, maintained by the platform.** The PAYE tax table of each tax
  year (bands and reliefs), the states whose revenue services PAYE is remitted
  to, and the licensed pension fund administrators. These are the law and the
  regulator's register, not a tenant's choice, so no tenant can edit them: only
  platform staff can, through platform-scoped endpoints. They carry no entity.
* **Tenant data.** The tenant's payroll policy (:class:`FinancePayrollSettings`),
  each employee's salary history (:class:`EmployeeSalaryVersion`), the
  voluntary deductions a tenant defines and assigns, the deductions and
  contributions on each payroll line (:class:`PayrollLineItem`), and the
  payslips issued from paid runs.

Nothing here names a school or a staff record: an employee is a salary row and,
where known, a user account, as everywhere else in this app.
"""
from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator
from django.db import models

from ..constants import (
    PayeMethod,
    PayeReliefBasis,
    PayeReliefKind,
    PayrollItemCode,
    PayrollItemKind,
    PayslipEmailStatus,
    PayBroughtForwardSource,
)
from ..money import MoneyField, format_naira
from .core import LedgerEntity, TimeStampedModel
from .gl import Account, CostCenter
from .ops import (
    EmployeeSalary,
    PayrollLine,
    PayrollRun,
    SalaryStructure,
)

__all__ = [
    "PayrollTaxJurisdiction",
    "PayeTaxTable",
    "PayeTaxBand",
    "PayeTaxRelief",
    "PensionFundAdministrator",
    "FinancePayrollSettings",
    "EmployeeSalaryVersion",
    "PayrollDeductionType",
    "EmployeeDeduction",
    "PayrollLineItem",
    "Payslip",
    "PayBroughtForward",
]


# --------------------------------------------------------------------------- #
# National data (platform-maintained)                                          #
# --------------------------------------------------------------------------- #

class PayrollTaxJurisdiction(TimeStampedModel):
    """A state (or territory) whose revenue service PAYE is remitted to.

    PAYE is due to the state where the employee lives, so each jurisdiction
    names its authority. ``code`` is short and stable ("LA" for Lagos) because
    it becomes part of the per-state PAYE account and obligation codes a tenant's
    books carry. Platform data: maintained by platform staff only.
    """

    country = models.CharField(max_length=2, default="NG")
    code = models.CharField(max_length=8)
    name = models.CharField(max_length=80)
    authority_name = models.CharField(max_length=160)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["country", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["country", "code"], name="uniq_finance_tax_jurisdiction_code",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class PayeTaxTable(TimeStampedModel):
    """One tax year's PAYE rules for one country, as data.

    The bands (:class:`PayeTaxBand`) and reliefs (:class:`PayeTaxRelief`) are
    annual figures. A payroll month is priced on the table of its own tax year
    (the calendar year of its payroll date) and nothing else, so a month in 2027
    is never priced on 2026's bands, and a year with no table refuses to compute
    rather than borrowing another year's.

    ``minimum_tax_rate_bps`` charges at least that share of gross income where a
    regime has a minimum tax (zero where it has none). ``exempt_income_threshold``
    charges nothing to a person whose annual gross is at or below it (zero where
    the law has no such exemption).

    ``revision`` counts the edits. Each payroll line stores the bands and reliefs
    it was priced on, so an edit never changes what a past line says it used.
    Platform data: maintained by platform staff only.
    """

    country = models.CharField(max_length=2, default="NG")
    tax_year = models.PositiveSmallIntegerField()
    name = models.CharField(max_length=160)
    source_reference = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    minimum_tax_rate_bps = models.PositiveIntegerField(
        default=0, validators=[MaxValueValidator(10000)],
    )
    exempt_income_threshold = MoneyField(
        help_text="Annual gross at or below which no PAYE is charged, in kobo.",
    )
    revision = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="paye_tax_tables_created", null=True, blank=True,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="paye_tax_tables_updated", null=True, blank=True,
    )

    class Meta:
        ordering = ["country", "-tax_year"]
        constraints = [
            models.UniqueConstraint(
                fields=["country", "tax_year"], name="uniq_finance_paye_table_year",
            ),
        ]

    def snapshot(self) -> dict:
        """The table as a payroll line records it: enough to price the month again."""
        return {
            "id": self.pk, "country": self.country, "tax_year": self.tax_year,
            "revision": self.revision, "name": self.name,
            "minimum_tax_rate_bps": self.minimum_tax_rate_bps,
            "exempt_income_threshold": self.exempt_income_threshold,
            "bands": [
                {"lower": b.lower, "upper": b.upper, "rate_bps": b.rate_bps}
                for b in self.bands.all()
            ],
            "reliefs": [
                {
                    "code": r.code, "name": r.name, "kind": r.kind, "basis": r.basis,
                    "rate_bps": r.rate_bps, "cap_amount": r.cap_amount,
                    "floor_amount": r.floor_amount,
                }
                for r in self.reliefs.all()
            ],
        }

    def __str__(self) -> str:
        return f"{self.country} PAYE {self.tax_year}"


class PayeTaxBand(TimeStampedModel):
    """One annual band of a :class:`PayeTaxTable`: income from ``lower`` to ``upper`` at ``rate_bps``.

    ``upper`` is empty on the top band. Bands are contiguous: each starts where
    the one before it ends.
    """

    table = models.ForeignKey(PayeTaxTable, on_delete=models.CASCADE, related_name="bands")
    sequence = models.PositiveSmallIntegerField(default=0)
    lower = MoneyField(help_text="Annual income where the band starts, in kobo.")
    upper = MoneyField(null=True, default=None, help_text="Where it ends; empty on the top band.")
    rate_bps = models.PositiveIntegerField(validators=[MaxValueValidator(10000)])

    class Meta:
        ordering = ["table", "sequence", "lower"]

    def __str__(self) -> str:
        return f"{self.lower}-{self.upper or ''} @ {self.rate_bps}"


class PayeTaxRelief(TimeStampedModel):
    """One relief rule of a :class:`PayeTaxTable`, as data (see :class:`PayeReliefKind`).

    For example: pension contributions deducted in full (CONTRIBUTION of
    PENSION), or rent relief at 20% of annual rent capped at N500,000
    (PERCENT_CAPPED of ANNUAL_RENT, 2000 bps, cap 500,000.00). Amounts are
    annual and pro-rated over the months of the year elapsed.
    """

    table = models.ForeignKey(PayeTaxTable, on_delete=models.CASCADE, related_name="reliefs")
    sequence = models.PositiveSmallIntegerField(default=0)
    code = models.CharField(max_length=24)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=16, choices=PayeReliefKind.choices)
    basis = models.CharField(
        max_length=16, choices=PayeReliefBasis.choices, default=PayeReliefBasis.NONE,
    )
    rate_bps = models.PositiveIntegerField(default=0, validators=[MaxValueValidator(10000)])
    cap_amount = MoneyField(null=True, default=None, help_text="Annual cap, in kobo; empty for none.")
    floor_amount = MoneyField(help_text="Annual floor, in kobo.")

    class Meta:
        ordering = ["table", "sequence", "id"]

    def __str__(self) -> str:
        return f"{self.code} ({self.kind})"


class PensionFundAdministrator(TimeStampedModel):
    """A licensed pension fund administrator employees' pension is remitted to.

    ``code`` is short and stable because it becomes part of the per-PFA pension
    account and obligation codes in a tenant's books. Platform data: maintained
    by platform staff only, against the regulator's current list.
    """

    code = models.CharField(max_length=12, unique=True)
    name = models.CharField(max_length=160)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


# --------------------------------------------------------------------------- #
# Tenant policy                                                               #
# --------------------------------------------------------------------------- #

class FinancePayrollSettings(TimeStampedModel):
    """A tenant's payroll policy for one set of books, every choice with a default.

    * ``paye_method``: PAYE and employee pension computed (the default), or
      supplied by the tenant's structures and roster figures.
    * Each statutory deduction and employer contribution can be switched off and
      its rate changed: employee pension (8% of pensionable pay), employer
      pension (10% of pensionable pay), NHF (2.5% of basic, from the employee),
      NSITF (1% of gross, from the employer) and ITF (1% of gross, from the
      employer).
    * Payslip delivery: shown in the app to the employee, and emailed to them;
      either can be turned off.
    * ``previous_pay_required``: whether somebody whose first month on these
      books in a tax year is after January must have their earlier pay that
      year recorded (:class:`PayBroughtForward`) before a run pays them. Off
      by default: such people are listed as a warning instead.
    * ``payroll_moved_here_on``: for a tenant that ran payroll somewhere else
      earlier in the tax year, a day in the first payroll month it ran here.
      Bright Star moves its payroll onto these books in June 2026 with 40
      staff; every one of them is first paid here in June, exactly like a
      teacher hired in June from another school. Only the tenant knows which
      it is: a tenant that opened in June has only new joiners, one that moved
      its payroll has its own staff carried over. So it is said here, and a
      person first paid in that month is not taken for a mid-year joiner.
      Empty by default: nobody is carried over.

    The rates are the tenant's because whether a levy applies, and at what rate,
    can turn on the tenant's own circumstances (how many staff, an exemption, a
    negotiated pension scheme). The tax bands are not here: they are national law
    and live in :class:`PayeTaxTable`.
    """

    entity = models.OneToOneField(
        LedgerEntity, on_delete=models.CASCADE, related_name="finance_payroll_settings",
    )
    paye_method = models.CharField(
        max_length=10, choices=PayeMethod.choices, default=PayeMethod.COMPUTED,
    )
    tax_country = models.CharField(max_length=2, default="NG")
    employee_pension_enabled = models.BooleanField(default=True)
    employee_pension_rate_bps = models.PositiveIntegerField(
        default=800, validators=[MaxValueValidator(10000)],
    )
    employer_pension_enabled = models.BooleanField(default=True)
    employer_pension_rate_bps = models.PositiveIntegerField(
        default=1000, validators=[MaxValueValidator(10000)],
    )
    nhf_enabled = models.BooleanField(default=True)
    nhf_rate_bps = models.PositiveIntegerField(default=250, validators=[MaxValueValidator(10000)])
    nsitf_enabled = models.BooleanField(default=True)
    nsitf_rate_bps = models.PositiveIntegerField(default=100, validators=[MaxValueValidator(10000)])
    itf_enabled = models.BooleanField(default=True)
    itf_rate_bps = models.PositiveIntegerField(default=100, validators=[MaxValueValidator(10000)])
    payslip_in_app = models.BooleanField(default=True)
    payslip_email = models.BooleanField(default=True)
    previous_pay_required = models.BooleanField(
        default=False,
        help_text="Refuse to pay somebody first paid after January of a tax year until "
                  "their earlier pay that year is recorded (zeros where there was none).",
    )
    payroll_moved_here_on = models.DateField(
        null=True, blank=True,
        help_text="For a tenant that ran payroll elsewhere earlier in a tax year: a day in "
                  "the first payroll month run on these books. Staff first paid in that "
                  "month are its own staff carried over, not mid-year joiners.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_payroll_settings_updates", null=True, blank=True,
    )

    def __str__(self) -> str:
        return f"Finance payroll settings for {self.entity_id}"


# --------------------------------------------------------------------------- #
# Salary history                                                              #
# --------------------------------------------------------------------------- #

class EmployeeSalaryVersion(TimeStampedModel):
    """One dated version of a person's pay terms.

    Effective from ``effective_from`` until the next version. An edit to the
    terms writes a new version rather than changing an old one, so the terms of
    any past payroll month can be read back and the month recomputed. Two
    versions on the same day are both kept; the later written is in force.
    """

    salary = models.ForeignKey(EmployeeSalary, on_delete=models.CASCADE, related_name="versions")
    effective_from = models.DateField()
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="finance_salary_versions", null=True, blank=True,
    )
    structure = models.ForeignKey(
        SalaryStructure, on_delete=models.PROTECT, related_name="salary_versions",
        null=True, blank=True,
    )
    gross_amount = MoneyField()
    paye_amount = MoneyField()
    pension_amount = MoneyField()
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="salary_versions",
        null=True, blank=True,
    )
    residence_state = models.ForeignKey(
        PayrollTaxJurisdiction, on_delete=models.PROTECT, related_name="salary_versions",
        null=True, blank=True,
    )
    reason = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_salary_versions", null=True, blank=True,
    )

    class Meta:
        ordering = ["salary", "effective_from", "id"]
        indexes = [models.Index(fields=["salary", "effective_from"])]

    def __str__(self) -> str:
        return f"{self.salary_id} from {self.effective_from}: gross {format_naira(int(self.gross_amount))}"


class PayBroughtForward(TimeStampedModel):
    """A person's pay and tax for months of a tax year that no run on these books paid.

    PAYE is cumulative over the tax year: each month taxes everything earned
    since 1 January against the bands accrued since then, and takes off the
    tax already deducted (:func:`vs_finance.payroll_tax.compute_paye`). Months
    missing from the books shelter the pay of the months that follow behind
    bands accrued since January, so the person is under-taxed every month to
    December. Two kinds of month are missing (``source``,
    :class:`~vs_finance.constants.PayBroughtForwardSource`):

    * **A previous employer's.** Aisha joins in April with January to March
      earned and taxed at another employer, copied from its tax deduction
      card. That employer reports them, so they appear in no return or year to
      date of this one.
    * **This employer's own, before its payroll ran here.** Bright Star runs
      payroll on these books from June; its January to May ran elsewhere. Those
      months are its own pay: they belong in its year to date and its annual
      return, and in no monthly remittance schedule, because they were remitted
      from wherever payroll ran then. Optional: nothing requires them.

    At most one row of each kind per person per tax year, and every run of that
    year counts both (:func:`vs_finance.payroll_statutory.pay_brought_forward_for`).

    ``taxable_pay`` is the taxable part **before reliefs**, with the pension
    and NHF contributions made beside it, because the reliefs are worked once
    over the whole year to date here: the pension and NHF reliefs from the
    contributions actually made, and a percentage relief (rent relief, or an
    older regime's consolidated relief) on the whole year's gross or rent. A
    figure already net of reliefs would have those months' rent relief given
    twice.

    The row belongs to the salary record and follows its owning branch on
    every read and write (:meth:`EmployeeSalary.branch_on`); one active record
    per person keeps it to one person's year. Every figure is a pay figure
    under Field Access (:mod:`vs_finance.field_access`). A previous-employer
    row of zeros states that the person had no earlier taxable pay that year,
    which answers the tenant's ``previous_pay_required`` setting.
    ``employer_name`` names the previous employer, and is blank on a row of
    this employer's own.
    """

    salary = models.ForeignKey(
        EmployeeSalary, on_delete=models.CASCADE, related_name="pay_brought_forward",
    )
    tax_year = models.PositiveSmallIntegerField()
    source = models.CharField(
        max_length=20, choices=PayBroughtForwardSource.choices,
        default=PayBroughtForwardSource.PREVIOUS_EMPLOYER,
    )
    employer_name = models.CharField(max_length=160, blank=True, default="")
    evidence_reference = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Where the figures come from: a previous employer's tax card number, or a note.",
    )
    gross_amount = MoneyField(help_text="Gross pay of the months brought forward, in kobo.")
    taxable_pay = MoneyField(help_text="Its taxable part before reliefs, in kobo.")
    paye_amount = MoneyField(help_text="PAYE deducted in those months, in kobo.")
    pension_amount = MoneyField(help_text="Employee pension deducted in those months, in kobo.")
    nhf_amount = MoneyField(help_text="NHF deducted in those months, in kobo.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_pay_brought_forward_created", null=True, blank=True,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_pay_brought_forward_updated", null=True, blank=True,
    )

    class Meta:
        ordering = ["salary", "-tax_year", "source"]
        constraints = [
            models.UniqueConstraint(
                fields=["salary", "tax_year", "source"],
                name="uniq_finance_pay_brought_forward",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.salary_id} {self.tax_year} {self.source}: {format_naira(int(self.taxable_pay))}"


# --------------------------------------------------------------------------- #
# Voluntary deductions                                                        #
# --------------------------------------------------------------------------- #

class PayrollDeductionType(TimeStampedModel):
    """A deduction the tenant defines: a staff loan, cooperative savings, a union due.

    Withheld from net pay and credited to its own ``liability_account`` until the
    tenant pays it over (to the cooperative) or it clears (a loan the tenant made
    is recovered against the loan receivable it names). Shared configuration of
    the books: no branch.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="payroll_deduction_types",
    )
    code = models.CharField(max_length=24)
    name = models.CharField(max_length=120)
    liability_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_deduction_types",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["entity", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_finance_payroll_deduction_code",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code}: {self.name}"


class EmployeeDeduction(TimeStampedModel):
    """One person's voluntary deduction: so much a month, between two dates.

    ``total_limit`` stops the deduction once that much has been withheld on runs
    that were not cancelled (a loan of N120,000 at N10,000 a month stops after
    twelve months). Empty dates mean open-ended.
    """

    salary = models.ForeignKey(EmployeeSalary, on_delete=models.CASCADE, related_name="deductions")
    deduction_type = models.ForeignKey(
        PayrollDeductionType, on_delete=models.PROTECT, related_name="assignments",
    )
    amount = MoneyField(help_text="Withheld each payroll month, in kobo.")
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    total_limit = MoneyField(null=True, default=None, help_text="Stop once this much is withheld.")
    reference = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_employee_deductions", null=True, blank=True,
    )

    class Meta:
        ordering = ["salary", "deduction_type__code", "id"]
        indexes = [models.Index(fields=["salary", "is_active"])]

    def __str__(self) -> str:
        return f"{self.salary_id}: {self.deduction_type_id} {format_naira(int(self.amount))}"


# --------------------------------------------------------------------------- #
# What a line carries                                                         #
# --------------------------------------------------------------------------- #

class PayrollLineItem(TimeStampedModel):
    """One deduction from, or employer contribution on, a payroll line.

    ``code`` decides the accounts at posting: PAYE goes to the payable account
    of the line's tax state, employee and employer pension to that of the line's
    PFA, NHF, NSITF and ITF to their own payables (NSITF, ITF and employer
    pension also debit their own expense), and a voluntary deduction to its
    type's liability account. The accounts are stamped on the item when the run
    posts, which is what lets a remittance schedule list each person's share of
    a return. ``basis_amount`` and ``rate_bps`` record what a percentage was
    taken of.
    """

    line = models.ForeignKey(PayrollLine, on_delete=models.CASCADE, related_name="items")
    kind = models.CharField(max_length=10, choices=PayrollItemKind.choices)
    code = models.CharField(max_length=16, choices=PayrollItemCode.choices)
    label = models.CharField(max_length=120, blank=True, default="")
    amount = MoneyField()
    basis_amount = MoneyField()
    rate_bps = models.PositiveIntegerField(default=0)
    deduction_type = models.ForeignKey(
        PayrollDeductionType, on_delete=models.PROTECT, related_name="line_items",
        null=True, blank=True,
    )
    employee_deduction = models.ForeignKey(
        EmployeeDeduction, on_delete=models.PROTECT, related_name="line_items",
        null=True, blank=True,
    )
    liability_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_item_liabilities",
        null=True, blank=True,
    )
    expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_item_expenses",
        null=True, blank=True,
    )

    class Meta:
        ordering = ["line", "kind", "id"]
        indexes = [
            models.Index(fields=["line"]),
            models.Index(fields=["liability_account"]),
        ]

    def __str__(self) -> str:
        return f"{self.line_id} {self.code}: {format_naira(int(self.amount))}"


def payslip_attachment_path(payslip) -> str:
    """Where a payslip's emailed PDF is kept, grouped by tenant and run."""
    return (
        f"finance/payslips/{payslip.entity.tenant_id}/{payslip.entity_id}/"
        f"{payslip.run_id}/payslip-{payslip.pk}.pdf"
    )


class Payslip(TimeStampedModel):
    """The payslip one person receives for one paid payroll line.

    Issued when the line's pay leaves the bank. The PDF is rendered from the
    line whenever it is opened, so it always says what the books say; the copy
    attached to an email is kept under ``email_attachment`` (a storage key, not
    a file field, because it is never served from storage: the payslip
    endpoints render it). Who may open it is decided by the endpoints: the
    employee it is about, through their own payslips, or a payroll reader who
    may see every pay figure.
    """

    line = models.OneToOneField(PayrollLine, on_delete=models.PROTECT, related_name="payslip")
    entity = models.ForeignKey(LedgerEntity, on_delete=models.PROTECT, related_name="payslips")
    run = models.ForeignKey(PayrollRun, on_delete=models.PROTECT, related_name="payslips")
    salary = models.ForeignKey(
        EmployeeSalary, on_delete=models.PROTECT, related_name="payslips",
        null=True, blank=True,
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="finance_payslips",
        null=True, blank=True,
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT, related_name="finance_payslips",
        null=True, blank=True,
    )
    pay_date = models.DateField()
    period_label = models.CharField(max_length=40, blank=True, default="")
    issued_at = models.DateTimeField(auto_now_add=True)
    email_status = models.CharField(
        max_length=16, choices=PayslipEmailStatus.choices,
        default=PayslipEmailStatus.NOT_REQUESTED,
    )
    email_attachment = models.CharField(max_length=255, blank=True, default="")
    notification_ids = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-pay_date", "-id"]
        indexes = [
            models.Index(fields=["employee", "pay_date"]),
            models.Index(fields=["entity", "run"]),
        ]

    def __str__(self) -> str:
        return f"Payslip {self.pk} for line {self.line_id}"
