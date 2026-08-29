"""
schools.core.fal.contracts
==========================

Framework-agnostic value objects that make up the Finance Abstraction Layer
(FAL) contract. These are the *only* types that cross the boundary between the
generic finance/payments/procurement subsystems (``vs_finance`` /
``vs_payments`` / ``vs_procurement``) and their school-specific consumers
(M9 onboarding, M11 students, M25 dashboards, M26 reports, M28 parent portal).

The FAL is deliberately **school-aware**: school vocabulary (school, student,
guardian, session, term) belongs here, because the FAL is the boundary where
school concepts meet the neutral finance engines. School words stop here. The
engines never learn them, and the engines never import this package.

Design rules enforced here:

* **No Django imports.** This module is pure Python so the contract can be
  imported, type-checked, and faked without a database or app registry. Anything
  that touches the ORM lives in ``adapters``.
* **Immutable.** Every DTO is a frozen dataclass. Consumers receive read-only
  snapshots; they cannot mutate finance state by holding a returned object.
* **Money is integer kobo.** Amounts are ``Kobo`` (a plain ``int`` alias), the
  same representation ``vs_finance.money.MoneyField`` and the ``vs_payments``
  provider layer already use (N1,250.50 == ``125050``). Raw floats and Decimals
  are never used to *carry* money across the boundary; convert at the display
  edge with ``vs_finance.money.to_naira`` if a Decimal is needed for rendering.
* **Every engine ref is an integer.** See the reference section below.
* **Availability is a value, not an exception.** Every read returns a
  :class:`FinanceResult`. A healthy read is ``AVAILABLE`` with a value; an
  upstream outage is ``UNAVAILABLE`` with a reason. A missing source is never a
  silent zero. Caller/programming errors *do* raise; see ``exceptions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Generic, Optional, TypeVar

# --------------------------------------------------------------------------- #
# Money: integer kobo, matching vs_finance.money.MoneyField and the
# vs_payments provider layer (both speak integer minor units).
# --------------------------------------------------------------------------- #
Kobo = int
"""An amount of money in integer minor units (kobo). N1,250.50 == 125050.

