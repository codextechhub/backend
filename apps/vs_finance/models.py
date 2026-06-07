"""Foundational models for the finance engine (Phase 0).

This module holds only the *foundations* every later model leans on:

* :class:`TimeStampedModel` — shared created/updated stamps (matches the ``vs_*``
  convention).
* :class:`LedgerEntity` — the **accounting entity** that owns a set of books. This is
  the tenant of every finance/procurement document, and the key decoupling: the
  ledger belongs to an *entity*, not to a school. A customer School maps to one (or
  more) entities; Codex's own platform books are an entity with **no school**; future
  products plug in the same way.
* :class:`DocumentSequence` — the concurrency-safe, gap-free counter behind every
  human-facing document number.
* :class:`FinanceDocument` — the abstract base for numbered, entity-scoped,
  status-bearing documents (invoices, POs, journals …).

The ledger proper (Account, JournalEntry, FiscalPeriod …) arrives in Phase 1 and
builds on these.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .constants import (
    AccountType,
    AssetStatus,
    BankLineStatus,
    BudgetStatus,
    ConcessionKind,
    CreditNoteKind,
    DepreciationMethod,
    DocType,
    DocumentStatus,
    DunningChannel,
    DunningNoticeStatus,
    FinanceAuditAction,
    FinanceAuditStatus,
    InstallmentStatus,
    InvoicePaymentStatus,
    InvoiceSource,
    JournalSource,
    NORMAL_BALANCE_BY_TYPE,
    NormalBalance,
    PaymentMethod,
    PaymentPlanFrequency,
    PaymentPlanStatus,
    PayrollRunStatus,
    PeriodStatus,
    PLATFORM_ENTITY_CODE,
    TaxFilingFrequency,
    TaxFilingStatus,
    TaxObligationType,
)
from .exceptions import DocumentNumberingError
from .money import MoneyField


class TimeStampedModel(models.Model):
    """Common created/updated timestamps (matches the platform convention)."""

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class LedgerEntityManager(models.Manager):
    def platform(self):
        """Return Codex's own platform entity (the operator's set of books), or None.

        There is conceptually a single active platform entity; we resolve it by the
        reserved code rather than a conditional DB constraint (MariaDB can't enforce
        conditional uniqueness).
        """
        return self.filter(code=PLATFORM_ENTITY_CODE, is_active=True).first()

    def for_school(self, school):
        """All entities (sets of books) sourced from a given School tenant."""
        return self.filter(source_school=school)


class LedgerEntity(TimeStampedModel):
    """A distinct set of books — the tenant of every finance/procurement document.

    The accounting `entity concept` made concrete: books are kept for an entity, and
    an entity may be a customer organisation, Codex itself, or a future product. A
    tenant is *not* forced to be a school, and a single tenant may keep **several**
    entities (e.g. a school that wants to run its own books separately from
    platform-managed ones, or a group with subsidiaries).

    Fields:
        name: Human-friendly name of the entity/company keeping the books.
        code: Short, uppercase, unique identifier; appears inside document numbers
            (e.g. ``CFX-LEKKI-INV-2026-00001``). Reserved code ``CODEX`` is the
            platform entity.
        kind: Classification (platform / tenant / product / other).
        source_school: Optional link to the originating School tenant. **Nullable**
            (platform and product entities have none) and **non-unique** (a tenant
            may own multiple entities — 1:many).
        base_currency: FK to the :class:`Currency` this entity keeps its primary
            ledger in (its reporting currency). Defaults to NGN. Because
            ``Currency``'s PK is the 3-letter code, the column still stores ``"NGN"``
            — the FK just adds referential integrity over the old free-text code.
        is_active / activated_at / deleted_at: lifecycle.
    """

    class Kind(models.TextChoices):
        PLATFORM = "PLATFORM", "Platform (CodeX)"
        TENANT = "TENANT", "Tenant"
        PRODUCT = "PRODUCT", "Product"
        OTHER = "OTHER", "Other"

    name = models.CharField(max_length=160)
    code = models.CharField(
        max_length=16, unique=True,
        help_text="Short uppercase code used inside document numbers; e.g. LEKKI, CODEX.",
    )
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.TENANT)
    source_school = models.ForeignKey(
        "vs_schools.School", on_delete=models.PROTECT,
        related_name="ledger_entities", null=True, blank=True,
        help_text="Originating tenant; NULL for platform/product entities. A tenant "
                  "may own several entities (non-unique).",
    )
    base_currency = models.ForeignKey(
        "Currency", on_delete=models.PROTECT, related_name="entities",
        default="NGN",
        help_text="Primary ledger (reporting) currency. FK to Currency; defaults to NGN.",
    )
    is_active = models.BooleanField(default=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = LedgerEntityManager()

    class Meta:
        indexes = [
            models.Index(fields=["kind", "is_active"]),
            models.Index(fields=["source_school"]),
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"

    @property
    def is_platform(self) -> bool:
        return self.kind == self.Kind.PLATFORM


class DocumentSequence(models.Model):
    """Per-scope counter that issues gap-free document numbers.

    One row exists per ``(entity, branch, doc_type, fiscal_year)`` combination and
    holds the last number handed out. Allocation locks the row with
    ``select_for_update`` so two concurrent requests can never receive the same
    number (the classic duplicate-invoice-number bug). Branch is nullable: it is an
    optional sub-scope used by entities that actually have branches (school tenants);
    platform/product entities leave it null.

    Intentionally tiny and central: every numbered document in finance *and*
    procurement routes through it, so the locking logic is written and tested once.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT,
        related_name="doc_sequences",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        related_name="finance_doc_sequences", null=True, blank=True,
    )
    doc_type = models.CharField(max_length=8, choices=DocType.choices)
    fiscal_year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "branch", "doc_type", "fiscal_year"],
                name="uniq_finance_docseq_scope",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "doc_type", "fiscal_year"]),
        ]

    def __str__(self) -> str:
        scope = self.branch_id or "HQ"
        return f"{self.entity_id}/{scope}/{self.doc_type}/{self.fiscal_year} -> {self.last_number}"


