"""General ledger: currencies, accounts, periods, journals, balances, audit log.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

from ..constants import (
    AccountType,
    DocType,
    DocumentStatus,
    FinanceAuditAction,
    FinanceAuditStatus,
    IFRSLine,
    JournalSource,
    NORMAL_BALANCE_BY_TYPE,
    NormalBalance,
    PeriodStatus,
)
from ..money import MoneyField
from .core import TimeStampedModel, LedgerEntity, FinanceDocument

# ---------------------------------------------------------------------------
# Phase 1 — General Ledger core
# ---------------------------------------------------------------------------
#
# Reference data (Currency, FxRate) is **global** — a naira is a naira regardless of
# whose books it sits in — while everything that records value or structure (Account,
# fiscal calendar, tax codes, analytical dimensions, journals, balances) is scoped to
# a :class:`LedgerEntity`. The entity is the tenant; never a School.


class Currency(TimeStampedModel):
    """An ISO-4217 currency and how its minor units work.

    Global reference data, shared across every entity. ``minor_unit`` is the number
    of decimal places (2 for NGN/USD, 0 for JPY); ``MoneyField`` columns always store
    integer minor units, so this tells the boundary how many of them make one major
    unit. The platform's primary ledger currency is NGN.
    """

    code = models.CharField(
        max_length=3, primary_key=True,
        help_text="ISO 4217 alphabetic code, e.g. NGN, USD.",
    )
    name = models.CharField(max_length=60)
    symbol = models.CharField(max_length=8, default="")
    minor_unit = models.PositiveSmallIntegerField(
        default=2, help_text="Decimal places; 2 for NGN, 0 for JPY.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "currencies"
        ordering = ["code"]

    def __str__(self) -> str:
        return self.code


class FxRate(TimeStampedModel):
    """A spot exchange rate from one currency to another on a given date.

    Stored as a high-precision ``Decimal`` (a *rate*, not money — money never leaves
    integer minor units). ``rate`` means: 1 unit of ``base`` = ``rate`` units of
    ``quote``. Global reference data; sourced from CBN/ECB feeds in a later phase.
    """

    base = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="fx_rates_from",
    )
    quote = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="fx_rates_to",
    )
    rate = models.DecimalField(
        max_digits=20, decimal_places=10,
        help_text="1 unit of base = <rate> units of quote.",
    )
    as_of = models.DateField()
    source = models.CharField(max_length=32, default="", help_text="Feed/source, e.g. CBN, ECB.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["base", "quote", "as_of", "source"],
                name="uniq_finance_fxrate",
            ),
        ]
        indexes = [models.Index(fields=["base", "quote", "as_of"])]
        ordering = ["-as_of"]

    def __str__(self) -> str:
        return f"{self.base_id}/{self.quote_id} {self.rate} @ {self.as_of}"


class AccountManager(models.Manager):
    def postable(self):
        """Accounts that may receive postings (active leaves, not headers)."""
        return self.filter(is_active=True, is_postable=True)


class Account(TimeStampedModel):
    """A node in an entity's Chart of Accounts (a self-referential tree).

    Header accounts (``is_postable=False``) give the CoA its structure and roll-up
    totals; only **leaf**, postable accounts take journal lines. Each account has an
    :class:`~vs_finance.constants.AccountType` root and a :class:`NormalBalance`
    derived from it — flipped when ``is_contra`` is set (accumulated depreciation,
    sales returns …). ``code`` is unique within the entity, so two entities may both
    run a ``1000`` cash account without collision.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="accounts",
    )
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="children",
        null=True, blank=True,
    )
    code = models.CharField(max_length=32, help_text="CoA code, unique within the entity, e.g. 1000.")
    name = models.CharField(max_length=160)
    account_type = models.CharField(max_length=12, choices=AccountType.choices)
    normal_balance = models.CharField(max_length=6, choices=NormalBalance.choices)
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="accounts",
        null=True, blank=True,
        help_text="Leave null to use the entity's base currency.",
    )
    is_contra = models.BooleanField(
        default=False,
        help_text="Carries the opposite of its type's natural balance (e.g. accumulated depreciation).",
    )
    is_postable = models.BooleanField(
        default=True,
        help_text="Leaf accounts accept postings; header accounts (False) only aggregate.",
    )
    is_active = models.BooleanField(default=True)
    subtype = models.CharField(
        max_length=40, blank=True, default="",
        help_text="Optional sub-classification shown in the chart (e.g. 'Current asset', 'Operating revenue').",
    )
    description = models.TextField(blank=True, default="")
    ifrs_line = models.CharField(
        max_length=32, choices=IFRSLine.choices, blank=True, default="",
        help_text="IFRS-for-SMEs statutory presentation line; blank falls back to the type default.",
    )

    objects = AccountManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_finance_account_entity_code",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "account_type"]),
            models.Index(fields=["entity", "is_postable", "is_active"]),
        ]
        ordering = ["entity", "code"]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"

    def default_normal_balance(self) -> str:
        """The natural balance for this account given its type and contra flag."""
        base = NORMAL_BALANCE_BY_TYPE[AccountType(self.account_type)]
        if self.is_contra:
            return (
                NormalBalance.CREDIT if base == NormalBalance.DEBIT else NormalBalance.DEBIT
            )
        return base

    def save(self, *args, **kwargs):
        # Default the normal balance from the type/contra flag when not set explicitly.
        if not self.normal_balance and self.account_type:
            self.normal_balance = self.default_normal_balance()
        return super().save(*args, **kwargs)


