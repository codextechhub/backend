"""Receivables accruals: deferred income, the doubtful-debt provision and customer deposits.

Three things the invoice journal alone cannot say:

* **when billed income is earned.** A bill raised before the service it pays for
  is a liability until the service is given. :class:`DeferredIncomeEntry` holds
  each month's share of such a line until a :class:`DeferredIncomeRelease` moves
  it to revenue.
* **how much of what is owed will never be paid.** A
  :class:`DoubtfulDebtProvision` sets the allowance for doubtful debts from the
  age of what is owed, one journal per branch.
* **money held for a customer.** A :class:`CustomerDeposit` is a refundable
  deposit billed to a customer and held in a liability until it is returned, set
  against bills, or forfeited as unclaimed (:class:`DepositForfeiture`).

:class:`FinanceReceivablesPolicy` holds the entity's choices for all three.

A row here that points at the journal it posted does so through a field whose
name contains ``journal``, which makes the row that journal's owner
(:func:`vs_finance.posting._journal_document_owner`), so the journal cannot be
reversed by hand behind the service that posted it. A row that only records
which adjusting journal touched it (:class:`DeferredIncomeUnwind`) names that
journal ``adjustment_entry`` for the opposite reason: the adjusting journal
belongs to its credit note, concession or write-off, and must stay so.
"""
from __future__ import annotations

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from ..constants import (
    DeferredIncomeStatus,
    DepositStatus,
    DocType,
    RevenueRecognitionMethod,
)
from ..money import MoneyField
from .core import FinanceDocument, LedgerEntity, TimeStampedModel
from .gl import Account, CostCenter


def default_provision_bands():
    """The provision rates an entity starts with: 25% past 180 days, 50% past 365, 100% past 730."""
    return [
        {"over_days": 180, "rate_bps": 2500},
        {"over_days": 365, "rate_bps": 5000},
        {"over_days": 730, "rate_bps": 10000},
    ]


class FinanceReceivablesPolicy(TimeStampedModel):
    """An entity's rules for recognising, providing for and holding customer money.

    One row per set of books, changed only by a caller whose reach is the whole
    tenant: every branch posts to these books, so a rule set for one would bind the
    others. An entity with no row uses the model defaults.

    ``revenue_recognition`` applies to lines posted after it changes; a line
    already scheduled keeps the schedule it was given. ``provision_bands`` is a
    list of ``{"over_days", "rate_bps"}``: a balance more than ``over_days`` past
    due is provided for at ``rate_bps`` basis points, the highest band it passes
    deciding. ``deposits_offset_unpaid_bills`` lets a departing customer's deposit
    settle their unpaid bills before the rest is returned; off, the deposit is only
    ever returned. ``unclaimed_deposit_years`` is how long after a customer leaves
    an unclaimed deposit is held before it may be taken to income.
    """

    entity = models.OneToOneField(
        LedgerEntity, on_delete=models.CASCADE, related_name="finance_receivables_policy",
    )
    revenue_recognition = models.CharField(
        max_length=16, choices=RevenueRecognitionMethod.choices,
        default=RevenueRecognitionMethod.SPREAD_MONTHLY,
    )
    provision_bands = models.JSONField(default=default_provision_bands, blank=True)
    deposits_offset_unpaid_bills = models.BooleanField(default=False)
    unclaimed_deposit_years = models.PositiveSmallIntegerField(
        default=6, validators=[MinValueValidator(1), MaxValueValidator(50)],
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_receivables_policy_updates", null=True, blank=True,
    )

    def __str__(self) -> str:
        return f"Finance receivables policy for {self.entity_id}"


