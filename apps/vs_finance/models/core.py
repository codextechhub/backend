"""Finance foundations.

Foundational models for the finance engine (Phase 0).

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

from ..constants import (
    DocType,
    DocumentStatus,
    PLATFORM_ENTITY_CODE,
)
from ..exceptions import DocumentNumberingError



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
        from ..numbering import next_document_number  # local import avoids cycle

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


