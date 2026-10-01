"""
schools.core.fal.adapters.django_finance
========================================

The production adapters: the **only** place in the FAL that imports Django or
touches the ORM. Everything above this module (contracts, ports, registry,
testing) is plain Python.

Four rules this module keeps, and a reader should hold it to:

1. **Scope first, aggregate second.** Every queryset is bounded to one
   ``LedgerEntity`` before anything else happens, and a reference from another
   tenant fails closed with ``CrossTenantError`` rather than returning empty.
2. **No raw SQL.** Filters arrive as ``FilterClause`` values, are checked against
   a per-source whitelist, and become ORM lookups. Nothing is interpolated.
3. **Outages are values; caller errors are exceptions.** A dead or unreachable
   database becomes ``FinanceResult.unavailable(...)``. A cross-tenant ref, a
   missing entity or a disallowed filter raises a typed ``FALError``.
4. **Procurement is a pass-through.** Component 7 resolves entity and branch,
   checks the override right, delegates to ``vs_procurement``'s own services, and
   translates their exceptions. It computes no totals and drives no state
   machine.

A note on managers. Several models the FAL reads (``AcademicSession``,
``AcademicTerm``, ``WorkflowInstance``) default to a tenant-aware manager that
applies whatever tenant is in the ambient thread-local. The FAL is called with
explicit refs, sometimes from a task with no request behind it, and does its own
tenant comparison, so it reads through ``all_objects`` and compares tenants
itself. Relying on ambient scoping here would make the same call answer
differently depending on who happened to set the thread-local.
"""

from __future__ import annotations

import datetime
import functools
import logging
from dataclasses import replace
from typing import Optional