class DeferredIncomeRelease(TimeStampedModel):
    """One journal that moved one branch's due deferred income to revenue.

    ``Dr deferred income, Cr revenue`` (per revenue account and cost centre), dated
    in the month the income belongs to. A release is undone only as a whole, by
    :func:`vs_finance.deferred_income.reverse_deferred_release`, while its period
    is still open; its entries then wait to be released again.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="deferred_income_releases",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="deferred_income_releases", null=True, blank=True,
    )
    journal = models.OneToOneField(
        "JournalEntry", on_delete=models.PROTECT, related_name="deferred_income_release",
    )
    amount = MoneyField(help_text="Deferred income moved to revenue, in kobo.")
    reversed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="deferred_income_releases", null=True, blank=True,
    )

    class Meta:
        indexes = [models.Index(fields=["entity", "reversed_at"])]
        ordering = ["entity", "journal_id"]


class DeferredIncomeEntry(TimeStampedModel):
    """One month's share of an invoice line billed before its service period.

    Written when the invoice posts. ``recognition_date`` is the day the share
    becomes revenue: the service start in the first month, the first of the month
    after that. ``unwound_amount`` is what credit notes, concessions and write-offs
    of the bill have taken back out before release (:class:`DeferredIncomeUnwind`);
    only ``amount - unwound_amount`` is ever released.

    ``void_journal`` is the journal that took released revenue back out when the
    invoice was voided, so that journal belongs to this schedule and cannot be
    reversed by hand.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="deferred_income_entries",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="deferred_income_entries", null=True, blank=True,
    )
    invoice = models.ForeignKey(
        "Invoice", on_delete=models.PROTECT, related_name="deferred_income_entries",
    )
    line = models.ForeignKey(
        "InvoiceLine", on_delete=models.PROTECT, related_name="deferred_income_entries",
    )
    revenue_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="deferred_income_entries",
    )
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="deferred_income_entries",
        null=True, blank=True,
    )
    recognition_date = models.DateField()
    amount = MoneyField(help_text="This month's share, in kobo.")
    unwound_amount = MoneyField(help_text="Taken back out before release, in kobo.")
    released_amount = MoneyField(help_text="Moved to revenue by its release, in kobo.")
    status = models.CharField(
        max_length=12, choices=DeferredIncomeStatus.choices,
        default=DeferredIncomeStatus.PENDING,
    )
    release = models.ForeignKey(
        DeferredIncomeRelease, on_delete=models.PROTECT, related_name="entries",
        null=True, blank=True,
    )
    void_journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="deferred_income_voids",
        null=True, blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["entity", "status", "recognition_date"]),
            models.Index(fields=["invoice", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=(models.Q(unwound_amount__gte=0)
                       & models.Q(amount__gte=models.F("unwound_amount"))),
                name="ck_finance_deferred_unwound_within_amount",
            ),
        ]
        ordering = ["invoice", "recognition_date", "id"]

    @property
    def open_amount(self) -> int:
        """What this share still has to release, in kobo."""
        return int(self.amount) - int(self.unwound_amount)


class DeferredIncomeUnwind(TimeStampedModel):
    """Deferred income a credit note, concession or write-off took back before release.

    ``adjustment_entry`` is the adjusting document's own journal, which debited the
    deferred-income liability for ``amount`` instead of revenue. Voiding that
    document reverses its journal and restores the amount to the schedule
    (``restored``), so the income is released again when its month comes.
    """

    entry = models.ForeignKey(
        DeferredIncomeEntry, on_delete=models.PROTECT, related_name="unwinds",
    )
    adjustment_entry = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="deferred_income_unwinds",
    )
    amount = MoneyField(help_text="Deferred income taken back, in kobo.")
    restored = models.BooleanField(default=False)

    class Meta:
        indexes = [models.Index(fields=["adjustment_entry", "restored"])]
        ordering = ["entry", "id"]


class DoubtfulDebtProvision(FinanceDocument):
    """A run that sets the allowance for doubtful debts from what is owed and how old it is.

    Raised by a caller whose reach is the whole tenant, for every branch at once, so
    it names no branch itself: posting writes one :class:`DoubtfulDebtProvisionLine`
    and one journal per branch, each naming its branch. The figures are worked out
    again when it posts (``as_of`` fixes the ageing date), because receipts and
    write-offs made while it waited for approval change what the allowance must be.
    """

    DOC_TYPE = DocType.DOUBTFUL_DEBT_PROVISION

    #: Approval-gate identity, routed like the other receivable adjustments.
    workflow_document_type = "finance.doubtful_debt_provision"
    #: The field a threshold-gated stage reads to decide whether it applies.
    workflow_amount_field = "movement_total"

    as_of = models.DateField(help_text="The date balances are aged to and the journals are dated.")
    narration = models.CharField(max_length=255, blank=True, default="")
    required_total = MoneyField(help_text="Allowance the policy requires across branches, in kobo.")
    movement_total = MoneyField(
        help_text="Absolute change the run makes to the allowance across branches, in kobo.",
    )
    policy_snapshot = models.JSONField(
        default=list, blank=True,
        help_text="The provision bands the figures were worked out with.",
    )

    class Meta(FinanceDocument.Meta):
        indexes = [models.Index(fields=["entity", "status"])]


