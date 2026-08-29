"""
schools.core.fal.ports
======================

The capability ports that make up the Finance Abstraction Layer. A *port* is an
abstract interface (a hexagonal-architecture "port"): the contract a consumer
codes against, with no knowledge of how the data is actually fetched. Concrete
*adapters* (``adapters``) implement these against the real finance subsystems;
fakes (``testing``) implement them in memory.

Ports, by the seven approved components
---------------------------------------
    1. EntityResolverPort         School -> LedgerEntity (provision + resolve)
    2. FeeTermBridgePort          FeeStructure <-> session/term, cohort billing
    3. StudentCustomerPort        Student -> AR Customer (create on first bill)
    4. FinanceRbacPort            school-scoped finance permission checks
    5. FinanceReadPort            aggregates, lists, per-student views, dashboards
    6. ParentPaymentBridgePort    start a gateway session / read status & receipts
                                  (ownership-checked; never books)
    7. ProcurementActionPort      raise/submit/approve/order/receive/bill/pay
                                  (thin pass-through to vs_procurement services)
       ProcurementReadPort        procurement snapshot + rows (reads)

plus one support port that is not a component:

       GuardianLinkPort           answers "does this guardian own this child?",
                                  which component 6's ownership check needs and
                                  which no model in the repository can answer yet.

Decision (2026-07-04): ``PaymentPort`` (apply a confirmed collection) is
**deferred to FAL v1.2** and is not part of the v1.1.x surface. Settlement stays
inside ``vs_payments`` (``confirm_collection`` -> ``_book_receipt``). The class
is retained at the bottom of this module, clearly quarantined, as the v1.2
starting point; it is not exported from the package and has no registry key.

Why many small ports instead of one fat interface
--------------------------------------------------
Each consumer depends only on the capability it needs, and the type system
enforces the boundary:

    M9  onboarding     -> EntityResolverPort (+ ProcurementActionPort for seeding)
    M11 students       -> StudentCustomerPort + FinanceReadPort (read-only)
    M25 dashboards     -> FinanceReadPort  (+ ProcurementReadPort)
    M26 reports        -> FinanceReadPort  (+ ProcurementReadPort)
    M28 parent portal  -> FinanceReadPort + ParentPaymentBridgePort
    school procurement -> ProcurementActionPort + ProcurementReadPort

Because the student portal is handed a ``FinanceReadPort`` (never a
``ParentPaymentBridgePort``), it is *structurally incapable* of applying or
initiating a payment: the method is not on the type it holds. The read-only
guarantee is enforced by the type-checker, not by convention.

Guarantees every implementation must uphold
--------------------------------------------
* **Tenant isolation.** Every query is bounded to a single school/entity. Data
  from another school can never appear in a result. A reference from another
  school raises ``CrossTenantError`` (fail closed). Component 1 scopes through
  ``LedgerEntity.tenant`` (reached from the school via ``School.tenant``, which
  is a ``OneToOneField``); component 3 scopes through ``entity`` plus the loose
  ``source_type``/``source_id`` pair on ``Customer``.
* **Availability, not exceptions, for outages.** A reachable-but-empty source
  returns an ``AVAILABLE`` result with a zero/empty value; an *unreachable*
  source returns ``UNAVAILABLE``. Implementations never raise for a backend
  outage.
* **RBAC is offered, not assumed.** The read/write ports do NOT themselves check
  user permissions; that stays the consumer's job. ``FinanceRbacPort``
  (component 4) gives consumers a *school-scoped* way to evaluate the finance
  permission keys against the school's entity.
* **Side-effect-free reads.** Read methods never mutate finance state. The
  provisioning methods (``provision_entity``, ``ensure_customer``) are the only
  read-shaped methods that may write, and they are **idempotent** by contract.
* **No raw SQL in adapters.** Every adapter method builds its query through the
  Django ORM (scoped queryset first). No adapter interpolates a ``FilterClause``
  or a ref into a raw SQL string.
* **Thin pass-through for procurement.** ``ProcurementActionPort`` (component 7)
  never re-implements procurement logic; it delegates to the existing
  ``vs_procurement`` services and owns only tenancy, branch scoping, permission
  and error translation.

Placement
---------
This package belongs to the **school layer** (``apps/schools/core/fal/``), not to
``apps/core/``. The FAL is where school vocabulary meets the neutral finance
engines, and it is the boundary at which school words stop. Schools may import
the engines; the engines may never import schools.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Optional

from .contracts import (
    ApplyPaymentCommand,
    ApprovalDecision,
    ApprovalSubmission,
    ArAgeingReport,
    BillLine,
    BranchRef,
    CustomerHandle,
    CustomerRef,
    DateRange,
    DebtorRow,
    EntityHandle,
    EntityRef,
    FeeLiability,
    FeeRow,
    FeeStatus,
    FeeStructureRef,
    FeeTermLink,
    FilterClause,
    FinanceResult,
    GuardianRef,
    InvoiceGenerationResult,
    InvoiceRef,
    InvoiceView,
    Kobo,
    KpiValue,
    Page,
    PaymentApplication,
    PaymentRef,
    PaymentRow,
    Period,
    ProcDocRef,
    ProcDocument,
    ProcurementRow,
    ProcurementSnapshot,
    Receipt,
    ReceiptLine,
    SchoolRef,
    Series,
    SessionRef,
    StudentRef,
    TermRef,
    UserRef,
    VendorRef,
)


# =========================================================================== #
# Component 1 - School -> Entity resolver
# =========================================================================== #
class EntityResolverPort(ABC):
    """Provision and resolve a school's ``LedgerEntity`` (its set of books).

    The caller passes a **school**. The entity is keyed by **tenant**. Both are
    true at once, and this port is where they meet.

    Backing reality (verified against the code, August 2026):

    * ``LedgerEntity`` carries a ``tenant`` FK to ``vs_tenants.Tenant``; there is
      no ``source_school``.
    * ``LedgerEntity.objects.for_school(school)`` resolves
      ``filter(tenant=school.tenant)``.
    * ``School.tenant`` is a ``OneToOneField``, so school-keyed and tenant-keyed
      resolution select the same rows, always.
    * ``vs_finance.views.resolve_entity`` is **not** this port's implementation
      despite the shared name. It takes a ``request``, reads ``?entity=``, and
      matches against ``request.tenant``. There is no school in it.

    **Precondition.** Nothing sets ``request.school`` anywhere. Tenant context
    comes from ``TenantJWTAuthentication`` setting ``request.tenant`` /
    ``request.rbac_tenant`` after the caller asserts ``?tenant=<slug>``. This
    port takes ``school_ref`` explicitly and depends on none of it; an adapter
    must never read ``request``.

    Decision (2026-07-04): M9 provisions exactly **one primary** entity per
    school; ``resolve_entity`` returns that primary. If two candidate primaries
    exist, implementations raise ``AmbiguousPrimaryEntity``, loudly and never
    guessing. Ports that act on a specific entity also accept an explicit
    ``entity_ref`` for the rare non-primary case.
    """

    @abstractmethod
    def provision_entity(
        self, school_ref: SchoolRef, *, code: str, name: str,
        base_currency: str = "NGN",
    ) -> FinanceResult[EntityHandle]:
        """Ensure a ``LedgerEntity`` exists for ``school_ref`` (M9 onboarding).

        **Idempotent.** Onboarding retries are expected: a second call for a
        school that already has an entity returns the existing handle with
        ``was_created=False`` and does not create a duplicate. Creation runs in a
        single transaction, serialised on the school's tenant row so two
        concurrent onboardings cannot both create.

        The implementation must pass the school's tenant **explicitly**:
        ``LedgerEntity.save`` silently assigns the Codex platform tenant when
        ``tenant_id`` is unset, so a provisioning bug would otherwise write into
        the platform's own books.

        :returns: ``AVAILABLE`` with the :class:`EntityHandle`.
        :raises CrossTenantError: ``code`` already belongs to another school, or
            ``school_ref`` does not resolve.
        :raises AmbiguousPrimaryEntity: the school already has two candidates.
        """

    @abstractmethod
    def resolve_entity(self, school_ref: SchoolRef) -> FinanceResult[EntityHandle]:
        """Return the school's **primary** entity handle.

        :raises EntityNotProvisioned: the school has no entity yet (onboarding
            has not run). Reads never auto-provision.
        :raises AmbiguousPrimaryEntity: more than one candidate primary exists.
        """


# =========================================================================== #
# Component 2 - Fee structure <-> academic term bridge
# =========================================================================== #
class FeeTermBridgePort(ABC):
    """Link fee structures to academic terms and bill student cohorts.

    ``session_ref``/``term_ref`` are the primary keys of
    ``schools.vs_academics.AcademicSession`` / ``AcademicTerm``. Decision
    (2026-07-04) put the link in a **FAL-owned table**, and it still is:
    :class:`~schools.core.fal.models.FeeStructureTermLink`. What changed in 1.1.2
    is that the refs are real foreign keys rather than opaque strings, because
    the calendar app the decision was waiting on now exists.

    Cohort billing delegates to the real
    ``vs_finance.fees.generate_invoices(structure, customers, *,
    invoice_date=None, due_date=None, actor_user=None)``, which is decorated
    ``@transaction.atomic`` and is already idempotent: it skips a customer who
    already has a POSTED invoice referencing the structure via ``FEE:<code>``. It
    takes ``Customer`` objects, not students, which is why component 3 exists.
    """

    @abstractmethod
    def link_term(
        self, fee_structure_ref: FeeStructureRef, session_ref: SessionRef,
        term_ref: Optional[TermRef] = None,
    ) -> FinanceResult[FeeTermLink]:
        """Attach a fee structure to a session/term. Idempotent (re-link updates).

        :raises CrossTenantError: the structure's entity and the session belong
            to different tenants.
        :raises InvalidTermLinkError: the term is not in the named session.
        """

    @abstractmethod
    def generate_cohort_invoices(
        self, fee_structure_ref: FeeStructureRef, student_refs: tuple[StudentRef, ...],
        *, period: Optional[Period] = None,
    ) -> FinanceResult[InvoiceGenerationResult]:
        """Generate one posted invoice per student in the cohort.

        Resolves each student to its AR Customer, then calls
        ``fees.generate_invoices``. Idempotent: already-billed students are
        returned in ``students_skipped``. Runs in a single transaction.

        :raises TermNotLinkedError: the structure has no linked term.
        :raises CustomerNotProvisioned: a student has no AR customer in this
            entity. The FAL cannot create one here because it has no source for
            the child's name; call ``ensure_customer`` first.
        :raises CrossTenantError: a student's customer belongs to another school.
        """


# =========================================================================== #
# Component 3 - Student -> Customer resolver
# =========================================================================== #
class StudentCustomerPort(ABC):
    """Map a Student to an AR ``Customer`` in the school's entity.

    The backend ``Customer`` links to its domain record via the loose
    ``source_type`` + ``source_id`` strings, never an FK, and is unique on
    ``(entity, code)`` (``uniq_finance_customer_entity_code``). Creation is
    guarded against a concurrent first-billing race
    (``CustomerCreationRace``).

    Cross-tenant checks compare the entity's tenant against every other fact the
    FAL can actually reach: the branch's tenant, and the tenant of any entity in
    which this student is already a customer. They cannot compare the *student's*
    school, because no student model exists to carry one.
    """

    @abstractmethod
    def ensure_customer(
        self, student_ref: StudentRef, *, entity_ref: EntityRef,
        name: str, code: Optional[str] = None,
        branch_ref: Optional[BranchRef] = None,
    ) -> FinanceResult[CustomerHandle]:
        """Return the student's Customer, creating it on first billing.

        **Idempotent.** A second call returns the existing customer with
        ``was_created=False``. The check-then-create is serialised on the entity
        row, and a unique-constraint race on the allocated code is recovered by
        re-reading the winning row. ``branch_ref`` is optional because
        ``Customer.branch`` is nullable; a school-wide customer is first-class.

        :raises CrossTenantError: ``entity_ref``/``branch_ref`` are not the
            student's school's.
        :raises CustomerCreationRace: an unrecoverable concurrent create.
        """

    @abstractmethod
    def customer_for(
        self, student_ref: StudentRef, *, entity_ref: EntityRef,
    ) -> FinanceResult[Optional[CustomerHandle]]:
        """Return the student's Customer if one exists, else ``AVAILABLE``/None.
        Never creates."""


# =========================================================================== #
# Component 4 - School-scoped RBAC for finance
# =========================================================================== #
class FinanceRbacPort(ABC):
    """Evaluate finance permission keys against a *school's* entity.

    Delegates to ``vs_rbac.evaluator.has_permission(user, permission_key,
    tenant=None, branch=ANY_BRANCH)``. There is no ``school=`` parameter on the
    evaluator; the convenience wrapper
    ``vs_rbac.permissions.user_has_rbac_permission`` accepts one and converts it
    to ``school.tenant`` before forwarding.

    The point of the port is to guarantee the permission is evaluated in the
    correct school scope (the school owning ``entity_ref``), never globally: a
    permission granted in one school must never authorise an action in another.

    Three behaviours consumers must not get wrong:

    * **Fail-closed falls back to the user's tenant**, not their school, and the
      effective set is empty when the tenant is absent or is not the user's own.
    * **There is no super-admin bypass in the evaluator.**
      ``is_vision_super_admin`` short-circuits only inside the DRF classes
      ``HasRBACPermission`` / ``HasAnyModuleAccess``. So ``can()`` returns
      ``False`` for a Vision super-admin holding no explicit key, which differs
      from what that person experiences at a view.
    * **Omitting a branch is not the same as naming no branch.** The evaluator's
      ``branch`` default is the ``ANY_BRANCH`` sentinel ("do not narrow"), while
      an explicit ``None`` is a real scope meaning "the entity as a whole", which
      excludes branch-pinned grants. ``branch_ref=None`` on this port means the
      caller named no branch, so the adapter forwards ``ANY_BRANCH``.

    Decision (2026-07-04): this port applies to **staff-facing surfaces only**.
    Parents and guardians hold no RBAC keys; the M28 parent portal authorises via
    **ownership checks** on the guardian-to-student link instead.
    """

    @abstractmethod
    def can(
        self, user_ref: UserRef, permission_key: str, *, entity_ref: EntityRef,
        branch_ref: Optional[BranchRef] = None,
    ) -> FinanceResult[bool]:
        """True iff ``user_ref`` holds ``permission_key`` in the entity's school.

        :raises EntityNotProvisioned: ``entity_ref`` does not resolve.
        :raises CrossTenantError: ``branch_ref`` belongs to another tenant.
        """


# =========================================================================== #
# Component 5 - Finance reads + dashboard data contracts
# =========================================================================== #
class FinanceReadPort(ABC):
    """Read-only finance: aggregates, detail lists, per-entity fee views, and the
    standardised dashboard contracts (AR ageing, collection rate, fee liability).

    Nothing here mutates state. Every method is scoped to one school, and there
    is **no raw SQL**: adapters build ORM querysets.
    """

    # ----- Headline KPIs (M25) --------------------------------------------- #
    @abstractmethod
    def collections(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
        period: Optional[Period] = None,
    ) -> FinanceResult[KpiValue]:
        """Total amount collected in scope (kobo)."""

    @abstractmethod
    def outstanding(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
        period: Optional[Period] = None,
    ) -> FinanceResult[KpiValue]:
        """Total outstanding balance in scope (kobo)."""

    @abstractmethod
    def collection_rate(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
        period: Optional[Period] = None,
    ) -> FinanceResult[KpiValue]:
        """Collected / expected, as a scaled-integer ratio (unit RATIO, scale)."""

    @abstractmethod
    def debtor_count(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
        period: Optional[Period] = None,
    ) -> FinanceResult[KpiValue]:
        """Number of customers with an outstanding balance (count)."""

    @abstractmethod
    def payment_trend(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
        date_range: Optional[DateRange] = None,
    ) -> FinanceResult[Series]:
        """Payment volume over time, as an ordered series (kobo)."""

    # ----- Dashboard data contracts (M25, component 5) --------------------- #
    @abstractmethod
    def ar_ageing(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
        period: Optional[Period] = None,
    ) -> FinanceResult[ArAgeingReport]:
        """AR ageing bucketed (current / 1-30 / 31-60 / 61-90 / 90+), no raw SQL."""

    @abstractmethod
    def fee_liability(
        self, school_ref: SchoolRef, period: Optional[Period] = None,
    ) -> FinanceResult[FeeLiability]:
        """Total billed vs collected vs outstanding for a term."""

    # ----- Detail lists (dashboard drill-down + M26 report sources) -------- #
    @abstractmethod
    def debtors(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
        filters: tuple[FilterClause, ...] = (), page: int = 1, page_size: int = 20,
    ) -> FinanceResult[Page[DebtorRow]]:
        """Paged debtor list (KPI drill-down M25 + report 'debtors' source M26)."""

    @abstractmethod
    def fee_invoices(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
        filters: tuple[FilterClause, ...] = (), page: int = 1, page_size: int = 20,
    ) -> FinanceResult[Page[FeeRow]]:
        """Paged invoice-level rows. Report 'fees' source (M26)."""

    @abstractmethod
    def payments(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
        filters: tuple[FilterClause, ...] = (), page: int = 1, page_size: int = 20,
    ) -> FinanceResult[Page[PaymentRow]]:
        """Paged transaction-level rows. Report 'payments' source (M26)."""

    # ----- Per-entity fee views (portals, M11/M28) ------------------------- #
    @abstractmethod
    def fee_status(self, student_ref: StudentRef) -> FinanceResult[FeeStatus]:
        """A single student's read-only fee position (student portal, M11)."""

    @abstractmethod
    def invoices_for(
        self, student_ref: StudentRef, include_history: bool = True,
    ) -> FinanceResult[tuple[InvoiceView, ...]]:
        """A child's invoices with line items (parent portal, M28)."""

    @abstractmethod
    def combined_balance(
        self, student_refs: tuple[StudentRef, ...],
    ) -> FinanceResult[Kobo]:
        """Total outstanding across several children of one guardian (siblings).

        All students must belong to the same school; a mixed set raises
        ``CrossTenantError``."""