class FinanceDocument(TimeStampedModel):
    """Abstract base for numbered, entity-scoped, status-bearing finance documents.

    Subclasses set a class-level :attr:`DOC_TYPE` (a :class:`~vs_finance.constants.DocType`)
    and call :meth:`assign_number` (or rely on :meth:`save`) to receive a unique
    document number on first save.

    Tenancy: every document belongs to a :class:`LedgerEntity` (the accounting entity
    that keeps the books) and optionally a ``branch`` sub-scope. The entity — not a
    school — is the unit of ownership, so Codex's own books and future products are
    first-class. ``vs_rbac`` scoping (for school entities) keys off these; platform
    books are governed by platform-level access, not school boundaries.

    Document numbers are unique *within an entity*, not globally: each entity keeps
    its own clean ``…-INV-2026-00001`` series.
    """

    #: Override in concrete subclasses, e.g. ``DOC_TYPE = DocType.INVOICE``.
    DOC_TYPE: str | None = None

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_set",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_set", null=True, blank=True,
    )
    document_number = models.CharField(max_length=48, blank=True, db_index=True)
    status = models.CharField(
        max_length=20, choices=DocumentStatus.choices, default=DocumentStatus.DRAFT,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="%(app_label)s_%(class)s_created", null=True, blank=True,
    )

    class Meta:
        abstract = True
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "document_number"],
                name="uniq_%(app_label)s_%(class)s_entity_docnum",
            ),
        ]

    def assign_number(self, *, fiscal_year: int | None = None) -> str:
        """Allocate and store this document's number if it does not have one yet.

        Idempotent: returns the existing number unchanged once assigned. Must run
        inside a transaction (it locks the sequence row); :meth:`save` arranges that.
        """
        from .numbering import next_document_number  # local import avoids cycle

        if self.document_number:
            return self.document_number
        if self.DOC_TYPE is None:
            raise DocumentNumberingError(
                f"{type(self).__name__} must set a class-level DOC_TYPE before numbering."
            )
        if self.entity_id is None:
            raise DocumentNumberingError("Document needs an entity before a number can be allocated.")

        year = fiscal_year if fiscal_year is not None else timezone.now().year
        self.document_number = next_document_number(
            entity=self.entity, branch=self.branch, doc_type=self.DOC_TYPE, fiscal_year=year,
        )
        return self.document_number

    def save(self, *args, **kwargs):
        # Allocate a number on first save, atomically with the row lock it needs.
        if not self.document_number and self.DOC_TYPE is not None and self.entity_id:
            with transaction.atomic():
                self.assign_number()
                super().save(*args, **kwargs)
            return
        return super().save(*args, **kwargs)


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
    description = models.TextField(blank=True, default="")

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
        from .posting import sum_sides
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


# ---------------------------------------------------------------------------
# Phase 2 — Accounts Receivable (the revenue cycle)
# ---------------------------------------------------------------------------
#
# A deliberately **domain-neutral** AR core: a generic Customer is billed with a
# generic Invoice and settles with a generic Payment. Nothing here knows about
# students, parents, fees or terms — a school billing run is just one *source* that
# emits these same generic invoices (the adapter, behind a module flag, comes later).
# The link back to a domain record is a loose, nullable string reference so the ledger
# never imports the students app.