This mirrors ``vs_finance.money.MoneyField`` and the ``amount: int`` fields on
the ``vs_payments`` provider result dataclasses, so no rounding or
Decimal/float conversion ever happens *inside* the boundary. Convert to a
display Decimal only at the render edge (``vs_finance.money.to_naira``).
"""

CURRENCY_NGN = "NGN"

# --------------------------------------------------------------------------- #
# References.
#
# ``DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"``, and every model the
# FAL touches (Tenant, School, Branch, LedgerEntity, Customer, Invoice, Payment,
# User, WorkflowInstance, AcademicSession, AcademicTerm, the five procurement
# documents) has an integer primary key.
#
# Two ref families:
#
#   Ref      -> a platform row's primary key. A plain int.
#   LooseRef -> the opaque id of a record in a school app that does not exist in
#               the repository yet (student, guardian). Carried as a string
#               because that is exactly how the backend stores such a link:
#               ``Customer.source_type`` + ``Customer.source_id`` are plain
#               CharFields, never an FK, so the ledger stays decoupled from any
#               product app. Keeping these opaque also means the FAL never
#               guesses a primary-key type for a model nobody has written.
#
# CHANGED IN 1.1.2: ``SessionRef`` and ``TermRef`` move from LooseRef to Ref.
# v1.1.1 made them strings on the stated grounds that "there is no academic
# calendar app". There is now: ``schools.vs_academics.AcademicSession`` and
# ``AcademicTerm`` are real, tenant-scoped, integer-PK models, and the FAL lives
# inside ``apps/schools/`` so it may name them. See ``models.FeeStructureTermLink``.
# --------------------------------------------------------------------------- #
Ref = int
LooseRef = str

SchoolRef = Ref           # a schools.vs_schools School
BranchRef = Ref           # a vs_tenants Branch (the site primitive)
EntityRef = Ref           # a vs_finance LedgerEntity
CustomerRef = Ref         # a vs_finance AR Customer
InvoiceRef = Ref          # a vs_finance Invoice
PaymentRef = Ref          # a confirmed collection's booked vs_finance Payment
FeeStructureRef = Ref     # a vs_finance FeeStructure
UserRef = Ref             # a vs_user User
VendorRef = Ref           # a vs_procurement Vendor
DocRef = Ref              # a procurement document's primary key
#: CORRECTED IN 1.1.2. v1.1.1 listed ``WorkflowInstance`` among the models with
#: an integer primary key. It is not one: ``WorkflowInstance.id`` is a
#: ``CharField(primary_key=True, max_length=8)`` holding a generated short id
#: (``vs_workflow/models.py``), and ``document_object_id`` is a CharField too.
#: Typing this ref as an int would have been wrong at the first ``approve()``.
WorkflowInstanceRef = LooseRef  # a vs_workflow WorkflowInstance's short id
SessionRef = Ref          # a schools.vs_academics AcademicSession
TermRef = Ref             # a schools.vs_academics AcademicTerm

StudentRef = LooseRef     # a future school student record
GuardianRef = LooseRef    # a future parent/guardian record

#: The ``Customer.source_type`` value the FAL writes for a student. Defined once,
#: here, because it is the FAL's own value: ``Customer``'s docstring deliberately
#: names no product model ("this app must not know which products exist"), so
#: there is nothing upstream to agree with and exactly one line changes when the
#: student app settles its label.
SOURCE_TYPE_STUDENT = "vs_schools.Student"

T = TypeVar("T")


def _utcnow() -> datetime:
    """Timezone-aware 'now'. Defined locally to avoid a Django dependency."""
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Availability envelope
# --------------------------------------------------------------------------- #
class Availability(str, Enum):
    """Whether the FAL could actually produce the requested value."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class FinanceResult(Generic[T]):
    """The envelope every FAL read returns.

    * ``AVAILABLE``   -> ``value`` is populated, ``reason`` is None.
    * ``UNAVAILABLE`` -> ``value`` is None, ``reason`` explains why (e.g.
      ``"FINANCE_BACKEND_TIMEOUT"``). Consumers must render an explicit
      "unavailable" state, *not* a zero.

    ``computed_at`` is a freshness stamp. The FAL itself does no persistent
    caching; consumers (e.g. the M25 dashboard's cache layer) cache above the
    FAL and use ``computed_at`` to reason about staleness.
    """

    availability: Availability
    value: Optional[T] = None
    reason: Optional[str] = None
    computed_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def available(cls, value: T, computed_at: Optional[datetime] = None) -> "FinanceResult[T]":
        return cls(
            availability=Availability.AVAILABLE,
            value=value,
            computed_at=computed_at or _utcnow(),
        )

    @classmethod
    def unavailable(cls, reason: str, computed_at: Optional[datetime] = None) -> "FinanceResult[T]":
        return cls(
            availability=Availability.UNAVAILABLE,
            value=None,
            reason=reason,
            computed_at=computed_at or _utcnow(),
        )

    @property
    def is_available(self) -> bool:
        return self.availability is Availability.AVAILABLE

    def unwrap(self) -> T:
        """Return the value or raise if unavailable.

        Use only where the caller has *already* checked ``is_available`` or where
        an unavailable source is genuinely exceptional. The portal/dashboard
        consumers should branch on ``is_available`` and render the unavailable
        state rather than calling this.

        :raises ValueError: if the result is UNAVAILABLE.
        """
        if not self.is_available or self.value is None:
            raise ValueError(f"FinanceResult is UNAVAILABLE ({self.reason})")
        return self.value


class Unavailable:
    """Machine-readable reasons for an ``UNAVAILABLE`` result.

    Adapters should use these where they fit so consumers can branch on a stable
    vocabulary rather than free text.
    """

    BACKEND_UNAVAILABLE = "FINANCE_BACKEND_UNAVAILABLE"
    BACKEND_TIMEOUT = "FINANCE_BACKEND_TIMEOUT"
    NOT_CONFIGURED = "FINANCE_NOT_CONFIGURED"
    NOT_IMPLEMENTED = "FINANCE_NOT_IMPLEMENTED"
    GATEWAY_UNAVAILABLE = "PAYMENT_GATEWAY_UNAVAILABLE"
    TERM_NOT_LINKED = "FEE_TERM_NOT_LINKED"