from django.db import IntegrityError, InterfaceError, OperationalError, transaction
from django.db.models import (
    Case,
    Count,
    F,
    Min,
    Q,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, TruncMonth
from django.utils import timezone
from vs_config.clock import branch_today
from vs_tenants.context import get_current_audit_identity

from schools.vs_academics.services.words import term_word

from ..due_dates import policy_for, resolve_due_date
from ..contracts import (
    SOURCE_TYPE_STUDENT,
    student_pk,
    AccountMove,
    AgeingBucket,
    AgeingRow,
    ApprovalDecision,
    ApprovalOverride,
    ApprovalSubmission,
    ArAgeingReport,
    CustomerHandle,
    DebtorRow,
    EntityHandle,
    FeeLiability,
    FeeRow,
    FeeStatus,
    FeeTermLink,
    FilterClause,
    FinanceResult,
    InvoiceGenerationResult,
    InvoiceLine,
    InvoiceStatus,
    InvoiceView,
    KpiValue,
    Page,
    PaymentMethod,
    PaymentRow,
    Period,
    ProcApprovalState,
    ProcDocRef,
    ProcDocType,
    ProcDocument,
    ProcurementRow,
    ProcurementSnapshot,
    Receipt,
    Series,
    SeriesPoint,
    Unavailable,
    Unit,
)
from ..exceptions import (
    AmbiguousPrimaryEntity,
    ApprovalNotParkedError,
    ApprovalTemplateMissingError,
    BranchRequiredError,
    CrossBranchError,
    CrossTenantError,
    CustomerCreationRace,
    CustomerNotProvisioned,
    EntityNotProvisioned,
    GuardianLinkNotConfigured,
    InvalidFilterError,
    InvalidTermLinkError,
    OffPriceListError,
    OverrideNotPermittedError,
    PaymentGatewayError,
    ProcurementStateError,
    TermNotLinkedError,
)
from ..ports import (
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

#: Basis points. A collection rate of 87.5% is 8750 with ``scale=10000``, so the
#: whole contract stays integer-only.
BPS = 10000

#: The ageing buckets, in the order a dashboard shows them, with the age (in
#: days past due) at which each one starts. ``None`` is "not yet due".
_AGEING_EDGES = (
    (AgeingBucket.DAYS_90_PLUS, 90),
    (AgeingBucket.DAYS_61_90, 60),
    (AgeingBucket.DAYS_31_60, 30),
    (AgeingBucket.DAYS_1_30, 0),
)


# --------------------------------------------------------------------------- #
# Envelope plumbing
# --------------------------------------------------------------------------- #
logger = logging.getLogger(__name__)


def envelope(fn):
    """Turn a *connection-level* database failure into an UNAVAILABLE result.

    Only ``OperationalError`` and ``InterfaceError`` are caught: those are the
    ones that mean "the database did not answer". ``IntegrityError`` and friends
    are programming or data faults and must not be dressed up as an outage - if
    one escapes, it should be a 500 that somebody fixes.

    ``FALError`` passes straight through: an invariant violation is not an
    outage, and the split is the whole point of the availability envelope.

    ``AmbiguousPrimaryEntity`` passes through as well, and stays a 500, but it
    does not pass unnoticed. It is the one refusal nobody on the request can act
    on: it says a school's books are arranged so that no read can name its
    primary entity, so every dashboard and procurement request for that school
    raises it until an operator deactivates the spare set, and the person meeting
    the 500 is a bursar or a parent who cannot fix it.

    The report is made here rather than at either raise site because this
    decorator is the closest shared frame to both, and because it is *outside*
    ``provision_entity``'s ``transaction.atomic`` block. Reporting from inside
    that block would write the incident, raise, and roll the incident back with
    the refusal, leaving the onboarding fault silent.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (OperationalError, InterfaceError):
            return FinanceResult.unavailable(Unavailable.BACKEND_UNAVAILABLE)
        except AmbiguousPrimaryEntity as error:
            _report_ambiguous_primary(error)
            raise

    return wrapper


def _available(value):
    return FinanceResult.available(value)


# --------------------------------------------------------------------------- #
# Shared resolution helpers
# --------------------------------------------------------------------------- #
def _school_model():
    from schools.vs_schools.models import School

    return School


def _school(school_ref):
    """The school, or ``CrossTenantError``.

    A school that does not exist and a school the caller may not see are the same
    answer on purpose: the edge renders both 404, so nobody can enumerate school
    ids by watching the error change.
    """
    school = _school_model().objects.filter(pk=school_ref).select_related("tenant").first()
    if school is None:
        raise CrossTenantError(f"No school {school_ref!r} is visible.")
    return school


def _candidate_entities(tenant):
    """The rows the one-primary-per-school convention is judged on.

    Active, tenant-kind entities for this tenant. There is no ``is_primary``
    column and no constraint behind the rule, so this queryset *is* the rule.
    """
    from vs_finance.models import LedgerEntity

    return (
        LedgerEntity.objects
        .filter(tenant=tenant, is_active=True, kind=LedgerEntity.Kind.TENANT)
        .order_by("pk")
    )


#: Prefix of the health fault key this module reports under. The tenant is
#: appended, because one misconfigured school is one fault.
_AMBIGUOUS_PRIMARY_FAULT = "fal.ambiguous-primary-entity"


def _ambiguous_primary(school, detail):
    """The refusal raised when a school has two candidate sets of books.

    The read path and provisioning judge the same arrangement against the same
    queryset, so both build the error here. The attributes carry what an operator
    needs in order to act: the message alone reaches a log, while the incident
    opened from these reaches a person.

    An ``AmbiguousPrimaryEntity`` raised without them is still a valid refusal and
    simply opens no incident. The in-memory fake in ``schools.core.fal.testing``
    raises exactly that, so a consumer's test proves it surfaces the fault without
    writing health rows.
    """
    error = AmbiguousPrimaryEntity(detail)
    error.tenant_ref = school.tenant_id
    error.school_slug = school.slug
    error.school_name = school.name
    return error


def _report_ambiguous_primary(error):
    """Open one health incident for the school this refusal names.

    Reporting never changes what the caller sees. The refusal is a configuration
    fault, it stays one whatever happens here, and every failure of the reporting
    itself is logged and swallowed: a broken reporter that turned an ambiguity
    into an ``AttributeError`` would hide the very fault it exists to surface, and
    would do it on every finance read the school makes.

    The fault key is the school's tenant, because the arrangement being reported
    is one tenant holding two active tenant-kind entities. Keying it on the tenant
    is what holds a read path that raises on every request to a single incident.
    """
    tenant_ref = getattr(error, "tenant_ref", None)
    if tenant_ref is None:
        return
    try:
        from vs_health.faults import report_configuration_fault
        from vs_health.models import Severity

        name = getattr(error, "school_name", "") or ""
        slug = getattr(error, "school_slug", "") or ""
        report_configuration_fault(
            fault_key=f"{_AMBIGUOUS_PRIMARY_FAULT}.tenant-{tenant_ref}",
            title=f"Two sets of books for {name or slug or tenant_ref}",
            summary=(
                f"{error}\n\n"
                f"School: {name} ({slug}), tenant {tenant_ref}.\n"
                "Finance and procurement reads for this school fail until one of "
                "its active tenant-kind ledger entities is deactivated, or until "
                "its callers pass an explicit entity reference."
            ),
            severity=Severity.SEV2,
            affected_tenant_count=1,
        )
    except Exception:
        logger.exception(
            "Could not open a health incident for the ambiguous primary entity of "
            "tenant %s.", tenant_ref,
        )


def _primary_entity(school):
    rows = list(_candidate_entities(school.tenant)[:2])
    if len(rows) > 1:
        raise _ambiguous_primary(
            school,
            f"School {school.slug!r} has more than one active entity, so the FAL "
            f"cannot tell which set of books is its primary one. Deactivate the "
            f"spare, or pass an explicit entity_ref.",
        )
    if not rows:
        raise EntityNotProvisioned(
            f"School {school.slug!r} has no ledger entity yet; onboarding has not "
            f"provisioned its books."
        )
    return rows[0]


def _entity(entity_ref):
    from vs_finance.models import LedgerEntity

    entity = (
        LedgerEntity.objects.filter(pk=entity_ref).select_related("tenant").first()
    )
    if entity is None:
        raise EntityNotProvisioned(f"No ledger entity {entity_ref!r}.")
    return entity


def _school_of(entity):
    """The school behind an entity, or ``None`` for a platform/product tenant."""
    return getattr(entity.tenant, "school_profile", None)


def _entity_handle(entity, school_ref, *, was_created=False):
    return EntityHandle(
        entity_ref=entity.pk,
        school_ref=school_ref,
        code=entity.code,
        name=entity.name,
        base_currency=entity.base_currency_id or "NGN",
        was_created=was_created,
        is_primary=True,
    )


def _user(user_ref):
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(pk=user_ref).select_related("tenant").first()


def _branch(branch_ref, tenant):
    """Resolve a branch inside ``tenant``, or refuse it.

    ``Branch`` moved to ``vs_tenants`` and is owned directly by the tenant, so
    the ownership test is one hop (``branch.tenant_id``) and never
    ``branch.school.tenant``.
    """
    from vs_tenants.models import Branch

    branch = Branch.all_objects.filter(pk=branch_ref).first()
    if branch is None or branch.tenant_id != tenant.pk:
        raise CrossTenantError(f"Branch {branch_ref!r} does not belong to this school.")
    return branch


def _customer_qs():
    from vs_finance.models import Customer

    return Customer.objects.all()


def _customers_for_student(student_ref):
    """Every AR customer carrying this student's loose reference, any entity."""
    return (
        _customer_qs()
        .filter(source_type=SOURCE_TYPE_STUDENT, source_id=str(student_ref))
        .select_related("entity", "entity__tenant")
    )


def _one_tenant(customers, what):
    """The single tenant behind a set of customers, or ``CrossTenantError``."""
    tenants = {c.entity.tenant_id for c in customers}
    if len(tenants) > 1:
        raise CrossTenantError(
            f"{what} span more than one school, and the FAL never merges books "
            f"across schools."
        )
    return next(iter(tenants), None)


# --------------------------------------------------------------------------- #
# Component 1 - School -> Entity resolver
# --------------------------------------------------------------------------- #
def _normalise_entity_code(code: str) -> str:
    """The entity code as ``LedgerEntity`` stores it: uppercase, trimmed."""
    return (code or "").strip().upper()[:16]


class DjangoEntityResolverAdapter(EntityResolverPort):
    """Component 1 over ``vs_finance.provisioning``.

    It delegates rather than creating a ``LedgerEntity`` itself, and the
    difference is not cosmetic. ``provision_books`` is the choke point that gives
    an entity the things that make it *usable* books: the currencies, a starter
    chart of accounts, twelve open fiscal periods, and whatever the dependent
    apps have registered against entity creation (procurement's approval ladders,
    its default stock location, payments' payout approval). A bare
    ``LedgerEntity.objects.create`` produces a row that looks provisioned and
    fails at the school's first invoice, with no account to post to.

    So the FAL's job here is the translation only: school in, tenant and entity
    out, and the one-primary rule applied at the boundary.
    """

    @envelope
    def provision_entity(self, school_ref, *, code, name, base_currency="NGN"):
        from vs_finance.models import LedgerEntity
        from vs_finance.provisioning import provision_books
        from vs_tenants.models import Tenant

        school = _school(school_ref)
        wanted = _normalise_entity_code(code)
        if not wanted:
            raise CrossTenantError("An entity code is required to provision books.")

        with transaction.atomic():
            # Serialise on the tenant row. Both this adapter and
            # ``provision_books`` decide by reading first and creating second,
            # and neither read can lock a row that does not exist yet, so
            # without this two concurrent onboardings would each find no books
            # and each create a set.
            Tenant.objects.select_for_update().get(pk=school.tenant_id)

            rows = list(_candidate_entities(school.tenant)[:2])
            if len(rows) > 1:
                raise _ambiguous_primary(
                    school,
                    f"School {school.slug!r} already has more than one active "
                    f"entity; provisioning will not guess which is primary.",
                )
            if rows:
                # Idempotent: a retried onboarding gets the books it already has,
                # even if it now asks for a different code or name. Renaming a
                # school's entity is an edit, not a provisioning step.
                return _available(_entity_handle(rows[0], school_ref))

            clash = LedgerEntity.objects.filter(code=wanted).first()
            if clash is not None and clash.tenant_id != school.tenant_id:
                raise CrossTenantError(
                    f"Entity code {wanted!r} already belongs to another school."
                )
            if clash is not None:
                # Same school, but inactive or a non-tenant kind: hand it back
                # rather than colliding with the platform-wide unique code.
                return _available(_entity_handle(clash, school_ref))

            entity = provision_books(
                # Passed explicitly and never left to the model: LedgerEntity.save
                # assigns the Codex platform tenant when tenant_id is unset, so an
                # omission here would write a school's books into the operator's.
                tenant=school.tenant,
                name=name or school.name,
                code=wanted,
                base_currency=base_currency or None,
                kind=LedgerEntity.Kind.TENANT,
                # The guard below this one. Belt and braces: the read above has
                # already answered, but a caller reaching provision_books twice
                # must find its books rather than be refused.
                reuse_existing=True,
            )
        return _available(_entity_handle(entity, school_ref, was_created=True))

    @envelope
    def resolve_entity(self, school_ref):
        school = _school(school_ref)
        return _available(_entity_handle(_primary_entity(school), school_ref))


# --------------------------------------------------------------------------- #
# Component 3 - Student -> Customer resolver
#
# Defined before component 2 because cohort billing uses it.
# --------------------------------------------------------------------------- #
def _customer_handle(customer, student_ref, *, was_created=False):
    return CustomerHandle(
        customer_ref=customer.pk,
        student_ref=student_ref,
        entity_ref=customer.entity_id,
        code=customer.code,
        was_created=was_created,
    )


def _existing_customer(entity, student_ref):
    return _customer_qs().filter(
        entity=entity, source_type=SOURCE_TYPE_STUDENT, source_id=str(student_ref),
    ).first()


def _mapped_account(entity, key, what):
    """An entity's account for a well-known role, or a provisioning fault.

    A missing or unusable mapping is a misconfigured set of books, not an outage:
    retrying will not fix it and somebody has to go and select an account, so it
    raises rather than reading as UNAVAILABLE. The mapping is resolved rather
    than a code hard-coded, so an entity that has remapped the role is honoured.
    """
    from vs_finance.account_mappings import resolve_mapped_account
    from vs_finance.exceptions import MissingAccountError

    try:
        return resolve_mapped_account(entity, key)
    except MissingAccountError as exc:
        raise EntityNotProvisioned(
            f"Entity {entity.code!r} has no usable {what} account: {exc}"
        ) from exc


def _receivable_account(entity):
    """The entity's AR control account, without which a customer cannot be billed."""
    from vs_finance.constants import AccountMappingKey

    return _mapped_account(
        entity, AccountMappingKey.ACCOUNTS_RECEIVABLE, "accounts-receivable control",
    )


def _student_row(student_ref):
    """The child this reference names, as the three facts the FAL needs, or None.

    ``None`` covers two different cases on purpose: a reference that is not a
    student's primary key, and one that names no student. Neither is refused
    here. The refs are opaque strings by contract because the ledger stores them
    as strings, and a school that imported receivables before it imported its
    roll has AR accounts whose source predates any student row. Reads stay
    scoped by entity either way, so an unresolvable reference reaches nothing.
    """
    from schools.vs_students.models import Student

    student_id = student_pk(student_ref)
    if student_id is None:
        return None
    return (
        Student.all_objects.filter(pk=student_id)
        .values("tenant_id", "branch_id", "first_name", "last_name")
        .first()
    )


def _student_display_name(row):
    """What the child is called on their own invoice.

    The customer is named for the child rather than for whoever pays, decided
    2026-08-30. A child has several guardians and keeps the same one name; a
    payer can change mid-year, and a billing identity that changes under a
    family is worse than a receipt that names the pupil.
    """
    return " ".join(part for part in (row["first_name"], row["last_name"]) if part)


class _PreviewComplete(Exception):
    """Carries a finished preview out of the transaction that must not survive.

    An exception rather than a flag because rolling the transaction back is the
    whole mechanism: leaving the atomic block by raising is what discards the
    writes. It is module-private and caught one frame up, so it never reaches a
    caller and is never confused with a FAL error.
    """

    def __init__(self, result):
        super().__init__("dry run complete")
        self.result = result


def _class_labels(student_refs, tenant_id):
    """Map each student reference to the class that child is in now.

    SCOPED BY TENANT, and that is not decoration. ``Customer.source_id`` is a
    loose string, not a foreign key, so a school that imported receivables
    before it imported its roll can hold a reference like "42" that means
    nothing here - while some OTHER school's pupil genuinely has primary key 42.
    Filtering on the id alone would print that child's class on this school's
    debtor list. The entity scoping upstream cannot catch it, because the leak
    enters through a value the ledger merely stores.

    One query for the whole page, deliberately: these rows are built inside a
    paginated list, so a per-row lookup would be an N+1 on every fee report the
    product has. ``setdefault`` over an ordering of student then newest session
    picks the current placement without a subquery.

    A reference that resolves to nobody, and a child with no active placement,
    both fall out as absent and the row is left blank. That is the one case the
    old hardcoded ``""`` was right about.
    """
    from schools.vs_students.models import ClassEnrolment

    by_id = {student_pk(ref): ref for ref in student_refs}
    by_id.pop(None, None)
    if not by_id:
        return {}

    labels = {}
    rows = (
        ClassEnrolment.all_objects
        .filter(student_id__in=by_id, is_active=True, tenant_id=tenant_id)
        .order_by("student_id", "-session__start_date")
        .values_list("student_id", "school_class__name")
    )
    for student_id, name in rows:
        labels.setdefault(by_id[student_id], name or "")
    return labels


def _refuse_foreign_student(entity, student_ref):
    """Refuse a child who is another school's pupil.

    Two checks, and the first one is new. Module 11 landed, so the FAL can now
    ask the question the specification always wanted asked: does this child
    attend the school whose books are about to bill them? Before there was a
    student roll this could only be approximated, and the approximation is the
    second check.

    The second is still worth keeping. It catches a child whose reference does
    not resolve to a student row but who already has an AR account under another
    tenant, which is the shape a part-migrated school arrives in.
    """
    row = _student_row(student_ref)
    if row is not None and row["tenant_id"] != entity.tenant_id:
        raise CrossTenantError(
            f"Student {student_ref!r} attends another school, so this school's "
            f"books may not bill them."
        )

    foreign = (
        _customers_for_student(student_ref)
        .exclude(entity__tenant_id=entity.tenant_id)
        .first()
    )
    if foreign is not None:
        raise CrossTenantError(
            f"Student {student_ref!r} is already billed in another school."
        )


class DjangoStudentCustomerAdapter(StudentCustomerPort):
    """Component 3 over ``vs_finance.Customer``'s loose source reference."""

    @envelope
    def ensure_customer(self, student_ref, *, entity_ref, name=None, code=None,
                        branch_ref=None):
        from vs_finance.models import Customer, LedgerEntity

        entity = _entity(entity_ref)
        _refuse_foreign_student(entity, student_ref)

        # Both defaults come from the roll, and both are corrections rather than
        # conveniences. The name is the child's because the child is who the
        # account is for. The branch is the child's because the engine's own rule
        # is that the customer decides where a receivable is filed, so a Lekki
        # pupil's fees must land in Lekki's books rather than school-wide.
        row = _student_row(student_ref)
        if not name:
            if row is None:
                raise CustomerNotProvisioned(
                    f"Student {student_ref!r} names no child on the roll, so the "
                    f"FAL has no name to open an account under. Pass one."
                )
            name = _student_display_name(row)
        if branch_ref is None and row is not None:
            branch_ref = row["branch_id"]

        branch = _branch(branch_ref, entity.tenant) if branch_ref is not None else None

        with transaction.atomic():
            # Lock the entity row so the check-then-create is serialised. The
            # (entity, code) unique constraint cannot do this job on its own:
            # two concurrent first-billings for the same child are allocated
            # *different* codes, so nothing would collide and the school would
            # end up with two AR accounts for one pupil.
            LedgerEntity.objects.select_for_update().get(pk=entity.pk)

            existing = _existing_customer(entity, student_ref)
            if existing is not None:
                return _available(_customer_handle(existing, student_ref))

            try:
                customer = Customer.objects.create(
                    entity=entity,
                    branch=branch,
                    code=code or "",
                    name=name,
                    # Without a receivable account the customer exists but cannot
                    # be billed: posting refuses it, and the school discovers this
                    # on the first fee run rather than at provisioning. The
                    # mapping is resolved rather than hard-coded to 1200 so an
                    # entity that has remapped its AR control account is honoured.
                    receivable_account=_receivable_account(entity),
                    source_type=SOURCE_TYPE_STUDENT,
                    source_id=str(student_ref),
                )
            except IntegrityError:
                # An explicit ``code`` that another customer already holds. Re-read
                # rather than fail: the winning row is the right answer.
                recovered = _existing_customer(entity, student_ref)
                if recovered is None:
                    raise CustomerCreationRace(
                        f"Could not create or recover an AR customer for student "
                        f"{student_ref!r}; retry the billing action."
                    )
                return _available(_customer_handle(recovered, student_ref))

        return _available(_customer_handle(customer, student_ref, was_created=True))

    @envelope
    def customer_for(self, student_ref, *, entity_ref):
        entity = _entity(entity_ref)
        existing = _existing_customer(entity, student_ref)
        if existing is None:
            return _available(None)
        return _available(_customer_handle(existing, student_ref))

    @envelope
    def set_customer_active(self, student_ref, *, active, reason="", actor_ref=None):
        """Deactivate or reactivate every AR account of this child in their own school.

        Read from the roll's tenant, so a stray account another school opened under
        the same loose reference is never touched. Returns how many accounts changed.
        """
        from vs_finance.customers import set_customer_active

        row = _student_row(student_ref)
        customers = _customers_for_student(student_ref)
        if row is not None:
            customers = customers.filter(entity__tenant_id=row["tenant_id"])
        actor = _user(actor_ref) if actor_ref is not None else None
        changed = 0
        for customer in customers.order_by("pk"):
            if customer.is_active != bool(active):
                set_customer_active(customer, bool(active), actor_user=actor, reason=reason)
                changed += 1
        return _available(changed)


# --------------------------------------------------------------------------- #
# Component 2 - Fee structure <-> term bridge
# --------------------------------------------------------------------------- #
def _academics():
    from schools.vs_academics.models import AcademicSession, AcademicTerm

    return AcademicSession, AcademicTerm


def _fee_structure(fee_structure_ref):
    from vs_finance.models import FeeStructure

    structure = (
        FeeStructure.objects
        .filter(pk=fee_structure_ref)
        .select_related("entity", "entity__tenant")
        .first()
    )
    if structure is None:
        raise CrossTenantError(f"No fee structure {fee_structure_ref!r} is visible.")
    return structure


def _link_dto(link):
    return FeeTermLink(
        fee_structure_ref=link.fee_structure_id,
        session_ref=link.session_id,
        term_ref=link.term_id,
        entity_ref=link.fee_structure.entity_id,
        session_label=link.session.name,
        term_label=link.term.name if link.term_id else "",
    )


def _translate_branch_refusal(exc, field):
    """Turn a DRF refusal from :mod:`vs_rbac.scoping`'s branch rules into a FAL one.

    The scoping helpers speak DRF because the screens call them; the FAL's
    callers speak its own exceptions. A 400 asks the caller to name a branch, a
    403 is a branch outside their reach, and anything else is not a branch
    refusal and is returned untouched.
    """
    from rest_framework.exceptions import PermissionDenied
    from rest_framework.exceptions import ValidationError as DRFValidationError

    if isinstance(exc, DRFValidationError):
        return BranchRequiredError(_drf_message(exc.detail), field=field)
    if isinstance(exc, PermissionDenied):
        return CrossBranchError(str(exc.detail))
    return exc


def _raised_transaction_branch_id(user, tenant, branch_ref, *, field="branch_ref"):
    """The branch a transaction *user* raises in *tenant* names, by their grants.

    :func:`vs_rbac.scoping.raised_transaction_branch`, the rule every screen
    raises a transaction by, asked for a caller the FAL holds as a user rather
    than a request: a caller pinned to one branch raises for it and may name no
    other; a caller covering several, or a school-wide caller at a school with
    several, must name one; a school with one branch gives its only one. A
    branch of another school is :class:`CrossTenantError`, as everywhere in the
    FAL, before any of that is asked.

    ``user`` may be ``None`` for a run with nobody behind it, which reaches the
    whole school.
    """
    from types import SimpleNamespace

    from rest_framework.exceptions import APIException

    from vs_rbac.scoping import raised_transaction_branch

    if branch_ref is not None:
        _branch(branch_ref, tenant)
    try:
        branch = raised_transaction_branch(
            SimpleNamespace(user=user), tenant, {"branch": branch_ref},
        )
    except APIException as exc:
        raise _translate_branch_refusal(exc, field) from exc
    return getattr(branch, "pk", None)


def _cohort_raiser(raiser_ref, effective_user, tenant):
    """Whose reach bounds a fee run: the named raiser, else the request's user.

    A named raiser of another school is refused. Without one, the effective user
    of the request in progress is the raiser when they belong to this school;
    otherwise nobody is, and the run is bounded by the school alone, as for a
    management command.
    """
    if raiser_ref is not None:
        user = _user(raiser_ref)
        if user is None or user.tenant_id != tenant.pk:
            raise CrossTenantError("That user does not belong to this school.")
        return user
    if getattr(effective_user, "tenant_id", None) == tenant.pk:
        return effective_user
    return None


class _RunBranch:
    """The branch a fee run names for a family that gives none of its own.

    A pupil's invoice names the pupil's branch, so most runs never ask this. A
    family every branch shares with no pupil behind it (a receivable imported
    before the roll) has no branch to give, and their invoice takes the
    raiser's, by :func:`_raised_transaction_branch_id`. A named ``branch_ref``
    is checked at once, so a bad one is refused whoever the cohort holds; an
    absent one is asked about only when such a family is billed.
    """

    def __init__(self, raiser, tenant, branch_ref):
        self._raiser, self._tenant, self._ref = raiser, tenant, branch_ref
        self._resolved = False
        self._id = None
        if branch_ref is not None:
            self.id()

    def id(self):
        if not self._resolved:
            self._id = _raised_transaction_branch_id(self._raiser, self._tenant, self._ref)
            self._resolved = True
        return self._id


def _cohort_branches(structure, pairs, to_bill, *, raiser, run_branch):
    """Every invoice's branch, and the pupils' accounts the run re-files.

    Returns ``({customer id: branch id}, {customer id: branch id})``: the branch
    each invoice this run raises names, and, for each pupil about to be billed
    whose account is filed at a branch other than the one they attend, the
    branch that account moves to.

    A pupil's own branch on the roll comes first: ``Student.branch`` is never
    null and a child attends exactly one branch, while a class may be shared by
    several. A family with no pupil behind it gives its account's branch, and
    one with neither takes the run's (:class:`_RunBranch`).

    Two refusals, each before anything is written and each over the whole
    cohort, so a run bills everyone named or nobody:

    * :class:`CrossBranchError` (404) for a family whose branch the raiser
      cannot reach. The roll decides, not the account: Tunde attends Ikeja, so a
      Lekki-bound bursar who names them is refused exactly as for an id that
      names nobody, wherever Tunde's account happens to be filed.
    * :class:`OffPriceListError` (409) when the structure is one branch's price
      list and a family belongs to another branch, or to every branch.

    A pupil whose account is filed elsewhere is billed where they attend, and
    their account and open balance follow them (:func:`_refile_accounts`).
    Only pupils about to be billed move: one already billed for this period is
    skipped, so a rerun bills nobody twice and moves nobody. An account every branch shares stays
    shared; its bill names the pupil's branch.

    The branch is not part of the billing key. A rerun finds a pupil's invoice
    for the period by account, structure and period whichever branch it names.
    """
    from schools.vs_students.models import Student
    from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids

    tenant = structure.entity.tenant
    ids = {student_pk(ref) for ref, _customer in pairs} - {None}
    pupils = dict(
        Student.all_objects.filter(tenant=tenant, pk__in=ids).values_list("pk", "branch_id")
    ) if ids else {}

    own = {}
    for ref, customer in pairs:
        pupil_branch = pupils.get(student_pk(ref))
        own[customer.pk] = (
            pupil_branch if pupil_branch is not None else customer.branch_id
        )

    reach = visible_branch_ids(raiser, tenant) if raiser is not None else WHOLE_TENANT
    if reach is not WHOLE_TENANT and any(
        branch_id is not None and branch_id not in reach for branch_id in own.values()
    ):
        raise CrossBranchError("No such student.")

    if structure.branch_id:
        off = sum(1 for branch_id in own.values() if branch_id != structure.branch_id)
        if off:
            raise OffPriceListError(
                f"{off} of the children named attend another branch. This fee "
                f"structure prices one branch only; bill them from their own "
                f"branch's structure."
            )

    moves = {
        customer.pk: own[customer.pk] for customer in to_bill
        if customer.branch_id is not None and own[customer.pk] != customer.branch_id
    }
    branches = {
        customer.pk: own[customer.pk] if own[customer.pk] is not None else run_branch.id()
        for customer in to_bill
    }
    return branches, moves


def _balance_words(amount):
    """A moved balance as an audit entry says it: owed, in credit, or nothing owed."""
    from vs_finance.money import format_naira

    if amount > 0:
        return f"{format_naira(amount)} owed"
    if amount < 0:
        return f"{format_naira(-amount)} in credit"
    return "nothing owed"


def _move_key(structure, period_key, customer_id, from_id, to_id):
    """The receivable move's idempotency key: one pupil, one branch pair, one run.

    ``fee-run:F<structure>:<period key>:C<customer>:B<from>-B<to>``. A run of the
    same structure for the same period that reaches the same move names the same
    key, so the engine answers it with the move already booked rather than
    booking a second; a different structure or period, or a different pair of
    branches, is a different move. The key is at most 96 characters long.
    """
    return f"fee-run:F{structure.pk}:{period_key}:C{customer_id}:B{from_id}-B{to_id}"[:96]


def _refile_accounts(to_bill, moves, *, actor_user, structure, period_key, on):
    """Re-file each pupil's account in ``moves`` at the branch they attend, balance and all.

    The account is a shared record that follows its pupil, and so does what the
    pupil owes: the run that bills Tunde at Lekki files Tunde's account at Lekki
    and hands Lekki their open balance, in the same transaction, so one branch
    chases everything Tunde owes. The balance moves through
    :func:`vs_finance.inter_branch.transfer_open_receivables`: open invoices and
    debit notes are given Lekki's branch (Tunde's First Term bill becomes
    Lekki's to collect), unapplied credit follows as a receipt at Lekki, and the
    income on a moved bill not yet earned at ``on`` moves with it. Income
    already earned stays Ikeja's, and Lekki owes Ikeja for it through the
    inter-branch account. ``on`` is the run's invoice date, so the move and the
    bills it precedes are booked on the same day.

    Each account is locked, in id order, before its move is decided, and the
    move runs from the branch the locked row names. A run that queued behind
    another that already moved the account finds it at the branch the pupil
    attends and moves nothing. The engine's move is keyed per pupil, branch
    pair, structure and period (:func:`_move_key`), which the database holds
    unique, so no two runs of one structure and period can each book the move.

    The account is changed through :func:`vs_finance.customers.update_customer`,
    the one way an account is changed, and the move is written to the finance
    audit trail once for each side, because a branch-bound reader sees only
    their own branches' entries:

    * the entry ``update_customer`` writes is the arriving side, filed under
      Lekki ("moved from Ikeja with N145,000.00 owed"), the account's branch as
      it now stands;
    * a second entry is the leaving side, filed under Ikeja ("moved to Lekki,
      where the pupil attends, with N145,000.00 owed"), so Ikeja's bursar,
      wondering why Tunde and their First Term bill left the list, finds the
      reason.

    Both carry the branch ids before and after and the net balance that moved
    (negative for a pupil in credit), and no other figure: what each branch
    earned or holds is in its own books, not in the other's trail. Returns the
    moves as :class:`~schools.core.fal.contracts.AccountMove` values, in the
    order the run bills, each with the balance that moved.
    """
    from vs_finance.audit import record
    from vs_finance.constants import FinanceAuditAction
    from vs_finance.customers import update_customer
    from vs_finance.inter_branch import transfer_open_receivables
    from vs_finance.models import Customer
    from vs_tenants.models import Branch

    if not moves:
        return ()
    filed = dict(
        Customer.objects.select_for_update(of=("self",))
        .filter(pk__in=list(moves)).order_by("pk").values_list("pk", "branch_id")
    )
    names = dict(
        Branch.all_objects.filter(
            pk__in={b for b in filed.values() if b is not None} | set(moves.values()),
        ).values_list("pk", "name")
    )
    done = []
    for customer in to_bill:
        if customer.pk not in moves or customer.pk in {m.customer_ref for m in done}:
            continue
        from_id, to_id = filed[customer.pk], moves[customer.pk]
        customer.branch_id = from_id
        if from_id is None or from_id == to_id:
            continue
        moved = transfer_open_receivables(
            customer, from_id, to_id, actor_user, move_date=on,
            move_key=_move_key(structure, period_key, customer.pk, from_id, to_id),
            purpose=f"Fee run: pupil attends {names[to_id]}",
        )
        balance = _balance_words(moved.amount)
        details = {
            "from_branch": names[from_id], "to_branch": names[to_id],
            "amount": moved.amount, "receivable_move_id": moved.transfer_id,
        }
        update_customer(
            customer, {"branch_id": to_id}, actor_user=actor_user,
            message=(
                f"Customer {customer.code} ({customer.name}) moved from "
                f"{names[from_id]} with {balance}, billed here where the pupil attends."
            ),
            **details,
        )
        record(
            entity=customer.entity, action=FinanceAuditAction.CUSTOMER_UPDATED,
            actor_user=actor_user, target=customer, branch=from_id,
            message=(
                f"Customer {customer.code} ({customer.name}) moved to "
                f"{names[to_id]}, where the pupil attends, with {balance}."
            ),
            before={"branch_id": from_id}, after={"branch_id": to_id}, **details,
        )
        done.append(AccountMove(
            customer_ref=customer.pk, student_ref=customer.source_id,
            name=customer.name, from_branch_ref=from_id, from_branch=names[from_id],
            to_branch_ref=to_id, to_branch=names[to_id],
            amount=moved.amount, invoice_count=moved.invoice_count,
            debit_note_count=moved.debit_note_count, credit_amount=moved.credit_amount,
            deferred_amount=moved.deferred_amount,
        ))
    return tuple(done)


class DjangoFeeTermBridgeAdapter(FeeTermBridgePort):
    """Component 2 over the FAL-owned link table and ``vs_finance.fees``."""

    @envelope
    def link_term(self, fee_structure_ref, session_ref, term_ref=None):
        from ..models import FeeStructureTermLink

        AcademicSession, AcademicTerm = _academics()
        structure = _fee_structure(fee_structure_ref)

        # all_objects, not objects: the academic managers apply the ambient
        # thread-local tenant, and the FAL is given an explicit ref and does its
        # own comparison two lines below.
        session = AcademicSession.all_objects.filter(pk=session_ref).first()
        if session is None or session.tenant_id != structure.entity.tenant_id:
            raise CrossTenantError(
                f"Academic session {session_ref!r} does not belong to this school."
            )

        term = None
        if term_ref is not None:
            term = AcademicTerm.all_objects.filter(pk=term_ref).first()
            if term is None or term.session_id != session.pk:
                word = term_word(session.tenant)
                raise InvalidTermLinkError(
                    f"{word.capitalize()} {term_ref!r} is not a {word} of "
                    f"session {session.name!r}."
                )

        link, _created = FeeStructureTermLink.objects.update_or_create(
            fee_structure=structure, defaults={"session": session, "term": term},
        )
        link.fee_structure = structure
        link.session = session
        link.term = term
        return _available(_link_dto(link))

    @envelope
    def generate_cohort_invoices(self, fee_structure_ref, student_refs, *, period=None,
                                 dry_run=False, raiser_ref=None, branch_ref=None):
        """Bill a cohort against a fee structure, or preview what billing would do.

        A preview runs the real thing and throws the writes away, deliberately
        rather than lazily. Fee items are priced and taxed inside ``post_invoice``,
        so a second implementation that summed the item amounts would quote a
        pre-tax figure, and be wrong in exactly the case a bursar most needs it
        right. Running the real code also means every refusal a real run would
        raise is raised here: a preview that hid the cross-tenant error would
        promise a run that then fails.

        The invoice pks are dropped rather than returned. They stop existing when
        the block exits, and handing back identifiers for rows nobody can fetch is
        the kind of honest-looking answer that costs an afternoon.

        Every charge line carries the linked term's first and last day as its
        service period, or the session's for a structure billing the whole year.
        Second Term fees billed in December for a term that starts in January are
        therefore deferred income in December and revenue month by month from
        January; a term already under way is billed as revenue on the day.

        Every invoice names its branch, decided here and handed to the engine
        per customer (:func:`_cohort_branches`): a pupil is billed in the branch
        they attend on the roll, and the run refuses, before anything is
        written, a pupil outside the raiser's reach and a family off a branch
        structure's price list. A pupil whose account is filed at another
        branch is billed where they attend, and their account and open balance
        move with them (:func:`_refile_accounts`); a preview lists the moves it
        would make, with the balance each would carry.
        """
        from vs_finance import fees
        from vs_finance.models import Customer

        from ..models import FeeStructureTermLink

        structure = _fee_structure(fee_structure_ref)
        tenant = structure.entity.tenant
        # The effective user, not the actor: where a CodeX operator is acting
        # inside a school's session, the bill is the school's act.
        _actor, effective_user, _proxy = get_current_audit_identity()
        raiser = _cohort_raiser(raiser_ref, effective_user, tenant)
        run_branch = _RunBranch(raiser, tenant, branch_ref)
        link = (
            FeeStructureTermLink.objects
            .filter(fee_structure=structure)
            .select_related("session", "term")
            .first()
        )
        if link is None:
            raise TermNotLinkedError(
                f"Fee structure {structure.code!r} is not linked to a "
                f"{term_word(structure.entity.tenant)}, so the FAL will not raise "
                f"invoices nobody can attribute to a period."
            )
        if period is not None and (
            period.session_ref != link.session_id or period.term_ref != link.term_id
        ):
            raise InvalidTermLinkError(
                f"Fee structure {structure.code!r} bills a different period from the "
                f"one requested."
            )
        link_period = Period(session_ref=link.session_id, term_ref=link.term_id)

        def _run():
            """The billing run itself, identical whether or not it is kept."""
            pairs = []
            for ref in student_refs:
                customer = _existing_customer(structure.entity, ref)
                if customer is None:
                    # Create on first billing, which is what this method was
                    # always specified to do. It could not, while no roll
                    # existed to read a child's name from, so it refused instead.
                    # Module 11 landed and the refusal is no longer the honest
                    # answer; it is kept only for a reference that names nobody.
                    handle = DjangoStudentCustomerAdapter().ensure_customer(
                        ref, entity_ref=structure.entity_id,
                    ).unwrap()
                    customer = Customer.objects.get(pk=handle.customer_ref)
                pairs.append((ref, customer))

            # Billed already for this period, or a child who has left.
            already = fees.already_billed_customer_ids(
                structure, [c.pk for _, c in pairs], link.period_key,
            ) | {c.pk for _ref, c in pairs if not c.is_active}
            skipped = tuple(ref for ref, c in pairs if c.pk in already)
            billable = tuple(ref for ref, c in pairs if c.pk not in already)
            to_bill = [c for _ref, c in pairs if c.pk not in already]
            branches, moves = _cohort_branches(
                structure, pairs, to_bill, raiser=raiser, run_branch=run_branch,
            )
            # One date for the run: the pinned branch's day, else the school's.
            invoice_date = branch_today(structure.entity.tenant, structure.branch_id)
            accounts_moved = _refile_accounts(
                to_bill, moves, actor_user=effective_user or raiser,
                structure=structure, period_key=link.period_key, on=invoice_date,
            )

            # The school's own rule, resolved here rather than in the engine.
            # Three of the four bases are academic-calendar facts, and the term
            # this structure bills is known at exactly this point; vs_finance
            # gets the answer as a plain date and stays ignorant of terms.
            basis, days_after = policy_for(structure.entity.tenant_id)
            due_date = resolve_due_date(
                basis=basis, days_after=days_after, invoice_date=invoice_date,
                term_end=link.term.end_date if link.term_id else None,
                session_end=link.session.end_date,
            )

            # The fees pay for the term's teaching (the session's, for a whole-year
            # fee), so those dates are the service period. The engine gets plain
            # dates and defers what is billed before they start.
            taught = link.term if link.term_id else link.session
            invoices = fees.generate_invoices(
                structure, to_bill, actor_user=effective_user or raiser,
                invoice_branches=branches,
                invoice_date=invoice_date, due_date=due_date,
                billing_period=link.period_key, billing_period_label=link.label,
                service_start=taught.start_date, service_end=taught.end_date,
            ) if to_bill else []

            return InvoiceGenerationResult(
                fee_structure_ref=structure.pk,
                period=link_period,
                invoices_created=tuple(inv.pk for inv in invoices),
                students_skipped=skipped,
                total_billed=sum(inv.total for inv in invoices),
                students_to_bill=billable,
                dry_run=dry_run,
                due_date=due_date,
                accounts_moved=accounts_moved,
            )

        if not dry_run:
            with transaction.atomic():
                return _available(_run())

        # Preview: run it for real, then discard the writes. See the docstring.
        try:
            with transaction.atomic():
                raise _PreviewComplete(_run())
        except _PreviewComplete as preview:
            return _available(replace(preview.result, invoices_created=()))



# --------------------------------------------------------------------------- #
# Component 4 - School-scoped finance RBAC
# --------------------------------------------------------------------------- #
class DjangoFinanceRbacAdapter(FinanceRbacPort):
    """Component 4 over ``vs_rbac.evaluator.has_permission``."""

    @envelope
    def can(self, user_ref, permission_key, *, entity_ref, branch_ref=None):
        from vs_rbac.evaluator import ANY_BRANCH, has_permission

        entity = _entity(entity_ref)
        user = _user(user_ref)
        if user is None:
            # Fail closed, and not an exception: "who is this?" has a real answer
            # here, and it is no.
            return _available(False)

        # ANY_BRANCH, not None. The evaluator treats an explicit ``None`` as a
        # real scope - "the entity as a whole" - which discards every
        # branch-pinned grant, so passing None whenever the caller named no
        # branch would deny a Lekki-scoped bursar everything, everywhere. The
        # sentinel is the "no branch was named" value.
        branch = ANY_BRANCH
        if branch_ref is not None:
            branch = _branch(branch_ref, entity.tenant)

        return _available(bool(
            has_permission(user, permission_key, tenant=entity.tenant, branch=branch)
        ))


# --------------------------------------------------------------------------- #
# Component 5 - Finance reads
# --------------------------------------------------------------------------- #
#: ``FilterClause.op`` -> ORM lookup suffix. Anything outside this map is a
#: caller error, which is what keeps a clause from ever reaching raw SQL.
_OPS = {
    "eq": "exact",
    "in": "in",
    "gte": "gte",
    "lte": "lte",
    "contains": "icontains",
}

#: Per-source field whitelists. The key is what a consumer names; the value is
#: the ORM path it is allowed to become. A field outside its source's map raises
#: InvalidFilterError before any query is built.
_ALLOWED_FILTER_FIELDS = {
    "debtors": {
        "branch_ref": "customer__branch_id",
        "customer_ref": "customer_id",
        "student_ref": "customer__source_id",
        "due_date": "due_date",
    },
    "fee_invoices": {
        "branch_ref": "branch_id",
        "customer_ref": "customer_id",
        "student_ref": "customer__source_id",
        "status": "payment_status",
        "invoice_date": "invoice_date",
        "due_date": "due_date",
        "reference": "reference",
    },
    "payments": {
        "branch_ref": "branch_id",
        "customer_ref": "customer_id",
        "student_ref": "customer__source_id",
        "method": "method",
        "payment_date": "payment_date",
    },
    "procurement": {
        "branch_ref": "branch_id",
        "status": "status",
        "approval_state": "approval_state",
        "request_date": "request_date",
    },
}


def _filter_q(source: str, filters: tuple[FilterClause, ...]) -> Q:
    """Turn whitelisted clauses into a ``Q``. Never touches SQL text."""
    allowed = _ALLOWED_FILTER_FIELDS[source]
    q = Q()
    for clause in filters or ():
        column = allowed.get(clause.field)
        if column is None:
            raise InvalidFilterError(
                f"'{clause.field}' is not a filterable field of the {source} source."
            )
        lookup = _OPS.get(clause.op)
        if lookup is None:
            raise InvalidFilterError(f"'{clause.op}' is not a supported filter operator.")
        q &= Q(**{f"{column}__{lookup}": clause.value})
    return q


#: Outstanding on an invoice, expressed once so every aggregate agrees with the
#: model's own ``balance_due`` property.
_BALANCE = F("total") - F("amount_paid") - F("amount_credited")


def _page(queryset_or_list, page: int, page_size: int, build):
    """Page a queryset with one COUNT and one sliced SELECT."""
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 100))
    total = queryset_or_list.count()
    start = (page - 1) * page_size
    rows = tuple(build(obj) for obj in queryset_or_list[start:start + page_size])
    total_pages = (total + page_size - 1) // page_size if page_size else 1
    return Page(
        items=rows, page=page, page_size=page_size,
        total_items=total, total_pages=max(total_pages, 1),
    )