class Customer(TimeStampedModel):
    """A billable party (the AR sub-ledger account) for one entity.

    Generic on purpose: a customer may be a parent/student in a school tenant, a
    client in another, or an internal counterparty in Codex's own books. The optional
    ``source_type``/``source_id`` pair is a *loose* reference to the originating
    domain record (e.g. ``"vs_schools.Student"`` + the student's pk) — stored as plain
    strings, never an FK, so the ledger stays decoupled from any product app.

    ``receivable_account`` is the AR control account this customer's balance rolls up
    into; the customer itself is the sub-ledger detail behind that control.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="customers",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        related_name="finance_customers", null=True, blank=True,
    )
    code = models.CharField(max_length=32, help_text="Customer code, unique within the entity.")
    name = models.CharField(max_length=200)
    billing_email = models.EmailField(blank=True, default="")
    billing_phone = models.CharField(max_length=32, blank=True, default="")
    billing_address = models.TextField(blank=True, default="")
    receivable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="ar_customers",
        null=True, blank=True,
        help_text="AR control account this customer's balance rolls into.",
    )
    opening_balance = MoneyField(help_text="Opening AR balance in kobo (informational; not auto-posted).")
    source_type = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Loose reference to the originating domain record's model, e.g. 'vs_schools.Student'.",
    )
    source_id = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_finance_customer_entity_code",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "is_active"]),
            models.Index(fields=["source_type", "source_id"]),
        ]
        ordering = ["entity", "code"]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"


class Invoice(FinanceDocument):
    """A generic sales invoice raised against a :class:`Customer`.

    Extends :class:`FinanceDocument` (entity scope, ``CFX-…-INV-…`` number, status,
    ``created_by``). Money totals are held in kobo and recomputed from the lines.
    Posting (:func:`vs_finance.receivables.post_invoice`) raises the AR journal
    (Dr receivable, Cr revenue, Cr output tax) and links it via ``journal``.

    Two status axes: the inherited document ``status`` tracks the ledger lifecycle
    (DRAFT→POSTED→CANCELLED), while ``payment_status`` tracks cash settled, derived
    from ``amount_paid`` vs ``total`` as payments allocate.
    """

    DOC_TYPE = DocType.INVOICE

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="invoices",
    )
    invoice_date = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="invoices",
        null=True, blank=True,
    )
    source = models.CharField(
        max_length=16, choices=InvoiceSource.choices, default=InvoiceSource.MANUAL,
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")

    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Total tax, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo.")
    amount_paid = MoneyField(help_text="Cash allocated to this invoice, in kobo.")
    amount_credited = MoneyField(
        help_text="Non-cash reductions (credit notes, write-offs) applied to this "
                  "invoice, in kobo. Reduces the balance due without recording cash.",
    )
    payment_status = models.CharField(
        max_length=8, choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
    )

    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="ar_invoices",
        null=True, blank=True, help_text="The AR journal raised when this invoice posted.",
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "payment_status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["entity", "invoice_date"]),
        ]

    @property
    def settled_amount(self) -> int:
        """Total settled (cash + non-cash credits/write-offs), in kobo."""
        return self.amount_paid + self.amount_credited

    @property
    def balance_due(self) -> int:
        """Outstanding amount in kobo (total minus cash paid and non-cash credits)."""
        return self.total - self.settled_amount

    def recompute_totals(self, *, save: bool = True) -> None:
        """Roll the line amounts up into subtotal/tax_total/total (kobo)."""
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    def refresh_payment_status(self, *, save: bool = True) -> None:
        """Derive ``payment_status`` from amount settled (cash + credits) vs ``total``."""
        settled = self.settled_amount
        if settled <= 0:
            status = InvoicePaymentStatus.UNPAID
        elif settled >= self.total:
            status = InvoicePaymentStatus.PAID
        else:
            status = InvoicePaymentStatus.PARTIAL
        self.payment_status = status
        if save:
            self.save(update_fields=["payment_status", "updated_at"])


class InvoiceLine(TimeStampedModel):
    """One billable line of an :class:`Invoice` → a GL revenue account (+ optional tax).

    ``net_amount`` (kobo) is ``quantity × unit_price`` and ``tax_amount`` is computed
    from the line's :class:`TaxCode` at post time; both are stored so the invoice
    total is a simple, auditable sum and never re-derived inconsistently.
    """

    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE, related_name="lines",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    revenue_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="invoice_lines",
        help_text="GL revenue account credited for this line's net.",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Price per unit in kobo.")
    tax_code = models.ForeignKey(
        TaxCode, on_delete=models.PROTECT, related_name="invoice_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Tax on the net, in kobo.")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="invoice_lines",
        null=True, blank=True,
    )
    dimensions = models.JSONField(default=dict, blank=True)
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["invoice", "line_no", "id"]
        indexes = [models.Index(fields=["invoice"]), models.Index(fields=["revenue_account"])]

    @property
    def line_total(self) -> int:
        return self.net_amount + self.tax_amount

    def __str__(self) -> str:
        return f"{self.description or self.revenue_account_id}: {self.line_total}"


class Payment(FinanceDocument):
    """A customer receipt — money in, settling one or more invoices.

    Extends :class:`FinanceDocument` (DOC_TYPE RECEIPT → ``CFX-…-RCP-…``). Posting
    (:func:`vs_finance.receivables.post_payment`) raises Dr bank/cash, Cr AR control,
    then allocates the cash across invoices (oldest-first or explicit). Any amount
    beyond what's allocated remains an unallocated **credit** on the customer.
    """

    DOC_TYPE = DocType.RECEIPT

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="payments",
    )
    payment_date = models.DateField()
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="payments",
        null=True, blank=True,
    )
    method = models.CharField(
        max_length=16, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER,
    )
    amount = MoneyField(help_text="Total received, in kobo.")
    allocated_amount = MoneyField(help_text="Portion allocated to invoices, in kobo.")
    deposit_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="customer_payments",
        null=True, blank=True,
        help_text="Bank/cash account debited (where the money landed).",
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="ar_payments",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["entity", "payment_date"]),
        ]

    @property
    def unallocated_amount(self) -> int:
        """Cash not yet applied to any invoice — an open credit on the customer."""
        return self.amount - self.allocated_amount


class PaymentAllocation(TimeStampedModel):
    """Links a slice of a :class:`Payment` to a specific :class:`Invoice`.

    The GL already moved when the payment posted (Dr bank, Cr AR); allocation is the
    *sub-ledger* act of saying which invoices that AR credit settles. This keeps
    partial payments and unallocated credit first-class without further GL postings.
    """

    payment = models.ForeignKey(
        Payment, on_delete=models.CASCADE, related_name="allocations",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="allocations",
    )
    amount = MoneyField(help_text="Amount of the payment applied to this invoice, in kobo.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["payment", "invoice"], name="uniq_finance_alloc_payment_invoice",
            ),
            models.CheckConstraint(
                check=models.Q(amount__gte=0), name="ck_finance_alloc_non_negative",
            ),
        ]
        indexes = [models.Index(fields=["invoice"]), models.Index(fields=["payment"])]
        ordering = ["payment", "id"]

    def __str__(self) -> str:
        return f"{self.payment_id}→{self.invoice_id}: {self.amount}"


# ---------------------------------------------------------------------------
# Phase 4 — AR adjustments (credit/debit notes, refunds, write-offs)
# ---------------------------------------------------------------------------
#
# The other side of the revenue cycle: not every billed amount is collected as first
# raised. A *credit note* gives value back (returns, allowances, corrections), a *debit
# note* charges more, a *refund* hands cash back for an over-paid credit balance, and a
# *write-off* concedes a receivable as bad debt. All post through the same
# `post_journal` service; credit notes and write-offs reduce an invoice's balance via
# its `amount_credited` field rather than recording cash.


class CreditNote(FinanceDocument):
    """A credit or debit note against a :class:`Customer`'s receivable.

    ``kind`` selects direction (:class:`~vs_finance.constants.CreditNoteKind`): a CREDIT
    note reduces AR (``Dr revenue/returns + Dr output tax, Cr AR``) and may be applied
    to specific invoices like a non-cash payment; a DEBIT note increases AR
    (``Dr AR, Cr revenue + Cr output tax``) as a supplementary charge. The document
    number token follows the kind (``CRN`` vs ``DRN``). Money is kobo throughout.
    """

    DOC_TYPE = DocType.CREDIT_NOTE  # overridden per-instance for DEBIT notes (DRN)

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="credit_notes",
    )
    kind = models.CharField(
        max_length=6, choices=CreditNoteKind.choices, default=CreditNoteKind.CREDIT,
    )
    note_date = models.DateField()
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="credit_notes",
        null=True, blank=True,
    )
    reason = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(max_length=64, blank=True, default="")
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="credit_notes",
        null=True, blank=True,
        help_text="Optional originating invoice this note relates to.",
    )

    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Total tax reversed/charged, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo.")
    allocated_amount = MoneyField(
        help_text="Portion of a CREDIT note applied to invoices, in kobo.",
    )

    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="credit_notes",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "kind"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["entity", "note_date"]),
        ]

    @property
    def is_debit(self) -> bool:
        return self.kind == CreditNoteKind.DEBIT

    @property
    def unallocated_amount(self) -> int:
        """Credit not yet applied to an invoice (CREDIT notes only)."""
        return self.total - self.allocated_amount

    def recompute_totals(self, *, save: bool = True) -> None:
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    def save(self, *args, **kwargs):
        # The document-number token tracks the note's direction (CRN vs DRN).
        if not self.document_number:
            self.DOC_TYPE = (
                DocType.DEBIT_NOTE if self.kind == CreditNoteKind.DEBIT
                else DocType.CREDIT_NOTE
            )
        return super().save(*args, **kwargs)


class CreditNoteLine(TimeStampedModel):
    """One line of a :class:`CreditNote` → a GL revenue/returns account (+ optional tax)."""

    note = models.ForeignKey(
        CreditNote, on_delete=models.CASCADE, related_name="lines",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    revenue_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="credit_note_lines",
        help_text="Revenue/returns account adjusted for this line's net.",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Price per unit in kobo.")
    tax_code = models.ForeignKey(
        TaxCode, on_delete=models.PROTECT, related_name="credit_note_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Tax on the net, in kobo.")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="credit_note_lines",
        null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["note", "line_no", "id"]
        indexes = [models.Index(fields=["note"]), models.Index(fields=["revenue_account"])]

    @property
    def line_total(self) -> int:
        return self.net_amount + self.tax_amount

    def __str__(self) -> str:
        return f"{self.description or self.revenue_account_id}: {self.line_total}"


class CreditNoteAllocation(TimeStampedModel):
    """Links a slice of a CREDIT :class:`CreditNote` to a specific :class:`Invoice`.

    The GL already moved when the note posted (Dr revenue, Cr AR); allocation is the
    sub-ledger act of saying which invoices that credit settles, mirroring
    :class:`PaymentAllocation`. It bumps the invoice's ``amount_credited``.
    """

    note = models.ForeignKey(
        CreditNote, on_delete=models.CASCADE, related_name="allocations",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="credit_allocations",
    )
    amount = MoneyField(help_text="Amount of the note applied to this invoice, in kobo.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["note", "invoice"], name="uniq_finance_cnalloc_note_invoice",
            ),
            models.CheckConstraint(
                check=models.Q(amount__gte=0), name="ck_finance_cnalloc_non_negative",
            ),
        ]
        indexes = [models.Index(fields=["invoice"]), models.Index(fields=["note"])]
        ordering = ["note", "id"]

    def __str__(self) -> str:
        return f"{self.note_id}→{self.invoice_id}: {self.amount}"


class Refund(FinanceDocument):
    """A cash refund paid back to a :class:`Customer` for an over-paid credit balance.

    Posting (:func:`vs_finance.credit_notes.post_refund`) raises ``Dr AR control,
    Cr bank`` — handing money back and restoring the customer's receivable to zero.
    """

    DOC_TYPE = DocType.REFUND

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="refunds",
    )
    refund_date = models.DateField()
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="refunds",
        null=True, blank=True,
    )
    method = models.CharField(
        max_length=16, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER,
    )
    amount = MoneyField(help_text="Amount refunded, in kobo.")
    bank_account = models.ForeignKey(
        "BankAccount", on_delete=models.PROTECT, related_name="refunds",
        null=True, blank=True,
        help_text="Bank account the refund is paid from.",
    )
    deposit_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="customer_refunds",
        null=True, blank=True,
        help_text="Cash/bank GL account credited (where the money left from).",
    )
    reference = models.CharField(max_length=64, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="ar_refunds",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["entity", "refund_date"]),
        ]


class Concession(FinanceDocument):
    """A non-cash reduction of a receivable — a discount, waiver or scholarship.

    Posting (:func:`vs_finance.installments.post_concession`) raises ``Dr discounts &
    allowances, Cr AR control`` for ``amount`` and clears that much of the linked
    invoice via :attr:`Invoice.amount_credited` — exactly like a targeted, single-line
    credit note, but tagged by :class:`~vs_finance.constants.ConcessionKind` for
    reporting (a school tenant's *scholarship*/*bursary* is just ``kind=SCHOLARSHIP``).
    """

    DOC_TYPE = DocType.CONCESSION

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="concessions",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="concessions",
        help_text="The invoice whose balance this concession reduces.",
    )
    kind = models.CharField(
        max_length=12, choices=ConcessionKind.choices, default=ConcessionKind.DISCOUNT,
    )
    concession_date = models.DateField()
    amount = MoneyField(help_text="Amount of the receivable forgiven/discounted, in kobo.")
    allowance_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="concessions",
        null=True, blank=True,
        help_text="Contra-revenue/expense account debited. Defaults to 4910 "
                  "Discounts & Concessions Allowed.",
    )
    reason = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(max_length=64, blank=True, default="")
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="concessions",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "kind"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["invoice"]),
            models.Index(fields=["entity", "concession_date"]),
        ]


class PaymentPlan(FinanceDocument):
    """An installment schedule that spreads a receivable over dated installments.

    A pure scheduling overlay — it never posts to the GL. The invoice it references
    already sits in AR; the plan only says *when* the customer is expected to pay and
    *how much* each time, so reminders/dunning and progress tracking have something to
    measure against. Settlement is reflected by distributing the linked invoice's
    settled amount across installments oldest-first
    (:func:`vs_finance.installments.refresh_plan_progress`).
    """

    DOC_TYPE = DocType.PAYMENT_PLAN

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="payment_plans",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="payment_plans",
        null=True, blank=True,
        help_text="The invoice this plan settles (optional for a standalone plan).",
    )
    plan_status = models.CharField(
        max_length=10, choices=PaymentPlanStatus.choices, default=PaymentPlanStatus.DRAFT,
    )
    start_date = models.DateField(help_text="Due date of the first installment.")
    frequency = models.CharField(
        max_length=12, choices=PaymentPlanFrequency.choices,
        default=PaymentPlanFrequency.MONTHLY,
    )
    installment_count = models.PositiveSmallIntegerField(default=1)
    total_amount = MoneyField(help_text="Total amount being spread, in kobo.")
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "plan_status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["invoice"]),
            models.Index(fields=["entity", "start_date"]),
        ]

    @property
    def scheduled_total(self) -> int:
        """Sum of the installment amounts (should equal ``total_amount`` once built)."""
        return self.installments.aggregate(s=models.Sum("amount"))["s"] or 0

    @property
    def settled_total(self) -> int:
        """Sum settled across installments, in kobo."""
        return self.installments.aggregate(s=models.Sum("amount_settled"))["s"] or 0

    @property
    def outstanding_total(self) -> int:
        return self.total_amount - self.settled_total


class PaymentPlanInstallment(TimeStampedModel):
    """One dated installment of a :class:`PaymentPlan` (scheduling detail, no GL)."""

    plan = models.ForeignKey(
        PaymentPlan, on_delete=models.CASCADE, related_name="installments",
    )
    seq_no = models.PositiveSmallIntegerField(help_text="1-based position in the schedule.")
    due_date = models.DateField()
    amount = MoneyField(help_text="Amount due for this installment, in kobo.")
    amount_settled = MoneyField(help_text="Amount settled against this installment, in kobo.")
    status = models.CharField(
        max_length=8, choices=InstallmentStatus.choices, default=InstallmentStatus.PENDING,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "seq_no"], name="uniq_finance_installment_plan_seq",
            ),
        ]
        indexes = [
            models.Index(fields=["plan"]),
            models.Index(fields=["due_date", "status"]),
        ]
        ordering = ["plan", "seq_no", "id"]

    @property
    def balance(self) -> int:
        return self.amount - self.amount_settled

    def is_overdue(self, *, as_of=None) -> bool:
        """True if not fully settled and its due date has passed ``as_of`` (default today)."""
        import datetime as _dt

        ref = as_of or _dt.date.today()
        return self.balance > 0 and self.due_date < ref

    def __str__(self) -> str:
        return f"#{self.seq_no} due {self.due_date}: {self.amount} ({self.status})"


# ---------------------------------------------------------------------------
# AR — dunning / automated payment reminders
# ---------------------------------------------------------------------------
#
# A dunning policy is a ladder of stages keyed by how many days an invoice is overdue.
# Generating a run scans the entity's open invoices, matches the *highest* stage each
# crosses, and emits a DunningNotice (idempotent per invoice+level). Notices never post
# to the GL — vs_finance only tracks the reminder lifecycle; an outer notifications
# service dispatches PENDING notices through the recorded channel.


class DunningPolicy(TimeStampedModel):
    """A named ladder of escalating reminder stages for an entity's overdue receivables."""

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="dunning_policies",
    )
    name = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False,
        help_text="The policy a dunning run uses when none is named. At most one per entity.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "name"], name="uniq_finance_dunning_policy_name",
            ),
            models.UniqueConstraint(
                fields=["entity"], condition=models.Q(is_default=True),
                name="uniq_finance_dunning_policy_default",
            ),
        ]
        indexes = [models.Index(fields=["entity", "is_active"])]
        ordering = ["entity", "name"]
        verbose_name_plural = "dunning policies"

    def __str__(self) -> str:
        return f"{self.name} ({self.entity.code})"