# --------------------------------------------------------------------------- #
# Scoping / query helpers
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Period:
    """An academic period to scope an aggregate by (session, optionally term).

    ``session_ref``/``term_ref`` are the primary keys of
    ``schools.vs_academics.AcademicSession`` / ``AcademicTerm``. The Django
    adapter maps them onto ``vs_finance`` scoping through the FAL-owned
    :class:`~schools.core.fal.models.FeeStructureTermLink`: the linked fee
    structures give the invoice ``reference`` values (``FEE:<code>``) that belong
    to that period.
    """

    session_ref: SessionRef
    term_ref: Optional[TermRef] = None


@dataclass(frozen=True)
class DateRange:
    """A calendar range for trend series (inclusive of both ends)."""

    start: date
    end: date


@dataclass(frozen=True)
class FilterClause:
    """A single filter for paged reads (report sources, debtor lists).

    ``field`` is validated by the adapter against the columns the source
    actually exposes. ``op`` is one of ``eq``, ``in``, ``gte``, ``lte``,
    ``contains``. The adapter builds ORM filters from these; it never
    interpolates ``field``/``value`` into raw SQL.
    """

    field: str
    op: str
    value: object


@dataclass(frozen=True)
class Page(Generic[T]):
    """A page of rows, mirroring the shape of ``core.pagination.XVSPagination``.

    CORRECTED IN 1.1.2: v1.1.1 warned consumers that an empty page serialises as
    ``{}`` rather than ``[]`` because ``core.response.success_response`` coerced a
    falsy payload. It no longer does - only a genuinely absent payload becomes
    ``{}``, and an empty list stays a list (``apps/core/response.py:23``). A
    consumer doing ``data.map(...)`` on an empty result is safe.
    """

    items: tuple[T, ...]
    page: int
    page_size: int
    total_items: int
    total_pages: int


# --------------------------------------------------------------------------- #
# KPI / aggregate value objects (consumed mainly by M25)
# --------------------------------------------------------------------------- #
class Unit(str, Enum):
    KOBO = "KOBO"        # money, integer minor units
    RATIO = "ratio"      # 0..1
    PERCENT = "percent"  # 0..100
    COUNT = "count"


@dataclass(frozen=True)
class KpiValue:
    """A single headline metric.

    ``value`` is interpreted via ``unit``. For ``KOBO`` it is integer kobo; for
    ``RATIO``/``PERCENT`` it is a scaled integer per ``scale`` (default: the raw
    integer, e.g. a ratio expressed in basis points when ``scale=10000``) so the
    whole contract stays integer-only and float-free. ``comparison`` is the
    signed delta versus the previous comparable period, when one is available.
    """

    value: int
    unit: Unit
    scale: int = 1
    comparison: Optional[int] = None
    label: Optional[str] = None


@dataclass(frozen=True)
class SeriesPoint:
    label: str
    value: int


@dataclass(frozen=True)
class Series:
    """An ordered set of points for a trend chart (e.g. payment volume by week)."""

    points: tuple[SeriesPoint, ...]
    unit: Unit = Unit.KOBO


# --------------------------------------------------------------------------- #
# Detail / list rows
# --------------------------------------------------------------------------- #
class AgeingBucket(str, Enum):
    CURRENT = "CURRENT"
    DAYS_1_30 = "DAYS_1_30"
    DAYS_31_60 = "DAYS_31_60"
    DAYS_61_90 = "DAYS_61_90"
    DAYS_90_PLUS = "DAYS_90_PLUS"


@dataclass(frozen=True)
class DebtorRow:
    """One debtor in the dashboard drill-down / report 'debtors' source.

    ``branch_ref`` is optional because a customer's branch is nullable and a
    school-wide receivable is a first-class case.

    ``student_name`` is the AR customer's name and ``class_label`` is empty until
    a student app exists to be asked: the FAL will not invent a class it cannot
    read. Both are documented as such rather than omitted, because M25/M26 have
    columns for them.
    """

    student_ref: StudentRef
    student_name: str
    class_label: str
    outstanding: Kobo
    ageing: AgeingBucket
    branch_ref: Optional[BranchRef] = None


class InvoiceStatus(str, Enum):
    """Mirrors ``vs_finance.constants.InvoicePaymentStatus`` (the cash axis).

    The middle value is ``PARTIAL``. This is distinct from the document lifecycle
    (DRAFT / POSTED / CANCELLED), which the FAL does not expose.
    """

    UNPAID = "UNPAID"
    PARTIAL = "PARTIAL"
    PAID = "PAID"