def _labelled(page, tenant_id):
    """Fill ``class_label`` on a built page of rows, in one query for the page.

    Applied after ``_page`` rather than inside the row builder because the class
    lookup wants every student on the page at once. Doing it per row would issue
    one query per debtor, on a list whose whole purpose is to be long.

    Rows are frozen, so this rebuilds them. That is cheap at a page's size and
    keeps the contract immutable, which is what the rest of the FAL relies on.
    """
    if not page.items:
        return page
    labels = _class_labels((row.student_ref for row in page.items), tenant_id)
    if not labels:
        return page
    return replace(page, items=tuple(
        replace(row, class_label=labels.get(row.student_ref, ""))
        for row in page.items
    ))


def _period_q(period: Optional[Period]):
    """The invoices a period covers, from the period each fee run stamped, or ``None``.

    ``None`` means "no period was asked for, do not narrow". The stamp
    (``Invoice.billing_period``) is fixed when the invoice posts, so which term a
    bill reports under never depends on how its fee structure is linked today. A
    session asked for without a term covers the session-wide fees and every term's.
    """
    if period is None:
        return None
    from ..models import period_key, session_period_q

    if period.term_ref is not None:
        return Q(billing_period=period_key(period.session_ref, period.term_ref))
    return session_period_q(period.session_ref)