class DunningStage(TimeStampedModel):
    """One rung of a :class:`DunningPolicy` — fires once an invoice is ``min_days_overdue``."""

    policy = models.ForeignKey(
        DunningPolicy, on_delete=models.CASCADE, related_name="stages",
    )
    level = models.PositiveSmallIntegerField(help_text="1-based escalation order.")
    name = models.CharField(max_length=80, help_text="e.g. 'First reminder', 'Final notice'.")
    min_days_overdue = models.PositiveSmallIntegerField(
        help_text="Days past due an invoice must be before this stage applies.",
    )
    channel = models.CharField(
        max_length=8, choices=DunningChannel.choices, default=DunningChannel.EMAIL,
    )
    message = models.TextField(
        blank=True, default="",
        help_text="Reminder text/template copied onto each notice raised at this stage.",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["policy", "level"], name="uniq_finance_dunning_stage_level",
            ),
        ]
        indexes = [models.Index(fields=["policy", "min_days_overdue"])]
        ordering = ["policy", "level"]

    def __str__(self) -> str:
        return f"L{self.level} {self.name} (≥{self.min_days_overdue}d)"


class DunningNotice(FinanceDocument):
    """A single reminder raised for an overdue invoice at a given escalation level.

    A communications overlay — it never posts to the GL. ``level`` snapshots the stage
    that fired and ``amount_due`` the invoice balance when generated; the notice is keyed
    uniquely per (invoice, level) so re-running a policy never duplicates a reminder the
    customer already received at that rung.
    """

    DOC_TYPE = DocType.DUNNING_NOTICE

    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="dunning_notices",
    )
    invoice = models.ForeignKey(
        Invoice, on_delete=models.PROTECT, related_name="dunning_notices",
    )
    policy = models.ForeignKey(
        DunningPolicy, on_delete=models.PROTECT, related_name="notices",
        null=True, blank=True,
    )
    stage = models.ForeignKey(
        DunningStage, on_delete=models.SET_NULL, related_name="notices",
        null=True, blank=True,
    )
    level = models.PositiveSmallIntegerField(help_text="Escalation level this notice fired at.")
    notice_date = models.DateField(help_text="The 'as at' date the run was generated for.")
    days_overdue = models.PositiveSmallIntegerField(default=0)
    amount_due = MoneyField(help_text="Invoice balance outstanding when generated, in kobo.")
    channel = models.CharField(
        max_length=8, choices=DunningChannel.choices, default=DunningChannel.EMAIL,
    )
    message = models.TextField(blank=True, default="")
    notice_status = models.CharField(
        max_length=10, choices=DunningNoticeStatus.choices,
        default=DunningNoticeStatus.PENDING,
    )
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta(FinanceDocument.Meta):
        constraints = FinanceDocument.Meta.constraints + [
            models.UniqueConstraint(
                fields=["invoice", "level"], name="uniq_finance_dunning_notice_invoice_level",
            ),
        ]
        indexes = [
            models.Index(fields=["entity", "notice_status"]),
            models.Index(fields=["customer"]),
            models.Index(fields=["invoice"]),
            models.Index(fields=["entity", "notice_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.document_number or 'DUN?'} L{self.level} {self.invoice_id} ({self.notice_status})"


# ---------------------------------------------------------------------------
# Phase 4 — banking, expenses, payroll, budget, fixed assets, period close
# ---------------------------------------------------------------------------
#
# All of these are entity-scoped finance-core concepts that post through the same
# `post_journal` service and period-lock guards as everything else. Nothing here
# imports a product/school app; staff are referenced through the platform user model
# (already used for `created_by`/`posted_by`), and a bank account is just a 1:1 view
# onto a cash/bank GL account.


class BankAccount(TimeStampedModel):
    """A real-world bank (or cash) account, mapped 1:1 to a GL cash account.

    The ledger already tracks cash in a GL account (e.g. ``1100 Cash & Bank`` or a
    child of it); this model adds the banking-side metadata (bank name, number) and is
    the anchor for statement import and reconciliation. Money still only ever moves via
    journals against ``gl_account`` — this is not a second source of truth for balance.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="bank_accounts",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        related_name="finance_bank_accounts", null=True, blank=True,
    )
    gl_account = models.OneToOneField(
        Account, on_delete=models.PROTECT, related_name="bank_account",
        help_text="The cash/bank GL account this maps to (1:1). All movement posts here.",
    )
    name = models.CharField(max_length=160, help_text="Friendly label, e.g. 'GTBank Operations'.")
    bank_name = models.CharField(max_length=120, blank=True, default="")
    account_number = models.CharField(max_length=34, blank=True, default="")
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="bank_accounts",
        null=True, blank=True, help_text="Defaults to the entity base currency.",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "name"], name="uniq_finance_bank_entity_name",
            ),
        ]
        indexes = [models.Index(fields=["entity", "is_active"])]
        ordering = ["entity", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.account_number or self.gl_account_id})"


class BankStatementLine(TimeStampedModel):
    """One line of an imported bank statement, awaiting reconciliation.

    ``amount`` is **signed** in kobo from *our* perspective: positive is money into the
    account (a GL **debit** to the cash account), negative is money out (a GL credit).
    A line is reconciled by pairing it with the matching cash-account
    :class:`JournalLine`; charges/credits the books don't yet know about get an
    *adjusting* journal first, then match.
    """

    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.CASCADE, related_name="statement_lines",
    )
    txn_date = models.DateField(help_text="Value/transaction date on the statement.")
    description = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(max_length=64, blank=True, default="")
    amount = MoneyField(help_text="Signed kobo: +inflow (GL debit), -outflow (GL credit).")
    status = models.CharField(
        max_length=10, choices=BankLineStatus.choices, default=BankLineStatus.UNMATCHED,
    )
    matched_line = models.ForeignKey(
        "JournalLine", on_delete=models.SET_NULL, related_name="bank_statement_lines",
        null=True, blank=True,
        help_text="The cash-account journal line this statement line reconciles to.",
    )
    adjusting_journal = models.ForeignKey(
        "JournalEntry", on_delete=models.SET_NULL, related_name="bank_adjustments",
        null=True, blank=True,
        help_text="Journal raised to book an unrecorded charge/credit before matching.",
    )
    external_id = models.CharField(
        max_length=128, blank=True, default="",
        help_text="Bank/provider line id, used to de-duplicate on import.",
    )
    reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bank_account", "external_id"],
                condition=models.Q(external_id__gt=""),
                name="uniq_finance_bankline_external",
            ),
        ]
        indexes = [
            models.Index(fields=["bank_account", "status"]),
            models.Index(fields=["bank_account", "txn_date"]),
        ]
        ordering = ["bank_account", "txn_date", "id"]

    def __str__(self) -> str:
        return f"{self.txn_date} {self.amount} [{self.status}]"


class ExpenseClaim(FinanceDocument):
    """A staff expense claim — staff acts as a one-off 'vendor' to be reimbursed.

    Posting raises ``Dr expense(s) (+ Dr input VAT), Cr accrued reimbursement`` — the
    liability owed to the employee. Settling it later (:func:`vs_finance.expenses.
    settle_expense_claim`) pays the employee: ``Dr accrued reimbursement, Cr bank``.
    Reuses :class:`InvoicePaymentStatus` for how much has been reimbursed.
    """

    DOC_TYPE = DocType.EXPENSE_CLAIM

    claimant = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_expense_claims", null=True, blank=True,
        help_text="The employee being reimbursed.",
    )
    claimant_name = models.CharField(
        max_length=160, blank=True, default="",
        help_text="Free-text name when the claimant isn't a platform user.",
    )
    claim_date = models.DateField()
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="expense_claims",
        null=True, blank=True,
    )
    title = models.CharField(max_length=200, blank=True, default="")
    narration = models.CharField(max_length=255, blank=True, default="")
    reimbursement_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="expense_claims",
        null=True, blank=True,
        help_text="Liability credited (accrued reimbursement). Defaults to 2400.",
    )
    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Recoverable input tax, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo.")
    amount_paid = MoneyField(help_text="Reimbursed so far, in kobo.")
    payment_status = models.CharField(
        max_length=8, choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
    )
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="expense_claims",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["entity", "payment_status"]),
            models.Index(fields=["claimant"]),
        ]

    @property
    def balance_due(self) -> int:
        return self.total - self.amount_paid

    def recompute_totals(self, *, save: bool = True) -> None:
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    def refresh_payment_status(self, *, save: bool = True) -> None:
        if self.amount_paid <= 0:
            status = InvoicePaymentStatus.UNPAID
        elif self.amount_paid >= self.total:
            status = InvoicePaymentStatus.PAID
        else:
            status = InvoicePaymentStatus.PARTIAL
        self.payment_status = status
        if save:
            self.save(update_fields=["payment_status", "updated_at"])


class ExpenseClaimLine(TimeStampedModel):
    """One expense line of a claim → a GL expense account (+ optional recoverable tax)."""

    claim = models.ForeignKey(
        ExpenseClaim, on_delete=models.CASCADE, related_name="lines",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="expense_claim_lines",
        help_text="GL expense account debited for this line's net.",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Price per unit in kobo.")
    tax_code = models.ForeignKey(
        TaxCode, on_delete=models.PROTECT, related_name="expense_claim_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Recoverable tax on the net, in kobo.")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="expense_claim_lines",
        null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["claim", "line_no", "id"]
        indexes = [models.Index(fields=["claim"]), models.Index(fields=["expense_account"])]

    @property
    def line_total(self) -> int:
        return self.net_amount + self.tax_amount

    def __str__(self) -> str:
        return f"{self.description or self.expense_account_id}: {self.line_total}"


class PettyCashFund(TimeStampedModel):
    """A physical petty-cash float, mapped 1:1 to its own petty-cash GL account.

    Master data (like :class:`BankAccount`) — money only ever moves via journals against
    :attr:`gl_account`; this row adds the operational metadata (custodian, the fixed
    ``float_amount`` the imprest is restored to) and a live ``current_balance`` mirror of
    the cash physically on hand. The fund runs **perpetually**: each
    :class:`PettyCashVoucher` posts ``Dr expense, Cr petty cash`` as it is spent, and
    :func:`vs_finance.petty_cash.replenish_fund` tops the float back up
    (``Dr petty cash, Cr bank``). ``current_balance`` always equals the GL balance of
    ``gl_account``.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="petty_cash_funds",
    )
    branch = models.ForeignKey(
        "vs_schools.Branch", on_delete=models.PROTECT,
        related_name="finance_petty_cash_funds", null=True, blank=True,
    )
    gl_account = models.OneToOneField(
        Account, on_delete=models.PROTECT, related_name="petty_cash_fund",
        help_text="The petty-cash GL account this float maps to (1:1). All movement posts here.",
    )
    name = models.CharField(max_length=160, help_text="Friendly label, e.g. 'Front-desk float'.")
    custodian = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="petty_cash_funds", null=True, blank=True,
        help_text="The person accountable for the cash tin.",
    )
    custodian_name = models.CharField(
        max_length=160, blank=True, default="",
        help_text="Free-text custodian when not a platform user.",
    )
    float_amount = MoneyField(
        help_text="The imprest/float the fund is restored to on replenishment, in kobo.",
    )
    current_balance = MoneyField(
        help_text="Live cash on hand, in kobo (maintained by the petty-cash ledger).",
    )
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="petty_cash_funds",
        null=True, blank=True, help_text="Defaults to the entity base currency.",
    )
    last_replenished_at = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "name"], name="uniq_finance_pettycash_entity_name",
            ),
        ]
        indexes = [models.Index(fields=["entity", "is_active"])]
        ordering = ["entity", "name"]

    @property
    def shortfall(self) -> int:
        """How much a replenishment would draw to restore the float (kobo, never negative)."""
        return max(self.float_amount - self.current_balance, 0)

    def __str__(self) -> str:
        return f"{self.name} ({self.current_balance} kobo on hand)"


