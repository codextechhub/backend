"""
schools.core.fal.exceptions
===========================

Exceptions the FAL *raises*. These are strictly for caller/programming errors
and invariant violations: bad input, cross-tenant references, misconfiguration,
missing provisioning.

They are deliberately separate from "the source is unavailable", which is NOT an
exception: an unreachable finance backend returns
``FinanceResult.unavailable(...)``. Keeping these apart means a consumer's
``try/except`` is only ever catching genuine bugs and bad requests, while
transient backend outages flow through the availability envelope and render a
clean "unavailable" state.

Mapping at the API edge (the consuming module's responsibility):

    CrossTenantError          -> 404   (never 403; no tenant enumeration)
    EntityNotProvisioned      -> 409 / 404 depending on the flow
    AmbiguousPrimaryEntity    -> 500-class config error; page the operator
    CustomerCreationRace      -> retried transparently by the adapter; surfaces
                                 only if it cannot be resolved
    CustomerNotProvisioned    -> 409 (student has no AR account to bill)
    TermNotLinkedError        -> 409 (fee structure has no term to bill against)
    InvalidTermLinkError      -> 400 (the named term is not in the named session)
    InvalidFilterError        -> 400 (bad report/list filter)
    PaymentGatewayError       -> 502-class (gateway rejected the session)
    ApprovalTemplateMissing   -> 409 (no approval rule configured at all)
    ApprovalNotParkedError    -> 409 (override attempted on a non-parked document)
    OverrideNotPermitted      -> 403 (caller lacks procurement.approval.override)
    CrossBranchError          -> 404 (branch-bound user reached another branch)
    ProcurementStateError     -> 409 (document in the wrong state for the action)

NOTE: "a rule exists but nobody holds the approving role" is **not** an error.
The document submits and *parks*. See ``ApprovalSubmission.is_parked``.
"""

from __future__ import annotations


class FALError(Exception):
    """Base class for all FAL-raised errors."""


class FALNotConfiguredError(FALError):
    """A port was requested but no adapter is wired in settings."""


class CrossTenantError(FALError):
    """A reference (student/invoice/branch/entity) belongs to a different school.

    Raised rather than returning empty, so the bug surfaces loudly in
    development. At the API edge the consumer translates this to a 404 (never
    403), consistent with the platform's no-tenant-enumeration rule.

    The check itself is a tenant comparison underneath: a school's books are
    reached through ``School.tenant`` -> ``LedgerEntity.tenant``. The error keeps
    its school-facing name because the FAL's callers think in schools.
    """


# --------------------------------------------------------------------------- #
# Provisioning / resolution errors (components 1 & 3)
# --------------------------------------------------------------------------- #
class ProvisioningError(FALError):
    """Base for entity/customer provisioning failures."""


class EntityNotProvisioned(ProvisioningError):
    """A school has no LedgerEntity yet (onboarding has not run, or not linked).

    Reads and billing that require the school's set of books raise this rather
    than silently creating one on a read path. Provisioning is an explicit,
    idempotent onboarding action (``EntityResolverPort.provision_entity``).
    """


class AmbiguousPrimaryEntity(ProvisioningError):
    """A school has more than one candidate primary ``LedgerEntity``.

    Decision (2026-07-04): onboarding provisions exactly **one** primary entity per
    school and ``resolve_entity(school_ref)`` returns it. If two candidates
    exist, the FAL raises this loudly; it never guesses. Callers that need a
    non-primary entity pass an explicit ``entity_ref`` override instead.

    Nothing below the FAL prevents the second entity. ``LedgerEntity`` has no
    ``is_primary`` column and no constraint expressing "one per tenant", and its
    docstring explicitly permits several entities per tenant. This exception is
    the only place the rule is enforced.
    """


class CustomerCreationRace(ProvisioningError):
    """Two concurrent first-billing actions raced to create the same Customer.

    The adapter serialises the check-then-create on the entity row and catches a
    unique-constraint violation on ``(entity, code)``
    (``uniq_finance_customer_entity_code``) to re-read the winning row; this
    error surfaces only if that recovery itself fails. Consumers should treat it
    as retryable.
    """


class CustomerNotProvisioned(ProvisioningError):
    """A student has no AR ``Customer`` in the entity being billed.

    Creating a Customer needs a display name.
    :meth:`StudentCustomerPort.ensure_customer` reads one from the roll, so this
    is raised when the student reference names no child there and the caller
    passed no name either. Cohort billing refuses that student rather than
    inventing a name or silently dropping the child from the run.
    """


# --------------------------------------------------------------------------- #
# Fee/term bridge errors (component 2)
# --------------------------------------------------------------------------- #
class TermNotLinkedError(FALError):
    """A FeeStructure has not been linked to an academic session/term.

    Billing a cohort requires knowing *which* term the invoices belong to;
    raising here prevents generating undated and unattributable invoices.
    """


class InvalidTermLinkError(FALError):
    """The session/term pair named in ``link_term`` does not hold together.

    Sessions and terms are real rows
    (``schools.vs_academics.AcademicSession`` and ``AcademicTerm``), not opaque
    strings, so a term belonging to a different session is a caller error the
    FAL can and should catch.
    """