# =========================================================================== #
# Support port - the guardian-to-student ownership question
# =========================================================================== #
class GuardianLinkPort(ABC):
    """Answers "does this guardian own this child?" for component 6.

    ADDED in 1.1.2 and not one of the seven components. Decision 5 (2026-07-04)
    authorises every parent-portal read and payment by the guardian-to-student
    link, because guardians hold no RBAC keys. There is no guardian model and no
    student model in the repository, so there is nothing for the parent-payment
    adapter to consult.

    Rather than leave the check as a comment for M11 to notice, it is a port with
    a deny-everything default. The bridge is therefore closed in production until
    somebody wires a real resolver through ``FAL_GUARDIAN_LINK``, and open in a
    test that injects one, so the rest of component 6 is genuinely exercised.
    """

    @abstractmethod
    def owns(self, guardian_ref: GuardianRef, student_ref: StudentRef) -> bool:
        """True iff ``guardian_ref`` is a guardian of ``student_ref``.

        :raises GuardianLinkNotConfigured: no source can answer the question.
            Distinct from returning ``False``, which is a real "no".
        """


# =========================================================================== #
# Component 6 - Parent portal payment bridge
# =========================================================================== #
class ParentPaymentBridgePort(ABC):
    """M28's safe door onto ``vs_payments``: **initiate plus read-only
    status/receipts, ownership-checked**. It never books anything.

    Delegates initiation to ``vs_payments.services.initiate_collection(...)``,
    which returns a ``CollectionIntent`` carrying a ``checkout_url``. Settlement
    stays entirely inside ``vs_payments`` (``confirm_collection`` ->
    ``_book_receipt``), per the 2026-07-04 decision. The parent portal never
    imports ``vs_payments`` directly; it holds only this port plus a
    ``FinanceReadPort``.

    Two facts about the backend that shape this port:

    * **Providers are Paystack and Fake.** OPay was removed;
      ``PaymentProvider`` has exactly those two values.
    * **A customer is mandatory.** ``initiate_collection`` infers it from the
      invoice when one is supplied and raises a DRF ``ValidationError`` when it
      ends up with neither. Hence ``customer_ref`` on this port.

    Authorization (decision 2026-07-04): guardians hold **no RBAC keys**. Every
    method here is guarded by an **ownership check** through
    :class:`GuardianLinkPort`: the invoice's or payment's student must be linked
    to the authenticated ``guardian_ref``. A real "no" fails closed as
    ``CrossTenantError``, rendered 404 at the edge.

    Family payments (decision 2026-07-04): a guardian may pay for several
    children **across branches within one school** (one entity, one customer
    ledger). Payments are NEVER merged across schools or entities.
    """

    @abstractmethod
    def start_payment_session(
        self, *, guardian_ref: GuardianRef, entity_ref: EntityRef,
        amount: Kobo, customer_ref: Optional[CustomerRef] = None,
        invoice_ref: Optional[InvoiceRef] = None,
        payer_email: str = "", callback_url: str = "",
    ) -> FinanceResult[str]:
        """Create a gateway checkout session; return its ``checkout_url``.

        ``amount`` is integer kobo. At least one of ``customer_ref`` or
        ``invoice_ref`` must resolve to a customer, because the backend refuses
        a customer-free collection.

        Outage versus rejection:

        * ``ProviderError`` (502) or a transport failure -> ``UNAVAILABLE`` with
          ``Unavailable.GATEWAY_UNAVAILABLE``.
        * ``ProviderNotConfiguredError`` (503) -> ``UNAVAILABLE`` with
          ``Unavailable.NOT_CONFIGURED``.
        * DRF ``ValidationError`` from ``initiate_collection`` (no customer,
          unposted invoice, amount over balance) -> **raises**
          ``PaymentGatewayError``.

        :raises PaymentGatewayError: the request was deterministically rejected.
        :raises CrossTenantError: the ownership check said no, or the customer /
            invoice is outside ``entity_ref``.
        :raises GuardianLinkNotConfigured: nothing can answer the ownership
            question yet.
        """

    @abstractmethod
    def receipt_for(
        self, payment_ref: PaymentRef, *, guardian_ref: GuardianRef,
    ) -> FinanceResult[Optional[Receipt]]:
        """Return the receipt for a confirmed payment the guardian may see.

        :raises CrossTenantError: the payment's student is not linked to
            ``guardian_ref`` (ownership check).
        :raises GuardianLinkNotConfigured: as above.
        """