class FiscalYear(TimeStampedModel):
    """A financial year for an entity — the container its periods sit in.

    Often a calendar year, but not necessarily: schools and many businesses run
    Sept–Aug or Apr–Mar years. The ``year`` integer is the label used in document
    numbers (``…-2026-00001``); ``start_date``/``end_date`` bound it.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="fiscal_years",
    )
    year = models.PositiveIntegerField(help_text="Label, e.g. 2026; used in document numbers.")
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=12, choices=PeriodStatus.choices, default=PeriodStatus.OPEN,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "year"], name="uniq_finance_fiscalyear_entity_year",
            ),
        ]
        ordering = ["entity", "-year"]

    def __str__(self) -> str:
        return f"FY{self.year} ({self.entity_id})"


class FiscalPeriod(TimeStampedModel):
    """A postable sub-window of a fiscal year (normally a calendar month).

    This is the object the Phase-0 :func:`~vs_finance.posting.ensure_period_open`
    guard was built for: its ``status`` is a :class:`PeriodStatus`, and the posting
    service refuses to write into anything but an OPEN one (SOFT_CLOSED only for
    privileged close-process auto-entries). Closing a period is the control that
    stops the past being silently rewritten.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="fiscal_periods",
    )
    fiscal_year = models.ForeignKey(
        FiscalYear, on_delete=models.PROTECT, related_name="periods",
    )
    period_no = models.PositiveSmallIntegerField(help_text="1–12 for monthly periods (13+ for adjustment periods).")
    name = models.CharField(max_length=40, help_text="e.g. 'Jan 2026'.")
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=12, choices=PeriodStatus.choices, default=PeriodStatus.OPEN,
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_periods_closed", null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["fiscal_year", "period_no"],
                name="uniq_finance_period_year_no",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "start_date", "end_date"]),
        ]
        ordering = ["entity", "fiscal_year", "period_no"]

    def __str__(self) -> str:
        return f"{self.name} [{self.status}]"


