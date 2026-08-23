"""Provisioning a tenant's books, and what else gets set up when they are.

:func:`provision_books` is the one way a usable set of books comes into
existence. It used to live inside ``LedgerEntityCreateSerializer.create``, which
meant an HTTP POST was the *only* way to get one: nothing else in the platform
could provision books without either faking a request or duplicating the
sequence and drifting out of step with it. School creation needs books at the
moment a school is created, so the sequence moved down here where any caller can
reach it, and the serializer became one of those callers.

The rest of this module is the provisioner registry described below.

Creating a :class:`~vs_finance.models.LedgerEntity` already provisions a fully usable
set of books in one call: the currencies, a starter chart of accounts, and open fiscal
periods. The intent is that no operator has to remember a follow-up command.

Approval ladders were the exception, and it mattered. A payout batch or a procurement
document is only gated when a workflow template exists for its scope, so a tenant whose
ladders were never published had **no** maker-checker at all on its highest-risk
cash-out path. The seeds existed but only a management command and one endpoint called
them, so turning the gate on was an operator step that could simply be missed.

This registry closes that. A dependent app registers a provisioner from its
``AppConfig.ready`` and it runs inside the same transaction that creates the entity, so
the books and the controls over them arrive together or not at all. Finance never
imports procurement or payments; the dependency keeps running one way, exactly as it
does for the period-close checks and the workflow handlers.

**Failures are not swallowed.** A provisioner that raises rolls the entity creation
back. That is deliberate: an entity that exists without its approval ladders is the
open door this module was written to close, and a loud, retryable failure at
onboarding is much cheaper than a silent one discovered at the first payout run.
"""
from __future__ import annotations

#: Callables run against a newly created entity, in registration order.
_PROVISIONERS: list = []


def register_entity_provisioner(fn):
    """Register a callable run as ``fn(entity)`` when a ledger entity is created.

    Registration is idempotent, so a module imported twice does not provision twice.
    Provisioners must themselves be idempotent and non-destructive: several entities
    can share one tenant, so the second entity in a tenant will call them again and
    must find the earlier work and leave it alone.
    """
    if fn not in _PROVISIONERS:
        _PROVISIONERS.append(fn)
    return fn


def registered_entity_provisioners() -> list:
    """The registered provisioners, for tests and diagnostics."""
    return list(_PROVISIONERS)


def provision_entity(entity) -> None:
    """Run every registered provisioner for a newly created entity."""
    for fn in _PROVISIONERS:
        fn(entity)


def primary_entity_for(tenant):
    """The tenant's primary (TENANT-kind, active) set of books, or ``None``.

    This is the lookup the second-entity guard consults, and the same question
    the finance abstraction layer will ask when it resolves a school's books.
    Ordered by id so the answer is stable if a tenant somehow holds more than
    one (rows created before this guard existed, or by a deliberate operator).
    """
    from vs_tenants.models import Tenant

    from .models import LedgerEntity

    if tenant is None:
        # No tenant means LedgerEntity.save() falls back to the platform tenant,
        # which is exempt below anyway.
        return None
    if getattr(tenant, "kind", None) == Tenant.Kind.PLATFORM:
        # Codex's own tenant legitimately keeps several sets of books (its
        # platform books, product books, test books) and is never resolved
        # through the primary-entity lookup, so the guard does not apply to it.
        return None
    return (
        LedgerEntity.objects
        .filter(tenant=tenant, kind=LedgerEntity.Kind.TENANT, is_active=True)
        .order_by("id")
        .first()
    )


def provision_books(
    *,
    tenant=None,
    name,
    code,
    base_currency=None,
    kind=None,
    number_code="",
    fiscal_year=None,
    fiscal_start_month=1,
    fiscal_period_frequency="MONTHLY",
    fiscal_start_day=1,
    reuse_existing=False,
):
    """Create a fully usable set of books for ``tenant`` and return the entity.

    One call produces the entity, the default currencies, a starter chart of
    accounts, open fiscal periods, and whatever dependent apps have registered
    (the procurement and payout approval ladders). The fiscal anchors let a
    school open e.g. a Sept-Aug year on a chosen day.

    Everything happens in one transaction, and provisioner failures are **not**
    swallowed: books without their approval ladders are the open door this
    module was written to close, so a failure takes the entity with it rather
    than leaving an ungated tenant behind.

    Args:
        tenant: The owning ``vs_tenants.Tenant``. ``None`` lets
            ``LedgerEntity.save()`` fall back to the Codex platform tenant,
            which is the behaviour the API had before this function existed.
        name: Human-friendly name of the entity keeping the books.
        code: Short uppercase code. Callers are responsible for normalising and
            for uniqueness; the column is globally unique and 16 characters.
        base_currency: A ``Currency`` instance or its 3-letter code. Left out,
            the model default (NGN) applies.
        kind: A ``LedgerEntity.Kind``; defaults to ``TENANT``.
        number_code: Leave blank and the model auto-derives a unique one.
        reuse_existing: When the tenant already has a primary set of books,
            return it untouched instead of refusing. School creation and the
            backfill command want this; the API deliberately does not, because
            an operator asking for a *new* entity should be told they already
            have one rather than silently handed the old one.

    Raises:
        PrimaryEntityExistsError: A second ``TENANT``-kind entity was requested
            for a tenant that already has an active one, and ``reuse_existing``
            is False. See that exception for why this is a service rule and not
            a database constraint.
    """
    from django.db import transaction
    from django.utils import timezone

    from .exceptions import PrimaryEntityExistsError
    from .models import LedgerEntity
    from .seed import seed_chart_of_accounts, seed_currencies, seed_fiscal_year

    if kind is None:
        kind = LedgerEntity.Kind.TENANT

    # The guard sits here, at the choke point every caller shares, rather than
    # on the endpoint: a rule enforced in one view is a rule the next caller
    # gets to skip.
    if kind == LedgerEntity.Kind.TENANT:
        existing = primary_entity_for(tenant)
        if existing is not None:
            if reuse_existing:
                return existing
            raise PrimaryEntityExistsError(entity_code=existing.code)

    fields = {"name": name, "code": code, "kind": kind, "number_code": number_code or ""}
    if tenant is not None:
        fields["tenant"] = tenant
    if base_currency is not None:
        # Currency's primary key is the 3-letter code, so a caller may pass
        # either the row or the code it is keyed by.
        if isinstance(base_currency, str):
            fields["base_currency_id"] = base_currency
        else:
            fields["base_currency"] = base_currency

    with transaction.atomic():
        entity = LedgerEntity.objects.create(
            is_active=True, activated_at=timezone.now(), **fields,
        )
        seed_currencies()
        seed_chart_of_accounts(entity)
        seed_fiscal_year(
            entity,
            year=fiscal_year,
            start_month=fiscal_start_month,
            fiscal_period_frequency=fiscal_period_frequency,
            fiscal_start_day=fiscal_start_day,
        )
        # Inside the transaction on purpose: books without their approval
        # ladders are the open door this closes, so a failure here takes the
        # entity with it rather than leaving an ungated tenant behind.
        provision_entity(entity)
    return entity