# --------------------------------------------------------------------------- #
# Read/list errors (component 5)
# --------------------------------------------------------------------------- #
class InvalidFilterError(FALError):
    """A ``FilterClause`` names a field/op the source does not permit.

    Raised by the adapter's per-source filter whitelist before any query is
    built, as part of the no-raw-SQL rule.
    """


# --------------------------------------------------------------------------- #
# Payment gateway errors (component 6)
# --------------------------------------------------------------------------- #
class PaymentGatewayError(FALError):
    """A gateway-session request was deterministically rejected.

    Raised by the parent-portal bridge (component 6) when
    ``vs_payments.initiate_collection`` refuses the request outright: no
    customer, an unposted invoice, or an amount exceeding the invoice balance.
    Those surface as a DRF ``ValidationError``, not as a ``FinanceError``.

    A *transient* provider failure is different and does not raise: the provider
    call re-raises ``ProviderError`` (502) or ``ProviderNotConfiguredError``
    (503), and the bridge turns those into ``Unavailable.GATEWAY_UNAVAILABLE`` /
    ``Unavailable.NOT_CONFIGURED``.
    """


class GuardianLinkNotConfigured(FALError):
    """The guardian-to-student ownership check has no source to consult.

    Decision 5 (2026-07-04) says every parent-portal read and payment is
    authorised by the guardian-to-student link, because guardians hold no RBAC
    keys. A deployment without the student module points ``FAL_GUARDIAN_LINK``
    at ``adapters.django_finance.DenyAllGuardianLinkAdapter``, which has no
    source to consult, and this error is what a caller sees.

    It is deliberately not ``CrossTenantError``: "this guardian does not own that
    child" and "nobody can answer that question yet" are different facts, and a
    portal must not report the second as the first. Wire a real resolver through
    ``FAL_GUARDIAN_LINK``.
    """


# --------------------------------------------------------------------------- #
# Procurement errors (component 7)
# --------------------------------------------------------------------------- #
class ProcurementError(FALError):
    """Base for procurement action failures."""


class ApprovalTemplateMissingError(ProcurementError):
    """No approval rule/template is configured for this document type at all.

    The **only** hard refusal on the submit path. ``vs_workflow`` raises
    ``TemplateNotFoundError`` *before* creating an instance, and
    ``vs_procurement`` translates it into its own ``ApprovalTemplateMissingError``
    with a non-engine message. Because the translation happens inside the atomic
    block after the document write, the PENDING flip rolls back: nothing is
    persisted and the document stays NOT_SUBMITTED. Actionable by a platform
    admin, not by assigning a role.

    Contrast with "a rule exists but nobody holds the approving role", which is
    **not** an error: the document submits and *parks*. See
    :class:`~schools.core.fal.contracts.ApprovalSubmission` (``is_parked``).
    """


class ApprovalNotParkedError(ProcurementError):
    """An approve-without-review override was attempted on a document that is
    not parked.

    The override exists to release a document **stuck** on an active stage with
    no eligible approver. A document progressing normally must go through its
    approvers; a NOT_SUBMITTED document must be submitted first. The backing
    service applies the same rule under a row lock
    (``vs_procurement.approval_override.release_parked_document``).
    """


class OverrideNotPermittedError(ProcurementError):
    """An approval override was attempted without the right to do it.

    The override requires the dedicated ``procurement.approval.override`` key
    (``vs_procurement.constants.WF_APPROVAL_OVERRIDE_PERMISSION``, seeded to
    **nobody**) *and* a non-empty typed reason; a missing or blank reason raises
    this too.
    """


class CrossBranchError(ProcurementError):
    """A branch-bound user referenced a document in another branch.

    Distinct from :class:`CrossTenantError` (a different *school*). Fails closed
    and is rendered 404 at the edge, under the same no-enumeration rule. NOTE: an
    **empty** branch is not a cross-branch error; it is a valid head-office
    document for a school-level user (decision 4).
    """


class ProcurementStateError(ProcurementError):
    """The document is in the wrong state for the requested action.

    Translation of ``vs_procurement``'s own state guards, for example
    ``ApprovalWorkflowError`` when a document is already PENDING or APPROVED, or
    the PO-creation gate that requires an APPROVED requisition. The FAL does not
    re-implement these rules; it surfaces them typed.
    """


# =========================================================================== #
# v1.2 (DEFERRED) - payment application errors.
#
# Decision (2026-07-04): PaymentPort/apply_payment is NOT part of the v1.1.x
# surface; settlement stays inside vs_payments (confirm_collection ->
# _book_receipt). These error types are retained only as the starting point for
# the v1.2 refactor and are not exported from the package.
# =========================================================================== #
class InvalidPaymentError(FALError):
    """v1.2 (deferred): a payment application command is structurally invalid."""


class AllocationMismatchError(InvalidPaymentError):
    """v1.2 (deferred): sum(command.allocations) != command.amount (or <= 0)."""


class UnknownInvoiceError(InvalidPaymentError):
    """v1.2 (deferred): an allocation targets an invoice absent from the entity."""


class OverAllocationError(InvalidPaymentError):
    """v1.2 (deferred): an allocation exceeds an invoice's outstanding balance."""