def _invoice_qs(entity, branch_ref=None, period=None):
    from vs_finance.models import Invoice

    qs = Invoice.objects.filter(entity=entity, status="POSTED")
    if branch_ref is not None:
        qs = qs.filter(branch_id=branch_ref)
    covered = _period_q(period)
    if covered is not None:
        qs = qs.filter(covered)
    return qs


def _payment_qs(entity, branch_ref=None):
    """Receipts that brought money in (a credit transfer moves credit already held)."""
    from vs_finance.collected import received_money_q
    from vs_finance.models import Payment

    qs = Payment.objects.filter(received_money_q(), entity=entity, status="POSTED")
    if branch_ref is not None:
        qs = qs.filter(branch_id=branch_ref)
    return qs


def _ageing_bucket(due_date, today):
    """The bucket a bill falls in by the date it fell due (its invoice date if none)."""
    if due_date is None or due_date >= today:
        return AgeingBucket.CURRENT
    days = (today - due_date).days
    for bucket, floor in _AGEING_EDGES:
        if days > floor:
            return bucket
    return AgeingBucket.CURRENT


def _ageing_case(today):
    """Bucket an invoice by days past due, in the database, with no raw SQL.

    The thresholds are computed here as dates and compared, rather than asking
    the database to subtract dates: date arithmetic is the part of SQL that
    differs most between backends, and this needs none of it.
    """
    due = Coalesce("due_date", "invoice_date")
    return Case(
        When(Q(aged_from__gte=today), then=Value(AgeingBucket.CURRENT.value)),
        When(aged_from__lt=today - datetime.timedelta(days=90),
             then=Value(AgeingBucket.DAYS_90_PLUS.value)),
        When(aged_from__lt=today - datetime.timedelta(days=60),
             then=Value(AgeingBucket.DAYS_61_90.value)),
        When(aged_from__lt=today - datetime.timedelta(days=30),
             then=Value(AgeingBucket.DAYS_31_60.value)),
        default=Value(AgeingBucket.DAYS_1_30.value),
    ), due