@dataclass(frozen=True)
class FeeRow:
    """One invoice-level row for the report 'fees' source."""

    invoice_ref: InvoiceRef
    student_ref: StudentRef
    student_name: str
    class_label: str
    term_label: str
    amount_due: Kobo
    amount_paid: Kobo
    balance: Kobo
    status: InvoiceStatus


class PaymentMethod(str, Enum):
    """Mirrors the subset of ``vs_finance.constants.PaymentMethod`` a portal shows."""

    CARD = "CARD"
    TRANSFER = "TRANSFER"
    USSD = "USSD"
    CASH = "CASH"
    ONLINE = "ONLINE"
    OTHER = "OTHER"


@dataclass(frozen=True)
class PaymentRow:
    """One transaction-level row for the report 'payments' source."""

    payment_ref: PaymentRef
    student_ref: StudentRef
    student_name: str
    amount: Kobo
    method: PaymentMethod
    paid_at: datetime
    reconciled: bool
    gateway_ref: Optional[str] = None


# --------------------------------------------------------------------------- #
# Aggregate dashboard contracts (M25) - component 5.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AgeingRow:
    """One ageing bucket's total, for the AR-ageing dashboard contract."""

    bucket: AgeingBucket
    total: Kobo
    debtor_count: int


@dataclass(frozen=True)
class ArAgeingReport:
    """AR ageing broken down by bucket, for one school/term scope."""

    school_ref: SchoolRef
    period: Optional[Period]
    buckets: tuple[AgeingRow, ...]
    total_outstanding: Kobo


@dataclass(frozen=True)
class FeeLiability:
    """Total billed vs collected vs outstanding for a term (fee liability)."""

    school_ref: SchoolRef
    period: Optional[Period]
    total_billed: Kobo
    total_collected: Kobo
    total_outstanding: Kobo


# --------------------------------------------------------------------------- #
# Per-entity fee views (student & parent portals - M11/M28)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class InvoiceLine:
    description: str
    amount: Kobo


@dataclass(frozen=True)
class InvoiceView:
    """A single invoice with its line items, for the parent portal."""

    invoice_ref: InvoiceRef
    student_ref: StudentRef
    term_label: str
    lines: tuple[InvoiceLine, ...]
    amount_due: Kobo
    amount_paid: Kobo
    balance: Kobo
    status: InvoiceStatus


@dataclass(frozen=True)
class FeeStatus:
    """A student's read-only fee position, for the student portal (M11)."""

    student_ref: StudentRef
    balance: Kobo
    total_billed: Kobo
    total_paid: Kobo
    invoices: tuple[InvoiceView, ...]


@dataclass(frozen=True)
class Receipt:
    """A downloadable receipt reference for a confirmed payment (parent portal)."""

    payment_ref: PaymentRef
    receipt_number: str
    amount: Kobo
    issued_at: datetime
    invoice_refs: tuple[InvoiceRef, ...]


# --------------------------------------------------------------------------- #
# Procurement views (M25 dashboards, M26 reports)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ProcurementSnapshot:
    open_requests: int
    pending_approvals: int
    spend: Kobo


@dataclass(frozen=True)
class ProcurementRow:
    request_ref: DocRef
    title: str
    status: str
    amount: Kobo
    raised_at: datetime
    branch_ref: Optional[BranchRef] = None


# --------------------------------------------------------------------------- #
# Component 1 - School -> Entity resolution / provisioning.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EntityHandle:
    """A resolved LedgerEntity for a school, as the FAL exposes it.

    ``entity_ref`` is the entity's primary key; ``code`` is the entity's human
    code (e.g. ``LEKKI``) that appears inside document numbers and is unique
    platform-wide. ``was_created`` distinguishes a fresh provision from an
    idempotent hit on a retried onboarding.

    Decision (2026-07-04): every school has exactly **one primary** entity,
    provisioned by M9; ``resolve_entity`` returns it (``is_primary=True``). Two
    candidate primaries raise ``AmbiguousPrimaryEntity``; the FAL never guesses.

    ``is_primary`` is **not** backed by a model field. ``LedgerEntity`` has no
    ``is_primary`` column and no constraint expressing "one per tenant", and its
    docstring explicitly permits several entities per tenant. The rule is a
    FAL-boundary convention: the adapter selects the active tenant-kind entities
    for the school's tenant, takes two, and raises if it gets two.
    """

    entity_ref: EntityRef
    school_ref: SchoolRef
    code: str
    name: str
    base_currency: str = CURRENCY_NGN
    was_created: bool = False
    is_primary: bool = True