class PettyCashVoucher(FinanceDocument):
    """A single small disbursement from a petty-cash fund, recorded against a voucher slip.

    Posting (perpetual) raises ``Dr expense(s) (+ Dr input VAT), Cr petty cash`` and lowers
    the fund's ``current_balance`` by the gross total. A voucher whose total exceeds the
    cash on hand is rejected (:class:`~vs_finance.exceptions.PettyCashOverdrawError`) — you
    cannot pay out more than is in the tin.
    """

    DOC_TYPE = DocType.PETTY_CASH_VOUCHER

    fund = models.ForeignKey(
        PettyCashFund, on_delete=models.PROTECT, related_name="vouchers",
    )
    voucher_date = models.DateField()
    payee = models.CharField(
        max_length=160, blank=True, default="",
        help_text="Who the cash was paid to.",
    )
    spent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="petty_cash_vouchers", null=True, blank=True,
        help_text="The staff member who incurred the spend.",
    )
    narration = models.CharField(max_length=255, blank=True, default="")
    reference = models.CharField(max_length=64, blank=True, default="")
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="petty_cash_vouchers",
        null=True, blank=True,
    )
    subtotal = MoneyField(help_text="Net of tax, in kobo.")
    tax_total = MoneyField(help_text="Recoverable input tax, in kobo.")
    total = MoneyField(help_text="subtotal + tax_total, in kobo (the cash paid out).")
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="petty_cash_vouchers",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "status"]),
            models.Index(fields=["fund"]),
        ]

    def recompute_totals(self, *, save: bool = True) -> None:
        agg = self.lines.aggregate(
            net=models.Sum("net_amount"), tax=models.Sum("tax_amount"),
        )
        self.subtotal = agg["net"] or 0
        self.tax_total = agg["tax"] or 0
        self.total = self.subtotal + self.tax_total
        if save:
            self.save(update_fields=["subtotal", "tax_total", "total", "updated_at"])

    def __str__(self) -> str:
        return f"{self.document_number or self.pk}: {self.total} kobo"


