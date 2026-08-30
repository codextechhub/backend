"""
schools.core.fal.testing
========================

Dependency-free in-memory fakes for every port, so a consuming module can be
tested without a database, a settings module, or a real finance backend.

The fakes are not toys. Each one reproduces the *behaviour a consumer must
handle*, which is mostly the awkward behaviour: provisioning is idempotent,
first billing can race, a submitted document can park, an override can be
refused three different ways, and a reader can be unavailable rather than zero.
A consumer that passes against these fakes and then fails against the Django
adapters has usually been written against the happy path only.

Usage::

    from schools.core.fal import registry
    from schools.core.fal.testing import FakeFinanceReader, unavailable_finance_reader

    registry.set_finance_reader(FakeFinanceReader(outstanding=120000))  # kobo
    # ... assert the dashboard renders N1,200.00 ...
    registry.set_finance_reader(unavailable_finance_reader())
    # ... assert the dashboard renders the 'unavailable' state, NOT zero ...
    registry.reset()

``FakePaymentPort`` is deliberately absent: ``PaymentPort`` is v1.2 (deferred),
so there is nothing for a consumer to test against.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from typing import Optional

from .contracts import (
    AgeingBucket,
    AgeingRow,
    ApprovalDecision,
    ApprovalOverride,
    ApprovalSubmission,
    ArAgeingReport,
    CustomerHandle,
    EntityHandle,
    FeeLiability,
    FeeStatus,
    FeeTermLink,
    FinanceResult,
    InvoiceGenerationResult,
    KpiValue,
    Page,
    Period,
    ProcApprovalState,
    ProcDocRef,
    ProcDocType,
    ProcDocument,
    ProcurementSnapshot,
    Series,
    Unavailable,
    Unit,
)
from .exceptions import (
    AmbiguousPrimaryEntity,
    ApprovalNotParkedError,
    ApprovalTemplateMissingError,
    CrossBranchError,
    CrossTenantError,
    CustomerCreationRace,
    EntityNotProvisioned,
    GuardianLinkNotConfigured,
    OverrideNotPermittedError,
    PaymentGatewayError,
    ProcurementStateError,
    TermNotLinkedError,
)
from .ports import (
    EntityResolverPort,
    FeeTermBridgePort,
    FinanceRbacPort,
    FinanceReadPort,
    GuardianLinkPort,
    ParentPaymentBridgePort,
    ProcurementActionPort,
    ProcurementReadPort,
    StudentCustomerPort,
)

_ids = itertools.count(1000)


def _next_id() -> int:
    return next(_ids)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _page(items: tuple, page: int, page_size: int) -> Page:
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    start = (page - 1) * page_size
    total = len(items)
    return Page(
        items=items[start:start + page_size],
        page=page, page_size=page_size, total_items=total,
        total_pages=max((total + page_size - 1) // page_size, 1),
    )


def _ok(value):
    return FinanceResult.available(value)


# --------------------------------------------------------------------------- #
# Component 1
# --------------------------------------------------------------------------- #
class FakeEntityResolver(EntityResolverPort):
    """Idempotent in-memory provisioning.

    ``ambiguous_schools`` makes ``resolve_entity`` raise
    ``AmbiguousPrimaryEntity`` for those school refs, so a consumer can prove it
    surfaces the two-candidate data fault instead of silently picking a set of
    books.
    """

    def __init__(self, *, entities: Optional[dict] = None,
                 ambiguous_schools: Optional[set] = None) -> None:
        self.entities: dict = dict(entities or {})
        self.ambiguous_schools = set(ambiguous_schools or ())

    def provision_entity(self, school_ref, *, code, name, base_currency="NGN"):
        if school_ref in self.ambiguous_schools:
            raise AmbiguousPrimaryEntity(f"School {school_ref!r} has two candidates.")
        existing = self.entities.get(school_ref)
        if existing is not None:
            return _ok(EntityHandle(
                entity_ref=existing.entity_ref, school_ref=school_ref,
                code=existing.code, name=existing.name,
                base_currency=existing.base_currency, was_created=False,
            ))
        clash = next(
            (s for s, h in self.entities.items() if h.code == code), None,
        )
        if clash is not None:
            raise CrossTenantError(f"Entity code {code!r} belongs to school {clash!r}.")
        handle = EntityHandle(
            entity_ref=_next_id(), school_ref=school_ref, code=code, name=name,
            base_currency=base_currency, was_created=True,
        )
        self.entities[school_ref] = handle
        return _ok(handle)

    def resolve_entity(self, school_ref):
        if school_ref in self.ambiguous_schools:
            raise AmbiguousPrimaryEntity(f"School {school_ref!r} has two candidates.")
        handle = self.entities.get(school_ref)
        if handle is None:
            raise EntityNotProvisioned(f"School {school_ref!r} has no entity.")
        return _ok(EntityHandle(
            entity_ref=handle.entity_ref, school_ref=school_ref, code=handle.code,
            name=handle.name, base_currency=handle.base_currency, was_created=False,
        ))


# --------------------------------------------------------------------------- #
# Component 2
# --------------------------------------------------------------------------- #
class FakeFeeTermBridge(FeeTermBridgePort):
    def __init__(self, *, entity_ref: int = 1) -> None:
        self.links: dict = {}
        self.billed: dict = {}
        self.entity_ref = entity_ref

    def link_term(self, fee_structure_ref, session_ref, term_ref=None):
        link = FeeTermLink(
            fee_structure_ref=fee_structure_ref, session_ref=session_ref,
            term_ref=term_ref, entity_ref=self.entity_ref,
        )
        self.links[fee_structure_ref] = link
        return _ok(link)

    def generate_cohort_invoices(self, fee_structure_ref, student_refs, *, period=None):
        link = self.links.get(fee_structure_ref)
        if link is None:
            raise TermNotLinkedError(f"Structure {fee_structure_ref!r} has no term.")
        already = self.billed.setdefault(fee_structure_ref, set())
        created, skipped = [], []
        for ref in student_refs:
            if ref in already:
                skipped.append(ref)
                continue
            already.add(ref)
            created.append(_next_id())
        return _ok(InvoiceGenerationResult(
            fee_structure_ref=fee_structure_ref,
            period=period or Period(session_ref=link.session_ref, term_ref=link.term_ref),
            invoices_created=tuple(created),
            students_skipped=tuple(skipped),
            total_billed=len(created) * 100000,
        ))


# --------------------------------------------------------------------------- #
# Component 3
# --------------------------------------------------------------------------- #
class FakeStudentCustomer(StudentCustomerPort):
    """``fail_race_once=True`` simulates the unrecoverable first-billing race."""

    def __init__(self, *, fail_race_once: bool = False) -> None:
        self.customers: dict = {}
        self._fail_race_once = fail_race_once

    def ensure_customer(self, student_ref, *, entity_ref, name=None, code=None,
                        branch_ref=None):
        key = (entity_ref, student_ref)
        existing = self.customers.get(key)
        if existing is not None:
            return _ok(CustomerHandle(
                customer_ref=existing.customer_ref, student_ref=student_ref,
                entity_ref=entity_ref, code=existing.code, was_created=False,
            ))
        if self._fail_race_once:
            self._fail_race_once = False
            raise CustomerCreationRace(f"Race creating a customer for {student_ref!r}.")
        handle = CustomerHandle(
            customer_ref=_next_id(), student_ref=student_ref, entity_ref=entity_ref,
            code=code or f"CU-{student_ref}", was_created=True,
        )
        self.customers[key] = handle
        return _ok(handle)

    def customer_for(self, student_ref, *, entity_ref):
        return _ok(self.customers.get((entity_ref, student_ref)))


# --------------------------------------------------------------------------- #
# Component 4
# --------------------------------------------------------------------------- #
class FakeFinanceRbac(FinanceRbacPort):
    """``granted`` holds ``(user_ref, permission_key, entity_ref)`` triples.

    Scoping by entity is the point: a key granted in one school's entity answers
    ``False`` in another's, which is the mistake this port exists to prevent.
    """

    def __init__(self, *, granted: Optional[set] = None) -> None:
        self.granted = set(granted or ())

    def can(self, user_ref, permission_key, *, entity_ref, branch_ref=None):
        return _ok((user_ref, permission_key, entity_ref) in self.granted)


# --------------------------------------------------------------------------- #
# Component 5
# --------------------------------------------------------------------------- #
class FakeFinanceReader(FinanceReadPort):
    def __init__(self, *, collections: int = 0, outstanding: int = 0,
                 collection_rate_bps: int = 0, debtor_count: int = 0,
                 series: Series = None, ageing: tuple = (), fee_liability=None,
                 debtors: tuple = (), fee_invoices: tuple = (), payments: tuple = (),
                 fee_status: Optional[FeeStatus] = None, invoices: tuple = (),
                 balances: Optional[dict] = None,
                 student_schools: Optional[dict] = None) -> None:
        self._collections = collections
        self._outstanding = outstanding
        self._rate = collection_rate_bps
        self._debtors_count = debtor_count
        self._series = series or Series(points=())
        self._ageing = ageing
        self._fee_liability = fee_liability
        self._debtor_rows = debtors
        self._fee_rows = fee_invoices
        self._payment_rows = payments
        self._fee_status = fee_status
        self._invoices = invoices
        self._balances = dict(balances or {})
        #: student_ref -> school_ref, so combined_balance can refuse a mixed set
        #: exactly as the real adapter does.
        self._student_schools = dict(student_schools or {})

    def collections(self, school_ref, branch_ref=None, period=None):
        return _ok(KpiValue(value=self._collections, unit=Unit.KOBO))

    def outstanding(self, school_ref, branch_ref=None, period=None):
        return _ok(KpiValue(value=self._outstanding, unit=Unit.KOBO))

    def collection_rate(self, school_ref, branch_ref=None, period=None):
        return _ok(KpiValue(value=self._rate, unit=Unit.RATIO, scale=10000))

    def debtor_count(self, school_ref, branch_ref=None, period=None):
        return _ok(KpiValue(value=self._debtors_count, unit=Unit.COUNT))

    def payment_trend(self, school_ref, branch_ref=None, date_range=None):
        return _ok(self._series)

    def ar_ageing(self, school_ref, branch_ref=None, period=None):
        buckets = self._ageing or tuple(
            AgeingRow(bucket=b, total=0, debtor_count=0) for b in AgeingBucket
        )
        return _ok(ArAgeingReport(
            school_ref=school_ref, period=period, buckets=buckets,
            total_outstanding=sum(b.total for b in buckets),
        ))

    def fee_liability(self, school_ref, period=None):
        return _ok(self._fee_liability or FeeLiability(
            school_ref=school_ref, period=period, total_billed=0,
            total_collected=0, total_outstanding=0,
        ))

    def debtors(self, school_ref, branch_ref=None, filters=(), page=1, page_size=20):
        return _ok(_page(self._debtor_rows, page, page_size))

    def fee_invoices(self, school_ref, branch_ref=None, filters=(), page=1, page_size=20):
        return _ok(_page(self._fee_rows, page, page_size))

    def payments(self, school_ref, branch_ref=None, filters=(), page=1, page_size=20):
        return _ok(_page(self._payment_rows, page, page_size))

    def fee_status(self, student_ref):
        return _ok(self._fee_status or FeeStatus(
            student_ref=student_ref, balance=self._balances.get(student_ref, 0),
            total_billed=0, total_paid=0, invoices=(),
        ))

    def invoices_for(self, student_ref, include_history=True):
        return _ok(self._invoices)

    def combined_balance(self, student_refs):
        schools = {
            self._student_schools[ref]
            for ref in student_refs if ref in self._student_schools
        }
        if len(schools) > 1:
            raise CrossTenantError("These children are at different schools.")
        return _ok(sum(self._balances.get(ref, 0) for ref in student_refs))


# --------------------------------------------------------------------------- #
# Guardian link
# --------------------------------------------------------------------------- #
class FakeGuardianLink(GuardianLinkPort):
    """``links`` maps a guardian ref to the student refs they may act for."""

    def __init__(self, links: Optional[dict] = None) -> None:
        self.links = {k: set(v) for k, v in (links or {}).items()}

    def owns(self, guardian_ref, student_ref):
        return student_ref in self.links.get(guardian_ref, set())


class UnconfiguredGuardianLink(GuardianLinkPort):
    """Mirrors the shipped default: it cannot answer, and refuses to guess."""

    def owns(self, guardian_ref, student_ref):
        raise GuardianLinkNotConfigured("No guardian-to-student link is wired.")


# --------------------------------------------------------------------------- #
# Component 6
# --------------------------------------------------------------------------- #
class FakeParentPaymentBridge(ParentPaymentBridgePort):
    """``reject=True`` reproduces a deterministic gateway refusal."""

    def __init__(self, *, checkout_url: str = "https://pay.example/checkout/FAKE",
                 guardian_link: Optional[GuardianLinkPort] = None,
                 receipts: Optional[dict] = None, reject: bool = False) -> None:
        self.checkout_url = checkout_url
        self.guardian_link = guardian_link or FakeGuardianLink()
        self.receipts: dict = dict(receipts or {})
        self.reject = reject
        self.sessions: list = []
        #: payment_ref -> student_ref, for the receipt ownership check.
        self.payment_students: dict = {}

    def start_payment_session(self, *, guardian_ref, entity_ref, amount,
                              customer_ref=None, invoice_ref=None,
                              payer_email="", callback_url=""):
        if customer_ref is None and invoice_ref is None:
            raise PaymentGatewayError("A customer or an invoice is required.")
        if self.reject:
            raise PaymentGatewayError("The gateway refused this request.")
        self.sessions.append((guardian_ref, entity_ref, amount, customer_ref, invoice_ref))
        return _ok(self.checkout_url)

    def receipt_for(self, payment_ref, *, guardian_ref):
        receipt = self.receipts.get(payment_ref)
        if receipt is None:
            return _ok(None)
        student_ref = self.payment_students.get(payment_ref)
        if student_ref is not None and not self.guardian_link.owns(guardian_ref, student_ref):
            raise CrossTenantError("That payment is not this guardian's to see.")
        return _ok(receipt)


# --------------------------------------------------------------------------- #
# Component 7
# --------------------------------------------------------------------------- #
class FakeProcurementActions(ProcurementActionPort):
    """Every component-7 path, without a database.

    The four dictionaries are the configuration a real school has:

    * ``seeded_entities``     - entities with an approval ladder. Absent means
      ``ApprovalTemplateMissingError`` on submission, and nothing is recorded.
    * ``entities_with_approver`` - entities where somebody actually holds the
      approving role. Absent means submission **parks**: PENDING,
      ``is_parked=True``, and never APPROVED.
    * ``override_users``      - who holds ``procurement.approval.override``.
      Absent means ``OverrideNotPermittedError``.
    * ``user_branches``       - user ref to branch ref, for branch defaulting and
      ``CrossBranchError``. A user absent from the map is school-level, and their
      documents legitimately carry no branch.

    ``grant_approver(entity_ref)`` proves the release-without-resubmission rule:
    a parked document becomes unparked the moment the role is filled.
    ``.overrides`` stands in for the append-only ``ApprovalOverride`` rows.
    """

    def __init__(self, *, seeded_entities: Optional[set] = None,
                 entities_with_approver: Optional[set] = None,
                 override_users: Optional[set] = None,
                 user_branches: Optional[dict] = None) -> None:
        self.seeded_entities = set(seeded_entities or ())
        self.entities_with_approver = set(entities_with_approver or ())
        self.override_users = set(override_users or ())
        self.user_branches = dict(user_branches or {})
        self.documents: dict = {}
        self.overrides: list = []
        self._parked: dict = {}

    # -- helpers -- #
    def grant_approver(self, entity_ref) -> None:
        """Fill the approving role, and release everything parked on it."""
        self.entities_with_approver.add(entity_ref)
        for doc_ref, entity in list(self._parked.items()):
            if entity == entity_ref:
                del self._parked[doc_ref]

    def is_parked(self, doc_ref) -> bool:
        return doc_ref in self._parked

    def _branch_for(self, actor_ref, entity_ref, branch_ref):
        caller = self.user_branches.get(actor_ref)
        if branch_ref is None:
            return caller
        if caller is not None and caller != branch_ref:
            raise CrossBranchError("That user cannot act for another branch.")
        return branch_ref

    def _get(self, doc: ProcDocRef) -> ProcDocument:
        document = self.documents.get(doc.doc_ref)
        if document is None or document.ref.entity_ref != doc.entity_ref:
            raise CrossTenantError(f"Document {doc.doc_ref!r} is not in this entity.")
        if doc.branch_ref is not None and document.ref.branch_ref != doc.branch_ref:
            raise CrossBranchError(f"Document {doc.doc_ref!r} is in another branch.")
        return document

    def _store(self, document: ProcDocument) -> ProcDocument:
        self.documents[document.ref.doc_ref] = document
        return document

    def _new(self, doc_type, entity_ref, branch_ref, total, raised_by=None,
             vendor_ref=None, status="DRAFT"):
        return self._store(ProcDocument(
            ref=ProcDocRef(doc_ref=_next_id(), doc_type=doc_type,
                           entity_ref=entity_ref, branch_ref=branch_ref),
            document_number=f"FAKE-{doc_type.value}",
            status=status,
            approval_state=ProcApprovalState.NOT_SUBMITTED,
            total=total, vendor_ref=vendor_ref, raised_by_ref=raised_by,
        ))

    # -- raise -- #
    def raise_requisition(self, *, entity_ref, raiser_ref, lines, branch_ref=None,
                          narration=""):
        if not lines:
            raise ProcurementStateError("A requisition needs at least one line.")
        branch = self._branch_for(raiser_ref, entity_ref, branch_ref)
        total = sum(line.quantity * line.unit_price for line in lines)
        return _ok(self._new(ProcDocType.REQUISITION, entity_ref, branch, total,
                             raised_by=raiser_ref))

    # -- approval -- #
    def submit_for_approval(self, doc, *, actor_ref):
        document = self._get(doc)
        if document.approval_state in (ProcApprovalState.PENDING,
                                       ProcApprovalState.APPROVED):
            raise ProcurementStateError(
                f"Document {doc.doc_ref!r} is already {document.approval_state.value}."
            )
        if document.ref.entity_ref not in self.seeded_entities:
            # Nothing is persisted on this path, exactly as the engine's atomic
            # block rolls the PENDING flip back.
            raise ApprovalTemplateMissingError(
                f"Entity {document.ref.entity_ref!r} has no approval rules."
            )
        staffed = document.ref.entity_ref in self.entities_with_approver
        self._store(ProcDocument(
            ref=document.ref, document_number=document.document_number,
            status=document.status, approval_state=ProcApprovalState.PENDING,
            total=document.total, vendor_ref=document.vendor_ref,
            raised_by_ref=document.raised_by_ref,
            approved_by_override=document.approved_by_override,
        ))
        if not staffed:
            self._parked[doc.doc_ref] = document.ref.entity_ref
        return _ok(ApprovalSubmission(
            doc_ref=doc.doc_ref, doc_type=doc.doc_type,
            approval_state=ProcApprovalState.PENDING,
            workflow_instance_ref=f"wf{doc.doc_ref}",
            is_parked=not staffed,
            parked_stage_code=None if staffed else "manager",
        ))

    def approve_without_review(self, doc, *, actor_ref, reason):
        document = self._get(doc)
        if actor_ref not in self.override_users:
            raise OverrideNotPermittedError("That user may not override an approval.")
        if not (reason or "").strip():
            raise OverrideNotPermittedError("An override needs a typed reason.")
        if doc.doc_ref not in self._parked:
            raise ApprovalNotParkedError(
                f"Document {doc.doc_ref!r} is not parked, so it must go to its approvers."
            )
        del self._parked[doc.doc_ref]
        override = ApprovalOverride(
            doc_ref=doc.doc_ref, doc_type=doc.doc_type, actor_ref=actor_ref,
            reason=reason.strip(), amount=document.total, overridden_at=_utcnow(),
            stage_code="manager",
        )
        self.overrides.append(override)
        self._store(ProcDocument(
            ref=document.ref, document_number=document.document_number,
            status=document.status, approval_state=ProcApprovalState.APPROVED,
            total=document.total, vendor_ref=document.vendor_ref,
            raised_by_ref=document.raised_by_ref, approved_by_override=True,
        ))
        return _ok(ApprovalSubmission(
            doc_ref=doc.doc_ref, doc_type=doc.doc_type,
            approval_state=ProcApprovalState.APPROVED,
            workflow_instance_ref=f"wf{doc.doc_ref}",
            is_parked=False, override=override,
        ))

    def approve(self, doc, *, approver_ref, comment=""):
        return self._decide(doc, approver_ref, comment, ProcApprovalState.APPROVED)

    def decline(self, doc, *, approver_ref, comment=""):
        return self._decide(doc, approver_ref, comment, ProcApprovalState.REJECTED)

    def _decide(self, doc, approver_ref, comment, outcome):
        document = self._get(doc)
        if document.approval_state is not ProcApprovalState.PENDING:
            raise ProcurementStateError(f"Document {doc.doc_ref!r} is not pending.")
        if doc.doc_ref in self._parked:
            raise ProcurementStateError(
                f"Document {doc.doc_ref!r} is parked: nobody holds the approving role."
            )
        self._store(ProcDocument(
            ref=document.ref, document_number=document.document_number,
            status=document.status, approval_state=outcome, total=document.total,
            vendor_ref=document.vendor_ref, raised_by_ref=document.raised_by_ref,
            approved_by_override=document.approved_by_override,
        ))
        return _ok(ApprovalDecision(
            doc_ref=doc.doc_ref, doc_type=doc.doc_type, approval_state=outcome,
            decided_by_ref=approver_ref, decided_at=_utcnow(), comment=comment,
        ))

    # -- order / receive / bill / pay -- #
    def raise_purchase_order(self, requisition, *, vendor_ref, actor_ref, order_date):
        document = self._get(requisition)
        if document.approval_state is not ProcApprovalState.APPROVED:
            raise ProcurementStateError("The requisition must be APPROVED first.")
        return _ok(self._new(ProcDocType.PURCHASE_ORDER, document.ref.entity_ref,
                             document.ref.branch_ref, document.total,
                             raised_by=actor_ref, vendor_ref=vendor_ref))

    def receive_goods(self, po, *, actor_ref, lines, received_date=None):
        document = self._get(po)
        if not lines:
            raise ProcurementStateError("A goods receipt needs at least one line.")
        return _ok(self._new(ProcDocType.GOODS_RECEIPT, document.ref.entity_ref,
                             document.ref.branch_ref, document.total,
                             raised_by=actor_ref, vendor_ref=document.vendor_ref,
                             status="POSTED"))

    def record_supplier_bill(self, po, *, vendor_ref, actor_ref, lines, invoice_date,
                             external_reference=""):
        document = self._get(po)
        if not lines:
            raise ProcurementStateError("A supplier bill needs at least one line.")
        total = sum(line.quantity * line.unit_price for line in lines)
        # DRAFT, like the real adapter: an unapproved bill is not posted.
        return _ok(self._new(ProcDocType.VENDOR_INVOICE, document.ref.entity_ref,
                             document.ref.branch_ref, total, raised_by=actor_ref,
                             vendor_ref=vendor_ref, status="DRAFT"))

    def pay_supplier(self, bill, *, actor_ref, amount, payment_date):
        document = self._get(bill)
        if amount <= 0 or amount > document.total:
            raise ProcurementStateError("The payment must be positive and within the bill.")
        return _ok(self._new(ProcDocType.VENDOR_PAYMENT, document.ref.entity_ref,
                             document.ref.branch_ref, amount, raised_by=actor_ref,
                             vendor_ref=document.vendor_ref, status="DRAFT"))

    def post_to_ledger(self, doc, *, actor_ref):
        document = self._get(doc)
        if doc.doc_type not in (ProcDocType.VENDOR_INVOICE, ProcDocType.VENDOR_PAYMENT):
            raise ProcurementStateError(
                f"A {doc.doc_type.value} is not posted through this method."
            )
        if document.approval_state is not ProcApprovalState.APPROVED:
            raise ProcurementStateError(
                f"Document {doc.doc_ref!r} must be approved before it is posted."
            )
        return _ok(self._store(ProcDocument(
            ref=document.ref, document_number=document.document_number,
            status="POSTED", approval_state=document.approval_state,
            total=document.total, vendor_ref=document.vendor_ref,
            raised_by_ref=document.raised_by_ref,
            approved_by_override=document.approved_by_override,
        )))

    # -- seeding -- #
    def seed_approval_rules(self, *, entity_ref, threshold):
        created = entity_ref not in self.seeded_entities
        self.seeded_entities.add(entity_ref)
        return _ok(created)


class FakeProcurementReader(ProcurementReadPort):
    def __init__(self, *, open_requests: int = 0, pending_approvals: int = 0,
                 spend: int = 0, rows: tuple = ()) -> None:
        self._snapshot = ProcurementSnapshot(
            open_requests=open_requests, pending_approvals=pending_approvals,
            spend=spend,
        )
        self._rows = rows

    def snapshot(self, school_ref, branch_ref=None):
        return _ok(self._snapshot)

    def rows(self, school_ref, branch_ref=None, filters=(), page=1, page_size=20):
        return _ok(_page(self._rows, page, page_size))


# --------------------------------------------------------------------------- #
# Unavailable variants - the state a consumer must render instead of zero
# --------------------------------------------------------------------------- #
class _UnavailableMixin:
    reason = Unavailable.BACKEND_UNAVAILABLE

    def _u(self):
        return FinanceResult.unavailable(self.reason)


class UnavailableFinanceReader(_UnavailableMixin, FinanceReadPort):
    def collections(self, *a, **k): return self._u()
    def outstanding(self, *a, **k): return self._u()
    def collection_rate(self, *a, **k): return self._u()
    def debtor_count(self, *a, **k): return self._u()
    def payment_trend(self, *a, **k): return self._u()
    def ar_ageing(self, *a, **k): return self._u()
    def fee_liability(self, *a, **k): return self._u()
    def debtors(self, *a, **k): return self._u()
    def fee_invoices(self, *a, **k): return self._u()
    def payments(self, *a, **k): return self._u()
    def fee_status(self, *a, **k): return self._u()
    def invoices_for(self, *a, **k): return self._u()
    def combined_balance(self, *a, **k): return self._u()


class UnavailableProcurementReader(_UnavailableMixin, ProcurementReadPort):
    def snapshot(self, *a, **k): return self._u()
    def rows(self, *a, **k): return self._u()


class UnavailableParentPaymentBridge(_UnavailableMixin, ParentPaymentBridgePort):
    reason = Unavailable.GATEWAY_UNAVAILABLE

    def start_payment_session(self, *a, **k): return self._u()
    def receipt_for(self, *a, **k): return self._u()


def unavailable_finance_reader() -> FinanceReadPort:
    return UnavailableFinanceReader()


def unavailable_procurement_reader() -> ProcurementReadPort:
    return UnavailableProcurementReader()


def unavailable_parent_payment_bridge() -> ParentPaymentBridgePort:
    return UnavailableParentPaymentBridge()


__all__ = [
    "FakeEntityResolver",
    "FakeFeeTermBridge",
    "FakeStudentCustomer",
    "FakeFinanceRbac",
    "FakeFinanceReader",
    "FakeGuardianLink",
    "UnconfiguredGuardianLink",
    "FakeParentPaymentBridge",
    "FakeProcurementActions",
    "FakeProcurementReader",
    "UnavailableFinanceReader",
    "UnavailableProcurementReader",
    "UnavailableParentPaymentBridge",
    "unavailable_finance_reader",
    "unavailable_procurement_reader",
    "unavailable_parent_payment_bridge",
]