# --------------------------------------------------------------------------- #
# Component 2 - Fee structure <-> term bridge + cohort billing.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FeeTermLink:
    """A link between a FeeStructure and an academic session/term."""

    fee_structure_ref: FeeStructureRef
    session_ref: SessionRef
    term_ref: Optional[TermRef]
    entity_ref: EntityRef
    session_label: str = ""
    term_label: str = ""


@dataclass(frozen=True)
class InvoiceGenerationResult:
    """Outcome of generating invoices for a student cohort from a fee structure."""

    fee_structure_ref: FeeStructureRef
    period: Period
    invoices_created: tuple[InvoiceRef, ...]
    students_skipped: tuple[StudentRef, ...]   # already billed (idempotent skip)
    total_billed: Kobo


# --------------------------------------------------------------------------- #
# Component 3 - Student -> Customer resolution.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CustomerHandle:
    """A resolved AR Customer for a student.

    The backend links a Customer to its domain record via the loose
    ``source_type``/``source_id`` strings (``SOURCE_TYPE_STUDENT`` plus the
    student's id), never an FK, and enforces uniqueness on ``(entity, code)``
    (``uniq_finance_customer_entity_code``). ``was_created`` distinguishes a
    first-billing provision from an idempotent hit.
    """

    customer_ref: CustomerRef
    student_ref: StudentRef
    entity_ref: EntityRef
    code: str
    was_created: bool = False


# --------------------------------------------------------------------------- #
# Component 7 - Procurement actions (school-facing procurement through the FAL).
#
# These DTOs describe *what the caller asked for* and *what the underlying
# vs_procurement service did*. They deliberately carry no procurement business
# logic: the FAL is a thin pass-through (tenancy, branch scoping, permission and
# error translation) over vs_procurement's own services.
# --------------------------------------------------------------------------- #
class ProcDocType(str, Enum):
    """The procurement documents the FAL exposes actions for.

    Mirrors the ``FinanceDocument`` subclasses in ``vs_procurement.models``:
    ``PurchaseRequisition``, ``PurchaseOrder``, ``GoodsReceivedNote``,
    ``VendorInvoice``, ``VendorPayment``.
    """

    REQUISITION = "REQUISITION"
    PURCHASE_ORDER = "PURCHASE_ORDER"
    GOODS_RECEIPT = "GOODS_RECEIPT"
    VENDOR_INVOICE = "VENDOR_INVOICE"
    VENDOR_PAYMENT = "VENDOR_PAYMENT"


class ProcApprovalState(str, Enum):
    """Mirrors ``vs_procurement.constants.ProcApprovalState`` exactly.

    NOTE (decision 3): these four values **cannot** express "approved via
    override". The marker is a separate persisted row; see
    :class:`ApprovalOverride`.
    """

    NOT_SUBMITTED = "NOT_SUBMITTED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class ProcDocRef:
    """A reference to one procurement document.

    ``branch_ref`` is **legitimately optional**: an empty branch on a
    school-level user's document means a *head-office purchase by that person*,
    not an error (decision 4).
    """

    doc_ref: DocRef
    doc_type: ProcDocType
    entity_ref: EntityRef
    branch_ref: Optional[BranchRef] = None


@dataclass(frozen=True)
class ProcDocument:
    """A procurement document as the FAL exposes it (read/return shape).

    ``approved_by_override`` matches the field the procurement API already
    returns; the FAL must not invent a second name for a flag the frontend
    already consumes.
    """

    ref: ProcDocRef
    document_number: str
    status: str                      # ledger lifecycle: DRAFT/POSTED/...
    approval_state: ProcApprovalState
    total: Kobo
    vendor_ref: Optional[VendorRef] = None
    raised_by_ref: Optional[UserRef] = None
    approved_by_override: bool = False   # decision 3 marker