# =========================================================================== #
# Component 7 - Procurement actions
# =========================================================================== #
class ProcurementActionPort(ABC):
    """School-facing procurement: raise, submit, approve/decline, order, receive,
    bill, pay, alongside the reads on ``ProcurementReadPort``.

    **THIS PORT IS A THIN PASS-THROUGH. DO NOT BUILD A PARALLEL PROCUREMENT
    ENGINE.** Every method delegates to an existing ``vs_procurement`` service.
    ``vs_procurement`` keeps owning what the documents *do*: pricing, matching,
    posting, stock, state machines. The FAL owns exactly four things:

      1. **Tenancy** - resolving the school's entity and refusing another's.
      2. **Branch scoping** - defaulting, filtering and routing by branch.
      3. **Permission** - the override right, via ``can_override``.
      4. **Error translation** - turning ``vs_procurement`` / ``vs_workflow``
         exceptions into the typed FAL errors.

    If a method here starts computing totals, deciding match variance, or driving
    a state machine, that logic is in the wrong place.

    Branch scoping (decision 4) - shipped
    -------------------------------------
    Procurement documents carry a branch, **defaulted from the raiser's branch**.
    An **empty branch is valid** for a school-level user and means a head-office
    purchase by that person, never an error. Approver routing follows the
    **document's** branch. This works for schools with one branch and for schools
    with many.

    Approval (decisions 2, 3, 5) - **park, don't skip** - shipped
    -------------------------------------------------------------
    Both seeded stages carry ``skip_if_no_approvers=False`` and
    ``approver_scope="BRANCH"``. The engine activates an unstaffed stage and
    holds it. So ``submit_for_approval`` **succeeds** and reports the resulting
    state via ``ApprovalSubmission.is_parked``; only a missing template is a hard
    refusal. ``approve_without_review`` is the audited escape hatch for a parked
    document, requiring ``procurement.approval.override`` (granted to nobody by
    default) plus a typed reason, and writing an append-only ``ApprovalOverride``
    row.
    """

    # ----- Raise ----------------------------------------------------------- #
    @abstractmethod
    def raise_requisition(
        self, *, entity_ref: EntityRef, raiser_ref: UserRef,
        lines: tuple[BillLine, ...], branch_ref: Optional[BranchRef] = None,
        narration: str = "",
    ) -> FinanceResult[ProcDocument]:
        """Create a DRAFT purchase requisition.

        ``branch_ref`` defaults to the raiser's branch when omitted; an empty
        branch for a school-level raiser is a valid head-office requisition, not
        an error.

        :raises CrossTenantError: ``entity_ref`` is not the raiser's school's.
        :raises CrossBranchError: a branch-bound raiser named another branch.
        :raises ProcurementStateError: no lines, or a line the engine refuses.
        """

    # ----- Approval -------------------------------------------------------- #
    @abstractmethod
    def submit_for_approval(
        self, doc: ProcDocRef, *, actor_ref: UserRef,
    ) -> FinanceResult[ApprovalSubmission]:
        """Submit a procurement document into its approval ladder.

        Delegates to ``vs_procurement.approvals.submit_for_approval``, which is
        already ``@transaction.atomic``, so the document flip and the workflow
        instance share one transaction.

        **Park, don't skip** (decision 2): a stage with no eligible approver is
        held, not skipped, so this returns ``AVAILABLE`` with
        ``ApprovalSubmission.is_parked=True`` and ``parked_stage_code`` set.
        Callers surface this as "waiting for an approver to be assigned",
        **not** as an error and never as "approved".

        :raises ApprovalTemplateMissingError: no rule/template configured at all.
            Nothing is persisted; this is the only hard refusal here.
        :raises ProcurementStateError: already PENDING/APPROVED, or no actor.
        :raises CrossTenantError / CrossBranchError: scope violation.
        """

    @abstractmethod
    def approve_without_review(
        self, doc: ProcDocRef, *, actor_ref: UserRef, reason: str,
    ) -> FinanceResult[ApprovalSubmission]:
        """Release a **parked** document by approving it without review.

        :returns: ``ApprovalSubmission`` with ``override`` populated.
        :raises OverrideNotPermittedError: no permission, or a blank reason.
        :raises ApprovalNotParkedError: the document is not parked.
        :raises CrossTenantError / CrossBranchError: scope violation.
        """

    @abstractmethod
    def approve(
        self, doc: ProcDocRef, *, approver_ref: UserRef, comment: str = "",
    ) -> FinanceResult[ApprovalDecision]:
        """Record an approval decision on the document's current stage.

        Delegates to ``vs_workflow.services.actions.record_action(instance_id,
        actor, action, comment="")``. There are no separate ``approve`` /
        ``decline`` functions in the engine.

        :raises ProcurementStateError: not PENDING, no active stage, or the actor
            is not an eligible approver.
        :raises CrossTenantError / CrossBranchError: scope violation.
        """

    @abstractmethod
    def decline(
        self, doc: ProcDocRef, *, approver_ref: UserRef, comment: str = "",
    ) -> FinanceResult[ApprovalDecision]:
        """Record a rejection. Terminal per the seeded ladder's ``on_rejection``."""

    # ----- Order / receive / bill / pay ------------------------------------ #
    @abstractmethod
    def raise_purchase_order(
        self, requisition: ProcDocRef, *, vendor_ref: VendorRef,
        actor_ref: UserRef, order_date: date,
    ) -> FinanceResult[ProcDocument]:
        """Create a PO from an APPROVED requisition
        (``purchasing.create_po_from_requisition``). The PO inherits the
        requisition's branch.

        :raises ProcurementStateError: requisition not APPROVED, or the vendor is
            blocked for purchasing.
        :raises CrossTenantError / CrossBranchError: scope violation.
        """

    @abstractmethod
    def receive_goods(
        self, po: ProcDocRef, *, actor_ref: UserRef, lines: tuple[ReceiptLine, ...],
        received_date: Optional[date] = None,
    ) -> FinanceResult[ProcDocument]:
        """Record a goods receipt against a PO and post it
        (``purchasing.post_grn``). Quantities only, no money.

        :raises ProcurementStateError: PO not in a receivable state, or a line
            over-receives.
        :raises CrossTenantError / CrossBranchError: scope violation.
        """

    @abstractmethod
    def record_supplier_bill(
        self, po: ProcDocRef, *, vendor_ref: VendorRef, actor_ref: UserRef,
        lines: tuple[BillLine, ...], invoice_date: date,
        external_reference: str = "",
    ) -> FinanceResult[ProcDocument]:
        """Record a vendor invoice, priced and three-way matched, still DRAFT.

        CORRECTED in 1.1.2: this used to say "record and post". It cannot do
        both. ``vs_procurement`` refuses to post a vendor invoice that has not
        been approved, and approval is a decision a person makes in between, so
        one call could only have posted by skipping the approval decision 2
        exists to protect. The bill is priced and matched here so the approver
        sees the variance verdict while deciding; :meth:`post_to_ledger` follows
        approval.

        :raises ProcurementStateError: the engine refused the bill or a line.
        :raises CrossTenantError / CrossBranchError: scope violation.
        """

    @abstractmethod
    def pay_supplier(
        self, bill: ProcDocRef, *, actor_ref: UserRef, amount: Kobo,
        payment_date: date,
    ) -> FinanceResult[ProcDocument]:
        """Record a vendor payment against a bill, still DRAFT and unposted.

        CORRECTED in 1.1.2, for the same reason as
        :meth:`record_supplier_bill`: money leaving the school is approvable
        spend, and the engine will not post an unapproved payment. The bill this
        money is for is named now, as a draft allocation instruction, so the
        approval wait cannot lose it and posting settles the bill the school
        chose rather than the oldest one. ``amount`` is integer kobo, and
        part-payment is allowed.

        :raises ProcurementStateError: the amount is not positive, or the engine
            refused the payment.
        :raises CrossTenantError / CrossBranchError: scope violation.
        """

    @abstractmethod
    def post_to_ledger(
        self, doc: ProcDocRef, *, actor_ref: UserRef,
    ) -> FinanceResult[ProcDocument]:
        """Post an APPROVED vendor invoice or vendor payment to the ledger.

        ADDED in 1.1.2. The chain is record, submit, approve, post, and without
        this the port could take a school as far as an approved bill and then
        leave it stranded. A goods receipt is not approvable and is posted by
        :meth:`receive_goods` directly, so it is not accepted here.

        :raises ProcurementStateError: the document is not approved, is already
            posted, or the engine refused the journal.
        :raises CrossTenantError / CrossBranchError: scope violation.
        """

    # ----- Onboarding seeding (decision 5) --------------------------------- #
    @abstractmethod
    def seed_approval_rules(
        self, *, entity_ref: EntityRef, threshold: Kobo,
    ) -> FinanceResult[bool]:
        """Seed the school's approval ladder **with no approver assigned**.

        Called by M9 onboarding **immediately after**
        ``EntityResolverPort.provision_entity``. Delegates to
        ``vs_procurement.approvals.ensure_tenant_approval_templates(tenant, ...)``,
        resolving ``entity.tenant`` because the ladder is a governance policy of
        the organisation and not of a single set of books.

        **Idempotent and never destructive**: a document type that already has a
        tenant-scoped template is left exactly as it is, so re-seeding after an
        administrator has customised the ladder does not restore defaults.

        The seeded stages carry ``skip_if_no_approvers=False``, so they park
        rather than auto-approve. Deliberate outcome: the school starts with
        rules but an unassigned approver slot, so the first submitted document
        **parks** until the school assigns someone.

        :returns: ``True`` when any rule was created, ``False`` on a re-seed.
        :raises EntityNotProvisioned: the entity does not resolve.
        """