_PAYMENT_METHODS = {
    "CASH": PaymentMethod.CASH,
    "BANK_TRANSFER": PaymentMethod.TRANSFER,
    "CARD": PaymentMethod.CARD,
    "ONLINE": PaymentMethod.ONLINE,
    "CHEQUE": PaymentMethod.OTHER,
    "OTHER": PaymentMethod.OTHER,
}


def _invoice_view(invoice):
    return InvoiceView(
        invoice_ref=invoice.pk,
        student_ref=invoice.customer.source_id,
        term_label=invoice.billing_period_label,
        lines=tuple(
            InvoiceLine(description=line.description, amount=line.net_amount + line.tax_amount)
            for line in invoice.lines.all()
        ),
        amount_due=invoice.total,
        amount_paid=invoice.amount_paid,
        balance=invoice.balance_due,
        status=InvoiceStatus(invoice.payment_status),
    )


class DjangoFinanceReadAdapter(FinanceReadPort):
    """Component 5. Every method scopes to the school's entity before it counts."""

    # ----- scope ----------------------------------------------------------- #
    def _entity_of(self, school_ref):
        return _primary_entity(_school(school_ref))

    # ----- headline KPIs --------------------------------------------------- #
    @envelope
    def collections(self, school_ref, branch_ref=None, period=None):
        """Money in: for a period, what was applied to its bills; else every receipt.

        For a period this is finance's own definition (:mod:`vs_finance.collected`),
        the one the finance dashboard reads: a receipt paying two terms counts in
        each for the part it paid there, and money left as credit counts in no term.
        """
        from vs_finance.collected import billed_and_collected

        entity = self._entity_of(school_ref)
        if period is not None:
            _billed, total = billed_and_collected(_invoice_qs(entity, branch_ref, period))
        else:
            total = _payment_qs(entity, branch_ref).aggregate(t=Sum("amount"))["t"] or 0
        return _available(KpiValue(value=total, unit=Unit.KOBO, label="Collections"))

    @envelope
    def outstanding(self, school_ref, branch_ref=None, period=None):
        entity = self._entity_of(school_ref)
        total = (
            _invoice_qs(entity, branch_ref, period)
            .annotate(bal=_BALANCE).filter(bal__gt=0)
            .aggregate(t=Sum("bal"))["t"] or 0
        )
        return _available(KpiValue(value=total, unit=Unit.KOBO, label="Outstanding"))

    @envelope
    def collection_rate(self, school_ref, branch_ref=None, period=None):
        from vs_finance.collected import billed_and_collected, collection_rate_bps

        entity = self._entity_of(school_ref)
        # Finance's own definition: billed is net of scholarships, discounts and
        # credit notes. A school that has billed nothing has collected 100% of
        # nothing, and reporting that as 0% would read as a crisis.
        billed, paid = billed_and_collected(_invoice_qs(entity, branch_ref, period))
        rate = collection_rate_bps(billed, paid)
        return _available(KpiValue(
            value=rate, unit=Unit.RATIO, scale=BPS, label="Collection rate",
        ))

    @envelope
    def debtor_count(self, school_ref, branch_ref=None, period=None):
        entity = self._entity_of(school_ref)
        count = (
            _invoice_qs(entity, branch_ref, period)
            .annotate(bal=_BALANCE).filter(bal__gt=0)
            .values("customer_id").distinct().count()
        )
        return _available(KpiValue(value=count, unit=Unit.COUNT, label="Debtors"))

    @envelope
    def payment_trend(self, school_ref, branch_ref=None, date_range=None):
        entity = self._entity_of(school_ref)
        qs = _payment_qs(entity, branch_ref)
        if date_range is not None:
            qs = qs.filter(payment_date__gte=date_range.start,
                           payment_date__lte=date_range.end)
        rows = (
            qs.annotate(month=TruncMonth("payment_date"))
            .values("month").annotate(total=Sum("amount")).order_by("month")
        )
        points = tuple(
            SeriesPoint(label=row["month"].strftime("%Y-%m"), value=row["total"] or 0)
            for row in rows if row["month"] is not None
        )
        return _available(Series(points=points, unit=Unit.KOBO))

    # ----- dashboard contracts --------------------------------------------- #
    @envelope
    def ar_ageing(self, school_ref, branch_ref=None, period=None):
        entity = self._entity_of(school_ref)
        today = branch_today(entity.tenant, branch_ref)
        bucket, aged_from = _ageing_case(today)
        rows = (
            _invoice_qs(entity, branch_ref, period)
            .annotate(bal=_BALANCE).filter(bal__gt=0)
            .annotate(aged_from=aged_from).annotate(bucket=bucket)
            .values("bucket")
            .annotate(total=Sum("bal"), debtors=Count("customer_id", distinct=True))
        )
        by_bucket = {row["bucket"]: row for row in rows}
        buckets = tuple(
            AgeingRow(
                bucket=bucket,
                total=(by_bucket.get(bucket.value) or {}).get("total") or 0,
                debtor_count=(by_bucket.get(bucket.value) or {}).get("debtors") or 0,
            )
            for bucket in (
                AgeingBucket.CURRENT, AgeingBucket.DAYS_1_30, AgeingBucket.DAYS_31_60,
                AgeingBucket.DAYS_61_90, AgeingBucket.DAYS_90_PLUS,
            )
        )
        return _available(ArAgeingReport(
            school_ref=school_ref,
            period=period,
            buckets=buckets,
            total_outstanding=sum(row.total for row in buckets),
        ))

    @envelope
    def fee_liability(self, school_ref, period=None):
        entity = self._entity_of(school_ref)
        qs = _invoice_qs(entity, period=period)
        if period is None:
            # "Fee liability" is about fees, not about every receivable the school
            # has ever raised, so an unscoped call still narrows to fee invoices.
            qs = qs.filter(reference__startswith="FEE:")
        from vs_finance.collected import billed_and_collected

        billed, paid = billed_and_collected(qs)
        return _available(FeeLiability(
            school_ref=school_ref,
            period=period,
            total_billed=billed,
            total_collected=paid,
            total_outstanding=billed - paid,
        ))

    # ----- detail lists ---------------------------------------------------- #
    @envelope
    def debtors(self, school_ref, branch_ref=None, filters=(), page=1, page_size=20):
        entity = self._entity_of(school_ref)
        today = branch_today(entity.tenant, branch_ref)
        qs = (
            _invoice_qs(entity, branch_ref)
            .filter(_filter_q("debtors", filters))
            .annotate(bal=_BALANCE).filter(bal__gt=0)
            .values(
                "customer_id", "customer__name", "customer__source_id",
                "customer__branch_id",
            )
            .annotate(outstanding=Sum("bal"),
                      oldest_due=Min(Coalesce("due_date", "invoice_date")))
            .order_by("-outstanding")
        )

        def build(row):
            return DebtorRow(
                student_ref=row["customer__source_id"] or "",
                student_name=row["customer__name"],
                class_label="",   # filled below, once the page is known
                outstanding=row["outstanding"] or 0,
                ageing=_ageing_bucket(row["oldest_due"], today),
                branch_ref=row["customer__branch_id"],
            )

        return _available(_labelled(_page(qs, page, page_size, build), entity.tenant_id))

    @envelope
    def fee_invoices(self, school_ref, branch_ref=None, filters=(), page=1, page_size=20):
        entity = self._entity_of(school_ref)
        qs = (
            _invoice_qs(entity, branch_ref)
            .filter(_filter_q("fee_invoices", filters))
            .select_related("customer")
            .order_by("-invoice_date", "-pk")
        )

        def build(invoice):
            return FeeRow(
                invoice_ref=invoice.pk,
                student_ref=invoice.customer.source_id or "",
                student_name=invoice.customer.name,
                class_label="",   # filled below, once the page is known
                term_label=invoice.billing_period_label,
                amount_due=invoice.total,
                amount_paid=invoice.amount_paid,
                balance=invoice.balance_due,
                status=InvoiceStatus(invoice.payment_status),
            )

        return _available(_labelled(_page(qs, page, page_size, build), entity.tenant_id))

    @envelope
    def payments(self, school_ref, branch_ref=None, filters=(), page=1, page_size=20):
        entity = self._entity_of(school_ref)
        qs = (
            _payment_qs(entity, branch_ref)
            .filter(_filter_q("payments", filters))
            .select_related("customer")
            .order_by("-payment_date", "-pk")
        )

        def build(payment):
            return PaymentRow(
                payment_ref=payment.pk,
                student_ref=payment.customer.source_id or "",
                student_name=payment.customer.name,
                amount=payment.amount,
                method=_PAYMENT_METHODS.get(payment.method, PaymentMethod.OTHER),
                paid_at=datetime.datetime.combine(
                    payment.payment_date, datetime.time.min,
                    tzinfo=datetime.timezone.utc,
                ),
                reconciled=payment.allocated_amount >= payment.amount,
                gateway_ref=payment.reference or None,
            )

        return _available(_page(qs, page, page_size, build))

    # ----- per-student views ----------------------------------------------- #
    @envelope
    def fee_status(self, student_ref):
        from vs_finance.models import Invoice

        customers = list(_customers_for_student(student_ref))
        if not customers:
            # No AR account is a real, reachable answer: this child has never been
            # billed. Zero here is a fact, not a swallowed failure.
            return _available(FeeStatus(
                student_ref=student_ref, balance=0, total_billed=0,
                total_paid=0, invoices=(),
            ))
        _one_tenant(customers, "This student's AR accounts")
        invoices = list(
            Invoice.objects
            .filter(customer__in=customers, status="POSTED")
            .select_related("customer").prefetch_related("lines")
            .order_by("-invoice_date", "-pk")
        )
        billed = sum(inv.total for inv in invoices)
        paid = sum(inv.amount_paid for inv in invoices)
        credited = sum(inv.amount_credited for inv in invoices)
        return _available(FeeStatus(
            student_ref=student_ref,
            balance=billed - paid - credited,
            total_billed=billed,
            total_paid=paid,
            invoices=tuple(_invoice_view(inv) for inv in invoices),
        ))

    @envelope
    def invoices_for(self, student_ref, include_history=True):
        from vs_finance.models import Invoice

        customers = list(_customers_for_student(student_ref))
        if not customers:
            return _available(())
        _one_tenant(customers, "This student's AR accounts")
        qs = (
            Invoice.objects
            .filter(customer__in=customers, status="POSTED")
            .select_related("customer").prefetch_related("lines")
            .order_by("-invoice_date", "-pk")
        )
        if not include_history:
            qs = qs.annotate(bal=_BALANCE).filter(bal__gt=0)
        return _available(tuple(_invoice_view(inv) for inv in qs))

    @envelope
    def combined_balance(self, student_refs):
        from vs_finance.models import Invoice

        customers = []
        for ref in student_refs or ():
            customers.extend(_customers_for_student(ref))
        if not customers:
            return _available(0)
        _one_tenant(customers, "These children's AR accounts")
        total = (
            Invoice.objects
            .filter(customer__in=customers, status="POSTED")
            .annotate(bal=_BALANCE).filter(bal__gt=0)
            .aggregate(t=Sum("bal"))["t"] or 0
        )
        return _available(total)