@dataclass(frozen=True)
class ApprovalOverride:
    """The audited record of an approval override (decision 3).

    Mirrors the shipped ``vs_procurement.models.ApprovalOverride``, an
    append-only side table whose ``save()`` refuses updates.

    ``amount`` is the document's approval amount **at the moment of the
    override**, copied rather than joined: the released document may legitimately
    change value afterwards, and the question an auditor asks is how much this
    person waved through.
    """

    doc_ref: DocRef
    doc_type: ProcDocType
    actor_ref: UserRef
    reason: str
    amount: Kobo
    overridden_at: datetime
    stage_code: str = ""


@dataclass(frozen=True)
class ApprovalSubmission:
    """Outcome of submitting a procurement document for approval.

    Decision 2 ("approval blocks, never skips") is realised as **park, don't
    skip**, and it is shipped behaviour: the seeded stages carry
    ``skip_if_no_approvers=False``, so a stage with no eligible approver is
    *activated and held* rather than auto-skipped to APPROVED. Submission
    therefore **succeeds**, and this DTO reports which of the two live states
    resulted:

    * ``is_parked=False`` - PENDING with real approvers; it will progress.
    * ``is_parked=True``  - PENDING but held on ``parked_stage_code``, which
      nobody can currently action. It releases **automatically** as soon as
      somebody is granted the approving permission, with no resubmission
      (``vs_procurement.approval_parking``). This is the expected state for a
      freshly onboarded school (decision 5).

    The only hard refusal on this path is a missing template
    (``ApprovalTemplateMissingError``), which persists nothing.

    ``override`` is populated only by the approve-without-review path, which
    acts on an already-parked document.
    """

    doc_ref: DocRef
    doc_type: ProcDocType
    approval_state: ProcApprovalState
    workflow_instance_ref: Optional[WorkflowInstanceRef] = None
    is_parked: bool = False
    parked_stage_code: Optional[str] = None
    override: Optional[ApprovalOverride] = None
    was_idempotent_replay: bool = False


@dataclass(frozen=True)
class ApprovalDecision:
    """Outcome of an approve/decline action on a procurement document."""

    doc_ref: DocRef
    doc_type: ProcDocType
    approval_state: ProcApprovalState
    decided_by_ref: UserRef
    decided_at: datetime
    comment: str = ""


@dataclass(frozen=True)
class ReceiptLine:
    """One received line on a goods receipt (quantities, not money)."""

    po_line_ref: Ref
    quantity_received: int


@dataclass(frozen=True)
class BillLine:
    """One line of a supplier bill (vendor invoice). Amounts in kobo."""

    description: str
    quantity: int
    unit_price: Kobo
    po_line_ref: Optional[Ref] = None


# =========================================================================== #
# v1.2 (DEFERRED) - payment application DTOs.
#
# Decision (2026-07-04): PaymentPort/apply_payment is NOT part of the v1.1.x
# surface. Settlement remains vs_payments' own confirm_collection ->
# _book_receipt -> post_payment flow. These DTOs are retained only as the
# starting point for the v1.2 refactor (confirm_collection delegating to
# apply_payment; combined-family allocation) and are NOT exported from the
# package.
#
# NOTE: "v1.2" here names a scope milestone, not this contract's version.
# =========================================================================== #
@dataclass(frozen=True)
class Allocation:
    """v1.2 (deferred): a slice of a payment applied to one invoice/child."""

    invoice_ref: InvoiceRef
    student_ref: StudentRef
    amount: Kobo


@dataclass(frozen=True)
class ApplyPaymentCommand:
    """v1.2 (deferred): settle a confirmed collection against invoices."""

    payment_ref: PaymentRef
    payer_ref: GuardianRef
    school_ref: SchoolRef
    entity_ref: EntityRef
    amount: Kobo
    allocations: tuple[Allocation, ...]
    confirmed_at: datetime


@dataclass(frozen=True)
class AppliedInvoice:
    invoice_ref: InvoiceRef
    amount_applied: Kobo
    resulting_balance: Kobo
    resulting_status: InvoiceStatus


@dataclass(frozen=True)
class PaymentApplication:
    """v1.2 (deferred): the outcome of applying a payment, a receipt's basis."""

    application_ref: Ref
    payment_ref: PaymentRef
    applied_at: datetime
    applied: tuple[AppliedInvoice, ...]
    was_idempotent_replay: bool = False