class DoubtfulDebtProvisionLine(TimeStampedModel):
    """One branch's share of a :class:`DoubtfulDebtProvision`, and the journal it posted.

    ``movement`` is ``required - current``: positive raises the allowance
    (``Dr bad debts, Cr allowance``), negative releases it (the reverse).
    """

    provision = models.ForeignKey(
        DoubtfulDebtProvision, on_delete=models.CASCADE, related_name="lines",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="doubtful_debt_provision_lines", null=True, blank=True,
    )
    required = MoneyField(help_text="Allowance required for this branch, in kobo.")
    current = MoneyField(help_text="Allowance already held for this branch, in kobo.")
    movement = MoneyField(help_text="Change posted to the allowance, in kobo.")
    bands = models.JSONField(
        default=dict, blank=True,
        help_text="Overdue balance and required allowance per band, in kobo.",
    )
    journal = models.OneToOneField(
        "JournalEntry", on_delete=models.PROTECT, related_name="doubtful_debt_provision_line",
        null=True, blank=True,
    )

    class Meta:
        ordering = ["provision", "branch_id", "id"]


class WriteOffRecovery(TimeStampedModel):
    """A written-off debt paid after all: the write-off reinstated and the receipt applied.

    ``journal`` reinstates the receivable (``Dr AR, Cr bad debts recovered``) on
    the receipt's date; the receipt's credit is then applied to the bill in the
    ordinary way (``Dr customer credit, Cr AR``), so the net effect is recovery
    income for money actually received. Voiding the receipt reverses the
    reinstatement too and the debt is written off again.
    """

    write_off = models.ForeignKey(
        "WriteOffRequest", on_delete=models.PROTECT, related_name="recoveries",
    )
    payment = models.ForeignKey(
        "Payment", on_delete=models.PROTECT, related_name="write_off_recoveries",
    )
    amount = MoneyField(help_text="Debt reinstated and paid, in kobo.")
    journal = models.OneToOneField(
        "JournalEntry", on_delete=models.PROTECT, related_name="write_off_recovery",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="write_off_recoveries", null=True, blank=True,
    )

    class Meta:
        indexes = [models.Index(fields=["write_off", "reversed_at"])]
        ordering = ["write_off", "id"]


class DepositForfeiture(TimeStampedModel):
    """One branch's unclaimed deposits taken to income by one run.

    ``Dr deposits held, Cr forfeited deposits income``, dated the run's day.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="deposit_forfeitures",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="deposit_forfeitures", null=True, blank=True,
    )
    journal = models.OneToOneField(
        "JournalEntry", on_delete=models.PROTECT, related_name="deposit_forfeiture",
    )
    amount = MoneyField(help_text="Deposits forfeited, in kobo.")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="deposit_forfeitures", null=True, blank=True,
    )

    class Meta:
        ordering = ["entity", "id"]


class CustomerDeposit(TimeStampedModel):
    """A refundable deposit billed to one customer, and where it stands.

    Opened when the invoice line that billed it posts. ``claim_opened_on`` is the
    day the customer left: from then the deposit is theirs to claim, and the
    unclaimed-deposit clock runs from it. A deposit leaves ``HELD`` by being
    released (``release_note``: a credit note that debits the deposits-held
    liability and settles the deposit's own unpaid bill, then the customer's other
    bills where the entity allows it, leaving the rest as refundable credit), by
    being forfeited (``forfeiture``), or by its invoice being voided.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="customer_deposits",
    )
    branch = models.ForeignKey(
        "vs_tenants.Branch", on_delete=models.PROTECT,
        related_name="customer_deposits", null=True, blank=True,
    )
    customer = models.ForeignKey(
        "Customer", on_delete=models.PROTECT, related_name="deposits",
    )
    invoice = models.ForeignKey(
        "Invoice", on_delete=models.PROTECT, related_name="customer_deposits",
    )
    line = models.OneToOneField(
        "InvoiceLine", on_delete=models.PROTECT, related_name="customer_deposit",
    )
    amount = MoneyField(help_text="Deposit billed, in kobo.")
    status = models.CharField(
        max_length=12, choices=DepositStatus.choices, default=DepositStatus.HELD,
    )
    claim_opened_on = models.DateField(null=True, blank=True)
    release_note = models.ForeignKey(
        "CreditNote", on_delete=models.PROTECT, related_name="released_deposits",
        null=True, blank=True,
    )
    forfeiture = models.ForeignKey(
        DepositForfeiture, on_delete=models.PROTECT, related_name="deposits",
        null=True, blank=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["customer", "status"]),
        ]
        ordering = ["customer", "id"]