# --------------------------------------------------------------------------- #
# Component 6 - Parent portal payment bridge
# --------------------------------------------------------------------------- #
class DjangoGuardianLinkAdapter(GuardianLinkPort):
    """The ownership check, answered from the student roll.

    ``StudentGuardian`` is the link between a child and the people responsible
    for them, and a row's existence is the whole answer decision 5 turns on.

    Two details matter. The pair carries its own tenant and both sides carry
    theirs, so a matching row cannot span two schools and the pair lookup is
    already the isolation check; there is nothing to add. And the read goes
    through ``all_objects`` rather than the tenant-aware manager, for the reason
    every other read in this adapter does: the FAL is called with explicit
    references, sometimes from a task with no request behind it, and a check
    that quietly answered "no" because no ambient tenant was set would fail
    closed in the most confusing way available.

    A reference that is not a number is a real no, not an error. The refs are
    opaque strings by contract, and a caller that hands over a name or a blank
    is asking about a child that does not exist.
    """

    def owns(self, guardian_ref, student_ref):
        from schools.vs_students.models import StudentGuardian

        student_id = student_pk(student_ref)
        try:
            guardian_id = int(guardian_ref)
        except (TypeError, ValueError):
            return False
        if student_id is None:
            return False
        return StudentGuardian.all_objects.filter(
            guardian_id=guardian_id, student_id=student_id,
        ).exists()


class DenyAllGuardianLinkAdapter(GuardianLinkPort):
    """The resolver for a deployment with no student roll: it refuses to answer.

    No longer the default. Module 11 landed and
    :class:`DjangoGuardianLinkAdapter` took its place, which is what opened the
    parent portal's payment bridge.

    It is kept, and kept exported, for two reasons. A deployment that has not
    installed the student module still needs the bridge to fail closed rather
    than crash on a missing import, and pointing ``FAL_GUARDIAN_LINK`` here is
    how it does that. And it remains the honest answer to "we cannot establish
    this relationship": raising rather than returning ``False``, because "this
    guardian is not the parent" is a claim, and a claim nobody has checked must
    not be made on a school's behalf.
    """

    def owns(self, guardian_ref, student_ref):
        raise GuardianLinkNotConfigured(
            "The guardian-to-student link has no source yet, so the FAL cannot "
            "tell whether this guardian may act for this child. Point "
            "FAL_GUARDIAN_LINK at a real resolver."
        )


class DjangoParentPaymentBridgeAdapter(ParentPaymentBridgePort):
    """Component 6 over ``vs_payments.services.initiate_collection``.

    Initiate and read only. It never books: settlement stays inside
    ``vs_payments`` (``confirm_collection`` -> ``_book_receipt``), which is
    already idempotent, and a second settlement path is exactly the way to
    double-credit a parent.
    """

    def __init__(self, guardian_link: Optional[GuardianLinkPort] = None):
        self._guardian_link = guardian_link

    def _links(self) -> GuardianLinkPort:
        if self._guardian_link is not None:
            return self._guardian_link
        from .. import registry

        return registry.get_guardian_link()

    def _assert_owns(self, guardian_ref, customer):
        """The ownership check, run before anything with a side effect."""
        if customer.source_type != SOURCE_TYPE_STUDENT or not customer.source_id:
            raise CrossTenantError(
                "That account is not a student's, so a guardian cannot act on it."
            )
        if not self._links().owns(guardian_ref, customer.source_id):
            raise CrossTenantError(
                "That child is not linked to this guardian."
            )

    @envelope
    def start_payment_session(self, *, guardian_ref, entity_ref, amount,
                              customer_ref=None, invoice_ref=None,
                              payer_email="", callback_url=""):
        from rest_framework.exceptions import ValidationError as DRFValidationError

        from vs_finance.models import Customer, Invoice
        from vs_payments.exceptions import ProviderError, ProviderNotConfiguredError
        from vs_payments.services import initiate_collection

        entity = _entity(entity_ref)

        invoice = None
        if invoice_ref is not None:
            invoice = (
                Invoice.objects.filter(pk=invoice_ref, entity=entity)
                .select_related("customer").first()
            )
            if invoice is None:
                raise CrossTenantError(
                    f"Invoice {invoice_ref!r} does not belong to this school's books."
                )

        customer = invoice.customer if invoice is not None else None
        if customer_ref is not None:
            named = Customer.objects.filter(pk=customer_ref, entity=entity).first()
            if named is None:
                raise CrossTenantError(
                    f"Customer {customer_ref!r} does not belong to this school's books."
                )
            if invoice is not None and invoice.customer_id != named.pk:
                raise CrossTenantError("That invoice belongs to a different child.")
            customer = named

        if customer is None:
            # The backend refuses a customer-free collection, and the FAL says so
            # in its own vocabulary rather than letting a DRF error escape.
            raise PaymentGatewayError(
                "A customer or an invoice is required to start a payment session."
            )

        self._assert_owns(guardian_ref, customer)

        try:
            intent = initiate_collection(
                entity=entity, amount=amount, customer=customer, invoice=invoice,
                payer_email=payer_email, callback_url=callback_url or None,
            )
        except ProviderNotConfiguredError:
            return FinanceResult.unavailable(Unavailable.NOT_CONFIGURED)
        except ProviderError:
            return FinanceResult.unavailable(Unavailable.GATEWAY_UNAVAILABLE)
        except DRFValidationError as exc:
            # A deterministic refusal (unposted invoice, amount over balance).
            # Retrying will not help, so it raises rather than reading as an outage.
            raise PaymentGatewayError(str(exc.detail)) from exc

        return _available(intent.checkout_url)

    @envelope
    def receipt_for(self, payment_ref, *, guardian_ref):
        from vs_finance.models import Payment

        payment = (
            Payment.objects.filter(pk=payment_ref)
            .select_related("customer")
            .prefetch_related("allocations")
            .first()
        )
        if payment is None or payment.status != "POSTED":
            # A payment that does not exist and one that has not settled are the
            # same answer to a parent: there is no receipt yet. Neither reveals
            # whether the id exists.
            return _available(None)

        self._assert_owns(guardian_ref, payment.customer)

        return _available(Receipt(
            payment_ref=payment.pk,
            receipt_number=payment.document_number,
            amount=payment.amount,
            issued_at=payment.created_at,
            invoice_refs=tuple(
                allocation.invoice_id for allocation in payment.allocations.all()
            ),
        ))


# --------------------------------------------------------------------------- #
# Component 7 - Procurement (thin pass-through)
# --------------------------------------------------------------------------- #
def _proc_models():
    from vs_procurement.models import (
        GoodsReceivedNote,
        PurchaseOrder,
        PurchaseRequisition,
        VendorInvoice,
        VendorPayment,
    )

    return {
        ProcDocType.REQUISITION: PurchaseRequisition,
        ProcDocType.PURCHASE_ORDER: PurchaseOrder,
        ProcDocType.GOODS_RECEIPT: GoodsReceivedNote,
        ProcDocType.VENDOR_INVOICE: VendorInvoice,
        ProcDocType.VENDOR_PAYMENT: VendorPayment,
    }