# =========================================================================== #
# ProcurementReadPort - read-only procurement (M25/M26)
# =========================================================================== #
class ProcurementReadPort(ABC):
    """Read-only procurement aggregates and rows (M25 dashboards, M26 reports)."""

    @abstractmethod
    def snapshot(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
    ) -> FinanceResult[ProcurementSnapshot]:
        """Open requests, pending approvals, and spend in scope."""

    @abstractmethod
    def rows(
        self, school_ref: SchoolRef, branch_ref: Optional[BranchRef] = None,
        filters: tuple[FilterClause, ...] = (), page: int = 1, page_size: int = 20,
    ) -> FinanceResult[Page[ProcurementRow]]:
        """Paged procurement rows. Report 'procurement' source (M26)."""


# =========================================================================== #
# v1.2 (DEFERRED) - PaymentPort: apply a CONFIRMED collection.
#
# Decision (2026-07-04): NOT part of the v1.1.x surface. Settlement is
# exclusively vs_payments' confirm_collection -> _book_receipt -> post_payment
# flow, which is already idempotent (select_for_update plus a terminal-intent
# no-op). This class is retained only as the starting point for the v1.2
# refactor. It is not exported from the package, has no registry key, and MUST
# NOT be wired.
# =========================================================================== #
class PaymentPort(ABC):
    """v1.2 (deferred): apply CONFIRMED collections to invoices."""

    @abstractmethod
    def preview_application(
        self, command: ApplyPaymentCommand,
    ) -> FinanceResult[PaymentApplication]:
        """Validate and compute the effect of applying ``command`` WITHOUT
        persisting."""

    @abstractmethod
    def apply_payment(
        self, command: ApplyPaymentCommand,
    ) -> FinanceResult[PaymentApplication]:
        """Settle ``command`` against its invoices, exactly once."""

    @abstractmethod
    def application_for(
        self, payment_ref: PaymentRef,
    ) -> FinanceResult[Optional[PaymentApplication]]:
        """Return a prior application for ``payment_ref`` (or AVAILABLE/None)."""


__all__ = [
    "EntityResolverPort",
    "FeeTermBridgePort",
    "StudentCustomerPort",
    "FinanceRbacPort",
    "FinanceReadPort",
    "GuardianLinkPort",
    "ParentPaymentBridgePort",
    "ProcurementActionPort",
    "ProcurementReadPort",
    # v1.2 (deferred): importable from this module, but deliberately NOT
    # re-exported from the package. See the quarantine note above.
    "PaymentPort",
]