class PettyCashVoucherLine(TimeStampedModel):
    """One expense line of a petty-cash voucher → a GL expense account (+ optional tax)."""

    voucher = models.ForeignKey(
        PettyCashVoucher, on_delete=models.CASCADE, related_name="lines",
    )
    description = models.CharField(max_length=255, blank=True, default="")
    expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="petty_cash_voucher_lines",
        help_text="GL expense account debited for this line's net.",
    )
    quantity = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    unit_price = MoneyField(help_text="Price per unit in kobo.")
    tax_code = models.ForeignKey(
        TaxCode, on_delete=models.PROTECT, related_name="petty_cash_voucher_lines",
        null=True, blank=True,
    )
    net_amount = MoneyField(help_text="quantity × unit_price, in kobo.")
    tax_amount = MoneyField(help_text="Recoverable tax on the net, in kobo.")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="petty_cash_voucher_lines",
        null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["voucher", "line_no", "id"]
        indexes = [models.Index(fields=["voucher"]), models.Index(fields=["expense_account"])]

    @property
    def line_total(self) -> int:
        return self.net_amount + self.tax_amount

    def __str__(self) -> str:
        return f"{self.description or self.expense_account_id}: {self.line_total}"


class TaxObligation(TimeStampedModel):
    """Master data for a recurring statutory remittance the entity must file & pay.

    Maps a tax type (VAT / WHT / PAYE / pension …) to the GL **liability control account**
    whose accumulated balance is what gets remitted, plus the authority it is paid to and
    how often a return falls due. VAT additionally carries a ``recoverable_account`` (input
    VAT, an asset) that is netted off the output payable at filing time.

    Kept as data (not hard-coded in services) so an entity with a customised chart, or a
    new statutory levy, can be configured without a code change.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="tax_obligations",
    )
    code = models.CharField(max_length=24, help_text="Short slug, e.g. 'VAT', 'WHT', 'PAYE'.")
    name = models.CharField(max_length=160)
    obligation_type = models.CharField(
        max_length=12, choices=TaxObligationType.choices,
        help_text="Which statutory tax this obligation covers.",
    )
    liability_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="tax_obligations",
        help_text="Liability control account whose balance is remitted (Dr on payment).",
    )
    recoverable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="tax_obligations_recoverable",
        null=True, blank=True,
        help_text="Recoverable input account netted off at filing (VAT only). Usually 1300.",
    )
    authority_name = models.CharField(
        max_length=160, blank=True, default="",
        help_text="Who the tax is paid to, e.g. 'FIRS', 'Lagos State IRS', a PFA.",
    )
    frequency = models.CharField(
        max_length=12, choices=TaxFilingFrequency.choices,
        default=TaxFilingFrequency.MONTHLY,
    )
    filing_day = models.PositiveSmallIntegerField(
        default=21,
        help_text="Day of the month after period end the return is due (e.g. 21 for VAT).",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "code"], name="uniq_finance_taxobligation_entity_code",
            ),
        ]
        ordering = ["entity", "code"]
        indexes = [models.Index(fields=["entity", "is_active"])]

    def __str__(self) -> str:
        return f"{self.code} → {self.authority_name or self.liability_account_id}"


class TaxFiling(FinanceDocument):
    """A single statutory return for one obligation over one period, with its remittance.

    Lifecycle ``DRAFT → FILED → PAID`` (perpetual ledger; the liability already sits in the
    control account from source transactions):

    * **Prepare** (:func:`vs_finance.tax_filing.prepare_filing`): derive the amount owed
      from the GL movement of the obligation's ``liability_account`` over the period (for
      VAT, less the recoverable input movement). No posting — a draft worksheet.
    * **File** (:func:`vs_finance.tax_filing.file_filing`): freeze the figures and submit.
      Posts a netting/penalty journal only if there is recoverable input to clear or a
      penalty/interest adjustment, so the liability account is left holding exactly
      ``amount_due``.
    * **Pay** (:func:`vs_finance.tax_filing.pay_filing`): ``Dr liability, Cr bank`` for the
      remittance; supports partial payment. Reuses :class:`InvoicePaymentStatus`.
    """

    DOC_TYPE = DocType.TAX_FILING

    obligation = models.ForeignKey(
        TaxObligation, on_delete=models.PROTECT, related_name="filings",
    )
    period_start = models.DateField()
    period_end = models.DateField()
    due_date = models.DateField(null=True, blank=True)
    filing_status = models.CharField(
        max_length=10, choices=TaxFilingStatus.choices, default=TaxFilingStatus.DRAFT,
    )
    gross_liability = MoneyField(help_text="Output/payable accrued in the period, in kobo.")
    recoverable_amount = MoneyField(help_text="Recoverable input netted off (VAT), in kobo.")
    adjustment_amount = MoneyField(
        help_text="Penalty / interest added at filing (increases amount due), in kobo.",
    )
    amount_due = MoneyField(help_text="gross_liability − recoverable_amount + adjustment, in kobo.")
    amount_paid = MoneyField(help_text="Remitted so far, in kobo.")
    payment_status = models.CharField(
        max_length=8, choices=InvoicePaymentStatus.choices,
        default=InvoicePaymentStatus.UNPAID,
    )
    adjustment_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="tax_filing_adjustments",
        null=True, blank=True,
        help_text="Expense account debited for a penalty/interest adjustment.",
    )
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="tax_filings",
        null=True, blank=True,
    )
    filing_reference = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Authority's return/receipt number once filed.",
    )
    filed_at = models.DateField(null=True, blank=True)
    narration = models.CharField(max_length=255, blank=True, default="")
    filing_journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="tax_filing_postings",
        null=True, blank=True,
        help_text="The netting/penalty journal posted at filing (if any).",
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "filing_status"]),
            models.Index(fields=["obligation"]),
        ]

    @property
    def balance_due(self) -> int:
        return self.amount_due - self.amount_paid

    def recompute_due(self, *, save: bool = True) -> None:
        self.amount_due = self.gross_liability - self.recoverable_amount + self.adjustment_amount
        if save:
            self.save(update_fields=["amount_due", "updated_at"])

    def refresh_payment_status(self, *, save: bool = True) -> None:
        if self.amount_paid <= 0:
            status = InvoicePaymentStatus.UNPAID
        elif self.amount_paid >= self.amount_due:
            status = InvoicePaymentStatus.PAID
        else:
            status = InvoicePaymentStatus.PARTIAL
        self.payment_status = status
        if save:
            self.save(update_fields=["payment_status", "updated_at"])

    def __str__(self) -> str:
        return f"{self.document_number or self.pk}: {self.amount_due} kobo"


class PayrollRun(FinanceDocument):
    """A batch payroll run — gross/PAYE/pension/net for many employees at once.

    Two postings (the classic payroll pair):

    * **Accrual** (:func:`vs_finance.payroll.post_payroll`):
      ``Dr salary expense (gross), Cr PAYE payable, Cr pension payable, Cr net wages
      payable`` — recognises the cost and parks each statutory/ net liability.
    * **Disbursement** (:func:`vs_finance.payroll.pay_payroll`):
      ``Dr net wages payable, Cr bank`` — clears the net-pay liability when employees
      are actually paid.
    """

    DOC_TYPE = DocType.PAYROLL_RUN

    pay_date = models.DateField(help_text="Date the run is accounted/posted on.")
    period_label = models.CharField(max_length=40, blank=True, default="", help_text="e.g. 'January 2026'.")
    currency = models.ForeignKey(
        Currency, on_delete=models.PROTECT, related_name="payroll_runs",
        null=True, blank=True,
    )
    run_status = models.CharField(
        max_length=10, choices=PayrollRunStatus.choices, default=PayrollRunStatus.DRAFT,
    )
    narration = models.CharField(max_length=255, blank=True, default="")

    salary_expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_salary_runs",
        null=True, blank=True, help_text="Defaults to 5200 Salaries & Wages.",
    )
    paye_payable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_paye_runs",
        null=True, blank=True, help_text="Defaults to 2310 PAYE Payable.",
    )
    pension_payable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_pension_runs",
        null=True, blank=True, help_text="Defaults to 2320 Pension Payable.",
    )
    net_payable_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="payroll_net_runs",
        null=True, blank=True, help_text="Defaults to 2330 Net Wages Payable.",
    )
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name="payroll_runs",
        null=True, blank=True, help_text="Cash account disbursed from at pay time.",
    )

    gross_total = MoneyField()
    paye_total = MoneyField()
    pension_total = MoneyField()
    net_total = MoneyField()

    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="payroll_accruals",
        null=True, blank=True,
    )
    disbursement_journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="payroll_disbursements",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "run_status"]),
            models.Index(fields=["entity", "pay_date"]),
        ]

    def recompute_totals(self, *, save: bool = True) -> None:
        agg = self.lines.aggregate(
            gross=models.Sum("gross_amount"), paye=models.Sum("paye_amount"),
            pension=models.Sum("pension_amount"), net=models.Sum("net_amount"),
        )
        self.gross_total = agg["gross"] or 0
        self.paye_total = agg["paye"] or 0
        self.pension_total = agg["pension"] or 0
        self.net_total = agg["net"] or 0
        if save:
            self.save(update_fields=[
                "gross_total", "paye_total", "pension_total", "net_total", "updated_at",
            ])


class PayrollLine(TimeStampedModel):
    """One employee's pay for a run. ``net = gross - paye - pension`` (all kobo)."""

    run = models.ForeignKey(
        PayrollRun, on_delete=models.CASCADE, related_name="lines",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_payroll_lines", null=True, blank=True,
    )
    employee_name = models.CharField(max_length=160, blank=True, default="")
    gross_amount = MoneyField(help_text="Gross pay in kobo.")
    paye_amount = MoneyField(help_text="PAYE (employee income tax) withheld, in kobo.")
    pension_amount = MoneyField(help_text="Employee pension contribution withheld, in kobo.")
    net_amount = MoneyField(help_text="Take-home: gross - paye - pension, in kobo.")
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="payroll_lines",
        null=True, blank=True,
    )
    line_no = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["run", "line_no", "id"]
        indexes = [models.Index(fields=["run"]), models.Index(fields=["employee"])]

    def __str__(self) -> str:
        return f"{self.employee_name or self.employee_id}: net {self.net_amount}"