def _translate_procurement(exc):
    """Map a ``vs_procurement`` / ``vs_workflow`` refusal onto a typed FAL error.

    Ordered subclass-first, so the specific refusals keep their identity and
    everything else the engines can say becomes ``ProcurementStateError``. A
    consumer's ``except ProcurementError`` therefore catches procurement
    refusals without an engine exception type leaking through the boundary.

    **Only the engines' own exceptions are translated.** A ``TypeError`` or an
    ``AttributeError`` raised in this adapter is a bug in the FAL, and returning
    it as a domain refusal is exactly how such a bug survives: the caller sees
    "the document is in the wrong state", believes it, and nobody looks. Those
    re-raise untouched, and a FAL error already raised inside the block passes
    through as itself.

    Some of those rules are the checks ``vs_procurement``'s screens run, shared
    with the FAL (the vendor-payment post, for one), and they refuse in DRF's
    terms. A 404 or 403 there means the row is outside the acting user's branches
    and becomes :class:`CrossBranchError`; a 400 is a refusal of the document's
    state and becomes :class:`ProcurementStateError` carrying its message.
    """
    from rest_framework.exceptions import NotFound, PermissionDenied
    from rest_framework.exceptions import ValidationError as DRFValidationError

    from vs_finance.exceptions import FinanceError
    from vs_workflow.exceptions import WorkflowError

    from ..exceptions import FALError
    from vs_procurement import exceptions as proc_exc

    if isinstance(exc, FALError):
        return exc
    if isinstance(exc, (NotFound, PermissionDenied)):
        return CrossBranchError(str(exc.detail))
    if isinstance(exc, DRFValidationError):
        return ProcurementStateError(_drf_message(exc.detail))
    if not isinstance(exc, (FinanceError, WorkflowError)):
        return exc
    if isinstance(exc, proc_exc.ApprovalTemplateMissingError):
        return ApprovalTemplateMissingError(str(exc))
    if isinstance(exc, proc_exc.ApprovalNotParkedError):
        return ApprovalNotParkedError(str(exc))
    if isinstance(exc, (proc_exc.ApprovalOverrideNotPermittedError,
                        proc_exc.ApprovalOverrideReasonError)):
        return OverrideNotPermittedError(str(exc))
    return ProcurementStateError(str(exc))


def _drf_message(detail) -> str:
    """The sentences of a DRF error ``detail``, however it nests, as one string."""
    if isinstance(detail, dict):
        return " ".join(_drf_message(value) for value in detail.values())
    if isinstance(detail, (list, tuple)):
        return " ".join(_drf_message(value) for value in detail)
    return str(detail)


def _amount_of(document):
    """The document's own workflow amount field, whatever it is called."""
    return getattr(document, getattr(document, "workflow_amount_field", "total"), 0) or 0


def _doc_type_of(document):
    for doc_type, model in _proc_models().items():
        if isinstance(document, model):
            return doc_type
    raise ProcurementStateError(f"{type(document).__name__} is not a FAL document type.")


def _proc_document(document, *, overridden=None):
    doc_type = _doc_type_of(document)
    if overridden is None:
        from vs_procurement import approval_override

        overridden = approval_override.is_document_overridden(document)
    return ProcDocument(
        ref=ProcDocRef(
            doc_ref=document.pk, doc_type=doc_type,
            entity_ref=document.entity_id, branch_ref=document.branch_id,
        ),
        document_number=document.document_number or "",
        status=document.status,
        approval_state=ProcApprovalState(
            getattr(document, "approval_state", ProcApprovalState.NOT_SUBMITTED.value)
        ),
        total=_amount_of(document),
        vendor_ref=getattr(document, "vendor_id", None),
        raised_by_ref=getattr(document, "requested_by_id", None)
        or getattr(document, "created_by_id", None),
        approved_by_override=bool(overridden),
    )


def _live_instance(document):
    from vs_workflow.models import WorkflowInstance

    return WorkflowInstance.all_objects.for_document(document).active().first()


def _submission_dto(document, instance=None, *, override=None):
    from vs_procurement import approval_parking

    document.refresh_from_db()
    stage = approval_parking.parked_stage_instance(document)
    return ApprovalSubmission(
        doc_ref=document.pk,
        doc_type=_doc_type_of(document),
        approval_state=ProcApprovalState(document.approval_state),
        workflow_instance_ref=(instance.pk if instance is not None else None),
        is_parked=stage is not None,
        parked_stage_code=(stage.stage.code if stage is not None else None),
        override=override,
    )