class TaxCode(TimeStampedModel):
    """A tax rate and the accounts it books to, for one entity.

    Nigerian set covers VAT (7.5%), WHT and PAYE/pension; the engine stores rate as
    basis points (``750`` = 7.5%) so the calculation stays integer-exact, mirroring
    the kobo rule for money. ``collected_account``/``paid_account`` are the control
    accounts the tax posts to (output vs input VAT, WHT payable …).
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="tax_codes",
    )
    code = models.CharField(max_length=20, help_text="e.g. VAT, WHT-5, PAYE.")
    name = models.CharField(max_length=120)
    rate_bps = models.PositiveIntegerField(
        help_text="Rate in basis points; 750 = 7.5%. Integer-exact, never a float.",
    )
    is_recoverable = models.BooleanField(
        default=True, help_text="Input tax recoverable against output tax (e.g. VAT).",
    )
    collected_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="tax_codes_collected",
        null=True, blank=True, help_text="Output/payable control account.",
    )
    paid_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="tax_codes_paid",
        null=True, blank=True, help_text="Input/recoverable control account.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_finance_taxcode_entity_code",
            ),
        ]
        ordering = ["entity", "code"]

    def __str__(self) -> str:
        return f"{self.code} ({self.rate_bps / 100:.2f}%)"


class CostCenter(TimeStampedModel):
    """An analytical bucket (department, project, branch unit) for slicing the P&L.

    Independent of the CoA: the same expense account ('Salaries') can be split across
    many cost centres ('Primary', 'Secondary', 'Admin'). A journal line may carry an
    optional cost centre so reports can answer 'what did each department spend?'.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="cost_centers",
    )
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=160)
    parent = models.ForeignKey(
        "self", on_delete=models.PROTECT, related_name="children",
        null=True, blank=True,
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_finance_costcenter_entity_code",
            ),
        ]
        ordering = ["entity", "code"]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class Dimension(TimeStampedModel):
    """A user-defined analytical axis (fund, programme, grant, campaign …).

    Cost centres answer 'which department'; dimensions let an entity add its own
    extra axes without schema changes. The axis is declared here; individual values
    are carried on journal lines as a small JSON map (``{dimension_code: value}``),
    keeping the line table narrow while still sliceable.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="dimensions",
    )
    code = models.CharField(max_length=32, help_text="Axis key used inside line ``dimensions`` JSON, e.g. FUND.")
    name = models.CharField(max_length=160)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_finance_dimension_entity_code",
            ),
        ]
        ordering = ["entity", "code"]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class JournalEntry(FinanceDocument):
    """A balanced double-entry transaction — the atom of the ledger.

    Extends :class:`FinanceDocument`, so it inherits entity scoping, a ``CFX-…-JNL-…``
    number, status and ``created_by``. Its lines (:class:`JournalLine`) must net to
    zero (Σdebits = Σcredits) before it can be **posted**; posting is the act that
    makes it affect balances, and is done only through
    :func:`vs_finance.posting.post_journal` (never by flipping ``status`` by hand).

    A reversal is itself a journal whose ``reverses`` points back at the original and
    whose lines are the mirror image — the audit-friendly way to undo, leaving both
    entries permanently on the record.
    """

    DOC_TYPE = DocType.JOURNAL

    date = models.DateField(help_text="Accounting date; determines the period it posts to.")
    period = models.ForeignKey(
        FiscalPeriod, on_delete=models.PROTECT, related_name="journal_entries",
        null=True, blank=True,
    )
    source = models.CharField(
        max_length=12, choices=JournalSource.choices, default=JournalSource.MANUAL,
    )
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="journal_entries",
        null=True, blank=True, help_text="Defaults to the entity base currency.",
    )
    fx_rate = models.DecimalField(
        max_digits=20, decimal_places=10, null=True, blank=True,
        help_text="Rate to base currency at posting; null for base-currency entries.",
    )
    narration = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(
        max_length=64, blank=True, default="",
        help_text="External reference (cheque no., supplier ref, etc.).",
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_journals_posted", null=True, blank=True,
    )
    reverses = models.OneToOneField(
        "self", on_delete=models.PROTECT, related_name="reversed_by",
        null=True, blank=True,
        help_text="Set on a reversing entry; points at the journal it cancels.",
    )

    class Meta(FinanceDocument.Meta):
        verbose_name_plural = "journal entries"
        indexes = [
            models.Index(fields=["entity", "date"]),
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["period"]),
        ]

    @property
    def is_posted(self) -> bool:
        return self.status == DocumentStatus.POSTED

    def totals(self) -> tuple[int, int]:
        """Return ``(total_debit, total_credit)`` in kobo over this entry's lines."""
        from ..posting import sum_sides
        return sum_sides(self.lines.all())