class Budget(TimeStampedModel):
    """An entity's plan of GL amounts for a fiscal year, by account/cost-centre/period.

    Read-only against the ledger: a budget never posts. Budget-vs-actual
    (:func:`vs_finance.reports.budget_vs_actual`) compares each line to the
    :class:`AccountBalance` actuals. Approval **locks** the figures so the plan can't be
    quietly rewritten to flatter the variance.
    """

    entity = models.ForeignKey(
        LedgerEntity, on_delete=models.PROTECT, related_name="budgets",
    )
    fiscal_year = models.ForeignKey(
        FiscalYear, on_delete=models.PROTECT, related_name="budgets",
    )
    name = models.CharField(max_length=160)
    status = models.CharField(
        max_length=10, choices=BudgetStatus.choices, default=BudgetStatus.DRAFT,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="finance_budgets_approved", null=True, blank=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entity", "fiscal_year", "name"],
                name="uniq_finance_budget_entity_year_name",
            ),
        ]
        indexes = [models.Index(fields=["entity", "status"])]
        ordering = ["entity", "-fiscal_year__year", "name"]

    def __str__(self) -> str:
        return f"{self.name} [{self.status}]"

    @property
    def is_locked(self) -> bool:
        return self.status in (BudgetStatus.APPROVED, BudgetStatus.LOCKED)