class DjangoProcurementActionAdapter(ProcurementActionPort):
    """Component 7. Tenancy, branch, permission, translation - and nothing else.

    Every method below either resolves a reference, checks a scope, calls a
    ``vs_procurement`` service, or turns an engine exception into a FAL one. If a
    future edit here starts computing a total or deciding a match, it is in the
    wrong file.
    """

    # ----- scope helpers --------------------------------------------------- #
    def _resolve(self, doc: ProcDocRef):
        model = _proc_models().get(doc.doc_type)
        if model is None:
            raise ProcurementStateError(f"Unknown document type {doc.doc_type!r}.")
        entity = _entity(doc.entity_ref)
        obj = model.objects.filter(pk=doc.doc_ref, entity=entity).first()
        if obj is None:
            raise CrossTenantError(
                f"{doc.doc_type.value} {doc.doc_ref!r} is not in this school's books."
            )
        if doc.branch_ref is not None and obj.branch_id != doc.branch_ref:
            raise CrossBranchError(
                f"{doc.doc_type.value} {doc.doc_ref!r} belongs to another branch."
            )
        return entity, obj

    def _actor(self, entity, actor_ref):
        user = _user(actor_ref)
        if user is None or user.tenant_id != entity.tenant_id:
            raise CrossTenantError("That user does not belong to this school.")
        return user

    def _resolve_for(self, doc: ProcDocRef, actor_ref):
        """The document, its books and the acting user, when the user may act on it.

        The reach every procurement action screen applies
        (:func:`vs_procurement.views.base._document_or_404`): a branch-bound user
        acts only on their own branches' documents, and one in another branch is
        :class:`CrossBranchError`, exactly as an id that names nothing. Mrs Eze's
        role is granted at Lekki, so they cannot submit, release, order against,
        receive, bill or pay an Ikeja requisition's chain through the FAL any
        more than through the procurement screens.

        Approving and declining do not come through here: who may decide a
        document is the approval ladder's question, and a named approver can sit
        at any branch.
        """
        from types import SimpleNamespace

        from rest_framework.exceptions import NotFound

        from vs_procurement.views.base import _document_or_404

        entity, document = self._resolve(doc)
        user = self._actor(entity, actor_ref)
        try:
            _document_or_404(
                SimpleNamespace(user=user), type(document).objects.filter(entity=entity),
                document.pk, "No such document.",
            )
        except NotFound as exc:
            raise CrossBranchError(
                f"{doc.doc_type.value} {doc.doc_ref!r} belongs to another branch."
            ) from exc
        return entity, document, user

    def _raised_branch(self, entity, user, branch_ref):
        """The branch a new document names, by the raiser's grants.

        The rule every procurement screen raises by
        (:func:`_raised_transaction_branch_id`): a raiser pinned to one branch
        raises for it and may name no other (:class:`CrossBranchError`); one
        covering several, or a school-wide raiser at a school with several, must
        name one (:class:`BranchRequiredError`); a school with one branch gives
        its only one. Head Office is a branch like any other, so a head-office
        purchase names Head Office rather than no branch.
        """
        return _raised_transaction_branch_id(user, entity.tenant, branch_ref)

    # ----- raise ----------------------------------------------------------- #
    @envelope
    def raise_requisition(self, *, entity_ref, raiser_ref, lines, branch_ref=None,
                          narration=""):
        from vs_procurement.models import PurchaseRequisition, PurchaseRequisitionLine

        entity = _entity(entity_ref)
        user = self._actor(entity, raiser_ref)
        branch_id = self._raised_branch(entity, user, branch_ref)
        if not lines:
            raise ProcurementStateError("A requisition needs at least one line.")

        try:
            with transaction.atomic():
                requisition = PurchaseRequisition.objects.create(
                    entity=entity, branch_id=branch_id, requested_by=user,
                    created_by=user, request_date=branch_today(entity.tenant, branch_id),
                    title=(narration or "")[:200],
                    justification=(narration or "")[:255],
                )
                for index, line in enumerate(lines, start=1):
                    PurchaseRequisitionLine.objects.create(
                        requisition=requisition, line_no=index,
                        description=line.description, quantity=line.quantity,
                        estimated_unit_price=line.unit_price,
                    )
                # The engine owns the arithmetic; the FAL only asks for it.
                requisition.recompute_total(save=True)
        except Exception as exc:
            raise _translate_procurement(exc) from exc
        return _available(_proc_document(requisition, overridden=False))

    # ----- approval -------------------------------------------------------- #
    @envelope
    def submit_for_approval(self, doc, *, actor_ref):
        from vs_procurement import approvals

        entity, document, user = self._resolve_for(doc, actor_ref)
        try:
            instance = approvals.submit_for_approval(document, actor_user=user)
        except Exception as exc:
            raise _translate_procurement(exc) from exc
        # A parked submission is a success with a caveat, never an error: the
        # document is PENDING and held, and it releases itself the moment
        # somebody is granted the approving role.
        return _available(_submission_dto(document, instance))

    @envelope
    def approve_without_review(self, doc, *, actor_ref, reason):
        from vs_procurement import approval_override

        entity, document, user = self._resolve_for(doc, actor_ref)
        try:
            row = approval_override.release_parked_document(
                document, actor_user=user, reason=reason,
            )
        except Exception as exc:
            raise _translate_procurement(exc) from exc

        override = ApprovalOverride(
            doc_ref=document.pk,
            doc_type=_doc_type_of(document),
            actor_ref=user.pk,
            reason=row.reason,
            amount=row.amount,
            overridden_at=row.created_at,
            stage_code=row.stage_code or "",
        )
        return _available(_submission_dto(document, _live_instance(document),
                                          override=override))

    def _decide(self, doc, actor_ref, comment, action):
        from vs_workflow.services import actions as wf_actions

        entity, document = self._resolve(doc)
        user = self._actor(entity, actor_ref)
        instance = _live_instance(document)
        if instance is None:
            raise ProcurementStateError(
                f"{doc.doc_type.value} {doc.doc_ref!r} has no approval in flight."
            )
        try:
            wf_actions.record_action(instance.pk, user, action, comment=comment or "")
        except Exception as exc:
            raise _translate_procurement(exc) from exc

        document.refresh_from_db()
        return _available(ApprovalDecision(
            doc_ref=document.pk,
            doc_type=_doc_type_of(document),
            approval_state=ProcApprovalState(document.approval_state),
            decided_by_ref=user.pk,
            decided_at=timezone.now(),
            comment=comment or "",
        ))

    @envelope
    def approve(self, doc, *, approver_ref, comment=""):
        from vs_workflow.constants import WorkflowStageAction

        return self._decide(doc, approver_ref, comment, WorkflowStageAction.APPROVED)

    @envelope
    def decline(self, doc, *, approver_ref, comment=""):
        from vs_workflow.constants import WorkflowStageAction

        return self._decide(doc, approver_ref, comment, WorkflowStageAction.REJECTED)

    # ----- order / receive / bill / pay ------------------------------------ #
    @envelope
    def raise_purchase_order(self, requisition, *, vendor_ref, actor_ref, order_date):
        from vs_procurement import purchasing
        from vs_procurement.models import Vendor

        entity, document, user = self._resolve_for(requisition, actor_ref)
        vendor = Vendor.objects.filter(pk=vendor_ref, entity=entity).first()
        if vendor is None:
            raise CrossTenantError(f"Vendor {vendor_ref!r} is not in this school's books.")
        try:
            with transaction.atomic():
                po = purchasing.create_po_from_requisition(
                    document, vendor=vendor, order_date=order_date, actor_user=user,
                )
        except Exception as exc:
            raise _translate_procurement(exc) from exc
        return _available(_proc_document(po, overridden=False))

    @envelope
    def receive_goods(self, po, *, actor_ref, lines, received_date=None):
        from vs_procurement import purchasing
        from vs_procurement.models import GoodsReceivedNote
        # The receipt-line rules (whole units, per-line PO membership, the
        # accepted+rejected <= PO-remainder cap, the expected-quantity snapshot)
        # live in this one function. Reimplementing them here would fork them, and
        # the fork would drift; a receipt that the API refuses must be refused
        # through the FAL too. That it currently sits in a view module rather than
        # a service is a layering problem in vs_procurement, not a reason for the
        # FAL to keep its own copy.
        from types import SimpleNamespace

        from vs_procurement.views.receiving import _write_grn_lines

        entity, order, user = self._resolve_for(po, actor_ref)
        if not lines:
            raise ProcurementStateError("A goods receipt needs at least one line.")

        try:
            with transaction.atomic():
                grn = GoodsReceivedNote.objects.create(
                    entity=entity, branch_id=order.branch_id, vendor=order.vendor,
                    purchase_order=order, received_by=user, created_by=user,
                    received_date=received_date or branch_today(entity.tenant, order.branch_id),
                )
                # The lines name no ledger account, but the helper reads accounts
                # under a caller's reach, so it is given the acting user as caller.
                _write_grn_lines(SimpleNamespace(user=user), entity, grn, order, [
                    {"po_line": line.po_line_ref,
                     "accepted_qty": line.quantity_received}
                    for line in lines
                ])
                purchasing.post_grn(grn, actor_user=user)
        except Exception as exc:
            raise _translate_procurement(exc) from exc
        return _available(_proc_document(grn, overridden=False))

    @envelope
    def record_supplier_bill(self, po, *, vendor_ref, actor_ref, lines, invoice_date,
                             external_reference=""):
        from vs_procurement import payables
        from vs_procurement.models import (
            PurchaseOrderLine, Vendor, VendorInvoice, VendorInvoiceLine,
        )

        entity, order, user = self._resolve_for(po, actor_ref)
        vendor = Vendor.objects.filter(pk=vendor_ref, entity=entity).first()
        if vendor is None:
            raise CrossTenantError(f"Vendor {vendor_ref!r} is not in this school's books.")
        if not lines:
            raise ProcurementStateError("A supplier bill needs at least one line.")

        try:
            with transaction.atomic():
                bill = VendorInvoice.objects.create(
                    entity=entity, branch_id=order.branch_id, vendor=vendor,
                    purchase_order=order, invoice_date=invoice_date,
                    vendor_reference=external_reference or "", created_by=user,
                )
                for index, line in enumerate(lines, start=1):
                    po_line = None
                    if line.po_line_ref is not None:
                        po_line = PurchaseOrderLine.objects.filter(
                            pk=line.po_line_ref, purchase_order=order,
                        ).first()
                        if po_line is None:
                            raise ProcurementStateError(
                                f"PO line {line.po_line_ref!r} is not on this order."
                            )
                    expense = (
                        (po_line.expense_account if po_line else None)
                        or vendor.default_expense_account
                        or (vendor.category.default_expense_account
                            if vendor.category_id else None)
                    )
                    if expense is None:
                        raise ProcurementStateError(
                            f"Line {index} has no expense account, and neither the "
                            f"purchase order nor the vendor supplies one."
                        )
                    VendorInvoiceLine.objects.create(
                        vendor_invoice=bill, line_no=index, po_line=po_line,
                        description=line.description, expense_account=expense,
                        quantity=line.quantity, unit_price=line.unit_price,
                        tax_code=(po_line.tax_code if po_line else None),
                        cost_center=(po_line.cost_center if po_line else None),
                    )
                # Priced and matched now, posted later. The engine refuses to
                # post a bill that has not been approved, and rightly: posting
                # is what puts the school on the hook for the money, so it
                # cannot happen before somebody says yes. Running the match
                # here anyway means the approver sees the variance verdict
                # while they are deciding, instead of discovering it afterwards.
                payables.price_vendor_invoice(bill)
                payables.match_vendor_invoice(bill, save=True)
                bill.refresh_from_db()
        except Exception as exc:
            raise _translate_procurement(exc) from exc
        return _available(_proc_document(bill, overridden=False))

    def _supplier_bank(self, entity, user, invoice, bank_account_ref):
        """The bank account a payment of *invoice* leaves from.

        A document is paid only from its own branch's bank account, which the
        caller can reach (:func:`vs_finance.views_ops.base._resolve_bank_account`,
        the check every money-out screen makes). Named, it is checked; not
        named, the branch's one active account in reach is used, and a branch
        with none or several is asked to name one. The account is chosen when
        the payment is recorded, not when it posts: a payment recorded against
        the wrong account would otherwise wait for approval and then be
        refused.
        """
        from types import SimpleNamespace

        from vs_finance.models import BankAccount
        from vs_finance.views_ops.base import _resolve_bank_account
        from vs_rbac.scoping import transaction_branch_match_q, transaction_branch_q_for_user

        if bank_account_ref is not None:
            return _resolve_bank_account(
                SimpleNamespace(user=user), entity, bank_account_ref,
                document_branch=invoice.branch_id, noun="supplier payment",
            )
        candidates = list(
            BankAccount.objects
            .filter(transaction_branch_q_for_user(user), entity=entity, is_active=True)
            .filter(transaction_branch_match_q(entity.tenant, invoice.branch_id))
            .select_related("gl_account")[:2]
        )
        if len(candidates) != 1:
            raise ProcurementStateError(
                "Name the bank account this payment leaves from (bank_account_ref): "
                "the bill's branch has "
                + ("no bank account." if not candidates else "more than one.")
            )
        return candidates[0]

    @envelope
    def pay_supplier(self, bill, *, actor_ref, amount, payment_date,
                     bank_account_ref=None):
        from vs_procurement.models import VendorPayment, VendorPaymentAllocation

        entity, invoice, user = self._resolve_for(bill, actor_ref)
        if not amount or amount <= 0:
            raise ProcurementStateError("A supplier payment must be a positive amount.")

        try:
            with transaction.atomic():
                bank = self._supplier_bank(entity, user, invoice, bank_account_ref)
                payment = VendorPayment.objects.create(
                    entity=entity, branch_id=invoice.branch_id, vendor=invoice.vendor,
                    payment_date=payment_date, gross_amount=amount,
                    wht_amount=0, net_amount=amount, created_by=user,
                    payment_account=bank.gl_account,
                )
                # A draft allocation row, which the engine calls an approval
                # instruction: it names the bill this money is for, survives the
                # approval wait, and is replaced by real settlement rows when the
                # payment posts. Naming it here rather than auto-allocating at
                # post time matters, because oldest-first would quietly pay a
                # different supplier bill from the one the school chose.
                VendorPaymentAllocation.objects.create(
                    payment=payment, vendor_invoice=invoice, amount=amount,
                )
        except Exception as exc:
            raise _translate_procurement(exc) from exc
        return _available(_proc_document(payment, overridden=False))

    # ----- posting --------------------------------------------------------- #
    @envelope
    def post_to_ledger(self, doc, *, actor_ref):
        """Post an approved bill or payment to the ledger.

        Recording and posting cannot be one call. ``vs_procurement`` refuses to
        post a vendor invoice or a vendor payment whose ``approval_state`` is
        not APPROVED, and approval is an act by a person that happens in
        between, so a single method could only manage both by skipping the
        approval decision 2 exists to forbid.

        So the chain is record, submit, approve, post, and this is the last step.
        A goods receipt does not appear here because a receipt is not an
        approvable document: ``receive_goods`` posts it directly.

        The acting user is the caller, with the reach the procurement screens give
        them: a bill they cannot reach is refused as another branch's, and a
        payment posts through
        :func:`vs_procurement.views.vendor_payments.post_payment_for_caller`, the
        same checks the procurement post runs (its saved plan, the reach of its
        bills, its branch and its bank account).
        """
        from types import SimpleNamespace

        from vs_procurement import payables
        from vs_procurement.views.base import _document_or_404
        from vs_procurement.views.vendor_payments import post_payment_for_caller

        entity, document = self._resolve(doc)
        user = self._actor(entity, actor_ref)
        caller = SimpleNamespace(user=user)
        try:
            with transaction.atomic():
                if doc.doc_type is ProcDocType.VENDOR_INVOICE:
                    _document_or_404(
                        caller, type(document).objects.filter(entity=entity),
                        document.pk, "No such vendor invoice in this entity.",
                    )
                    payables.post_vendor_invoice(document, actor_user=user)
                elif doc.doc_type is ProcDocType.VENDOR_PAYMENT:
                    post_payment_for_caller(caller, entity, document)
                else:
                    raise ProcurementStateError(
                        f"A {doc.doc_type.value} is not posted through this method."
                    )
        except Exception as exc:
            raise _translate_procurement(exc) from exc
        document.refresh_from_db()
        return _available(_proc_document(document))

    # ----- onboarding seeding ---------------------------------------------- #
    @envelope
    def seed_approval_rules(self, *, entity_ref, threshold):
        from vs_procurement import approvals

        entity = _entity(entity_ref)
        try:
            # The ladder is tenant-scoped because it is a governance policy of the
            # organisation, not of one set of books. Two entities under one tenant
            # share it, and that is correct.
            results = approvals.ensure_tenant_approval_templates(
                entity.tenant, threshold=int(threshold),
            )
        except Exception as exc:
            raise _translate_procurement(exc) from exc
        return _available(any(created for _template, created in results))


# --------------------------------------------------------------------------- #
# ProcurementReadPort
# --------------------------------------------------------------------------- #
class DjangoProcurementReadAdapter(ProcurementReadPort):
    """Read-only procurement for dashboards and reports, scoped to the entity."""

    def _entity_of(self, school_ref):
        return _primary_entity(_school(school_ref))

    def _requisitions(self, entity, branch_ref=None):
        from vs_procurement.models import PurchaseRequisition

        qs = PurchaseRequisition.objects.filter(entity=entity)
        if branch_ref is not None:
            qs = qs.filter(branch_id=branch_ref)
        return qs

    @envelope
    def snapshot(self, school_ref, branch_ref=None):
        from vs_procurement.models import PurchaseOrder

        entity = self._entity_of(school_ref)
        requisitions = self._requisitions(entity, branch_ref)
        orders = PurchaseOrder.objects.filter(entity=entity)
        if branch_ref is not None:
            orders = orders.filter(branch_id=branch_ref)
        return _available(ProcurementSnapshot(
            open_requests=requisitions.exclude(
                status__in=("APPROVED", "CANCELLED"),
            ).count(),
            pending_approvals=requisitions.filter(
                approval_state=ProcApprovalState.PENDING.value,
            ).count(),
            spend=orders.aggregate(t=Sum("total"))["t"] or 0,
        ))

    @envelope
    def rows(self, school_ref, branch_ref=None, filters=(), page=1, page_size=20):
        entity = self._entity_of(school_ref)
        qs = (
            self._requisitions(entity, branch_ref)
            .filter(_filter_q("procurement", filters))
            .order_by("-request_date", "-pk")
        )

        def build(requisition):
            return ProcurementRow(
                request_ref=requisition.pk,
                title=requisition.title or requisition.narration or "",
                status=requisition.status,
                amount=requisition.estimated_total,
                raised_at=datetime.datetime.combine(
                    requisition.request_date, datetime.time.min,
                    tzinfo=datetime.timezone.utc,
                ),
                branch_ref=requisition.branch_id,
            )

        return _available(_page(qs, page, page_size, build))


__all__ = [
    "DjangoEntityResolverAdapter",
    "DjangoFeeTermBridgeAdapter",
    "DjangoStudentCustomerAdapter",
    "DjangoFinanceRbacAdapter",
    "DjangoFinanceReadAdapter",
    "DjangoGuardianLinkAdapter",
    "DenyAllGuardianLinkAdapter",
    "DjangoParentPaymentBridgeAdapter",
    "DjangoProcurementActionAdapter",
    "DjangoProcurementReadAdapter",
]