class JournalLine(TimeStampedModel):
    """One leg of a journal entry: a debit or a credit against one account.

    By convention a line is **one-sided** — exactly one of ``debit``/``credit`` is
    non-zero (both are kobo via :class:`~vs_finance.money.MoneyField`). The optional
    ``cost_center`` and ``dimensions`` JSON attach analytics without widening the
    table. Lines are immutable once their journal is posted; corrections are made by
    reversing and re-posting, never by editing history.
    """

    entry = models.ForeignKey(
        JournalEntry, on_delete=models.CASCADE, related_name="lines",
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="journal_lines",
    )
    debit = MoneyField(help_text="Debit amount in kobo (0 if this is a credit line).")
    credit = MoneyField(help_text="Credit amount in kobo (0 if this is a debit line).")
    description = models.CharField(max_length=255, blank=True, default="")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="journal_lines",
        null=True, blank=True,
    )
    dimensions = models.JSONField(
        default=dict, blank=True,
        help_text="Analytical values keyed by Dimension.code, e.g. {'FUND': 'GRANT-A'}.",
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["entry", "line_no", "id"]
        indexes = [
            models.Index(fields=["account"]),
            models.Index(fields=["entry"]),
        ]
        constraints = [
            # A line may not be debit AND credit at once; zero/zero is allowed only as
            # a transient draft state (the balance guard rejects it at post time).
            models.CheckConstraint(
                check=models.Q(debit=0) | models.Q(credit=0),
                name="ck_finance_line_one_sided",
            ),
            models.CheckConstraint(
                check=models.Q(debit__gte=0) & models.Q(credit__gte=0),
                name="ck_finance_line_non_negative",
            ),
        ]

    def __str__(self) -> str:
        side = f"Dr {self.debit}" if self.debit else f"Cr {self.credit}"
        return f"{self.account_id}: {side}"


class AccountBalance(TimeStampedModel):
    """Running per-period totals for an account — a denormalised read model.

    Truth lives in the immutable journal lines; this table is the fast aggregate the
    posting service maintains atomically as entries post and reverse, so trial
    balances and statements don't re-sum the whole ledger each time. One row per
    ``(account, period)``; the closing balance is derived from the side totals and
    the account's normal balance.
    """

    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="balances",
    )
    period = models.ForeignKey(
        FiscalPeriod, on_delete=models.PROTECT, related_name="account_balances",
    )
    opening_debit = MoneyField()
    opening_credit = MoneyField()
    debit_total = MoneyField()
    credit_total = MoneyField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "period"], name="uniq_finance_balance_account_period",
            ),
        ]
        indexes = [models.Index(fields=["period"])]
        ordering = ["period", "account"]

    def __str__(self) -> str:
        return f"{self.account_id}@{self.period_id}: Dr {self.debit_total} Cr {self.credit_total}"

    @property
    def net_kobo(self) -> int:
        """Net movement in kobo, signed to the account's normal balance.

        Positive means the balance moved in its natural direction (a debit account
        that grew, a credit account that grew).
        """
        dr = (self.opening_debit + self.debit_total)
        cr = (self.opening_credit + self.credit_total)
        if self.account.normal_balance == NormalBalance.DEBIT:
            return dr - cr
        return cr - dr


class FinanceAuditLog(models.Model):
    """Authoritative, **append-only** audit record for finance actions.

    This is the finance module's own audit home — deliberately *not* the central
    ``vs_audit`` system. Two properties make it the right place for financial audit:

    * **Transactional.** Success rows are written in the *same* atomic transaction as
      the action they record (a posting can never commit without its audit row), and
      a write failure here is *not* swallowed — unlike central audit, which is
      best-effort by design and may silently drop events.
    * **Immutable.** Rows cannot be updated or deleted (enforced below); corrections
      are new rows, mirroring how the ledger corrects by reversal rather than edit.

    The journals themselves remain the primary trail for the *transactions*; this log
    captures the actions *around* them (who posted/reversed, **rejected attempts**,
    period state changes, master-data edits). A best-effort copy is still mirrored to
    ``vs_audit`` so the platform-wide activity view stays complete — but the record
    here is the source of truth.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="finance_audit_logs",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_audit_events", null=True, blank=True,
        help_text="The user who acted; null for system/automated actions.",
    )
    action = models.CharField(max_length=32, choices=FinanceAuditAction.choices)
    status = models.CharField(
        max_length=8, choices=FinanceAuditStatus.choices,
        default=FinanceAuditStatus.SUCCESS,
    )
    target_type = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Model of the thing acted on, e.g. 'JournalEntry'.",
    )
    target_id = models.CharField(max_length=64, blank=True, default="")
    document_number = models.CharField(max_length=48, blank=True, default="")
    message = models.CharField(max_length=255, blank=True, default="")
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["entity", "action"]),
            models.Index(fields=["target_type", "target_id"]),
            models.Index(fields=["entity", "created_at"]),
        ]
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.action} [{self.status}] {self.document_number or self.target_id}"

    def save(self, *args, **kwargs):
        # Append-only: allow the initial insert, forbid any later mutation.
        if self.pk is not None:
            raise ValueError("FinanceAuditLog rows are immutable and cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("FinanceAuditLog rows are immutable and cannot be deleted.")