class BudgetLine(TimeStampedModel):
    """A budgeted amount for one (account, cost-centre, period) cell of a budget."""

    budget = models.ForeignKey(
        Budget, on_delete=models.CASCADE, related_name="lines",
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="budget_lines",
    )
    cost_center = models.ForeignKey(
        CostCenter, on_delete=models.PROTECT, related_name="budget_lines",
        null=True, blank=True,
    )
    period_no = models.PositiveSmallIntegerField(help_text="1–12; the fiscal period within the year.")
    amount = MoneyField(help_text="Budgeted amount for this cell, in kobo.")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["budget", "account", "cost_center", "period_no"],
                name="uniq_finance_budgetline_cell",
            ),
        ]
        indexes = [models.Index(fields=["budget", "account"])]
        ordering = ["budget", "account", "period_no"]

    def __str__(self) -> str:
        return f"{self.account_id} P{self.period_no}: {self.amount}"


class FixedAsset(FinanceDocument):
    """A depreciable asset in the register, with a straight-line schedule.

    Acquisition optionally posts ``Dr PP&E, Cr bank/payable``. Each period's
    depreciation posts ``Dr depreciation expense, Cr accumulated depreciation`` (a
    contra-asset), driven by :class:`DepreciationSchedule` rows and fed into period
    close. ``accumulated_depreciation`` tracks the running total booked.
    """

    DOC_TYPE = DocType.FIXED_ASSET

    name = models.CharField(max_length=200)
    asset_code = models.CharField(max_length=40, blank=True, default="", help_text="Optional tag/serial.")
    asset_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="fixed_assets",
        null=True, blank=True, help_text="Capitalised cost account. Defaults to 1500 PP&E.",
    )
    accumulated_depreciation_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="fixed_assets_accum_dep",
        null=True, blank=True, help_text="Contra-asset. Defaults to 1900.",
    )
    depreciation_expense_account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="fixed_assets_dep_expense",
        null=True, blank=True, help_text="Expense account. Defaults to 5400.",
    )
    acquisition_date = models.DateField()
    cost = MoneyField(help_text="Capitalised cost in kobo.")
    salvage_value = MoneyField(help_text="Residual value at end of life, in kobo.")
    useful_life_months = models.PositiveIntegerField(help_text="Depreciable life in months.")
    method = models.CharField(
        max_length=16, choices=DepreciationMethod.choices,
        default=DepreciationMethod.STRAIGHT_LINE,
    )
    asset_status = models.CharField(
        max_length=20, choices=AssetStatus.choices, default=AssetStatus.DRAFT,
    )
    accumulated_depreciation = MoneyField(help_text="Total depreciation booked to date, in kobo.")
    acquisition_journal = models.ForeignKey(
        "JournalEntry", on_delete=models.PROTECT, related_name="asset_acquisitions",
        null=True, blank=True,
    )

    class Meta(FinanceDocument.Meta):
        indexes = [
            models.Index(fields=["entity", "asset_status"]),
            models.Index(fields=["entity", "acquisition_date"]),
        ]

    @property
    def depreciable_base(self) -> int:
        """Cost less salvage — the total to be spread over the asset's life (kobo)."""
        return max(self.cost - self.salvage_value, 0)

    @property
    def net_book_value(self) -> int:
        return self.cost - self.accumulated_depreciation


class DepreciationSchedule(TimeStampedModel):
    """One period's planned (then posted) depreciation charge for a :class:`FixedAsset`."""

    asset = models.ForeignKey(
        FixedAsset, on_delete=models.CASCADE, related_name="schedule",
    )
    seq = models.PositiveSmallIntegerField(help_text="1-based month index in the asset's life.")
    depreciation_date = models.DateField(help_text="Date this charge posts on.")
    amount = MoneyField(help_text="Depreciation for this period, in kobo.")
    is_posted = models.BooleanField(default=False)
    journal = models.ForeignKey(
        "JournalEntry", on_delete=models.SET_NULL, related_name="depreciation_charges",
        null=True, blank=True,
    )
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "seq"], name="uniq_finance_depschedule_asset_seq",
            ),
        ]
        indexes = [
            models.Index(fields=["asset", "is_posted"]),
            models.Index(fields=["depreciation_date"]),
        ]
        ordering = ["asset", "seq"]

    def __str__(self) -> str:
        return f"{self.asset_id} #{self.seq} {self.amount} {'✓' if self.is_posted else ''}".strip()
