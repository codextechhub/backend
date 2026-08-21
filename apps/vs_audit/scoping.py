"""Which audit rows are mine - one answer, read by every audit surface.

The question "which ``AuditEvent`` rows belong to tenant T" is asked by the Event
Explorer, by the event detail route, by the entity trail catalogue and its detail
page, by the security dashboard, by the audit app's own CSV export and by the
Export Centre dataset. Before this module the answer lived in ``vs_audit.views``
and the Export Centre had a second, narrower one of its own - a plain
``filter(tenant=...)`` - so Bright Star's audit officer saw her pre-backfill
password resets on screen and then opened an export of that same view with those
rows missing. Two screens, two answers, one question.

The answer now lives here, in a module both a view and a dataset may import
without either importing the other.

Two layers, deliberately separate:

* :func:`tenant_event_predicate` answers "which rows are tenant T's". It is the
  boundary itself and it never widens.
* :func:`audit_scope_predicate` answers "which rows may *this caller* read",
  which is the boundary plus one policy: the console a PLATFORM caller opens
  reads across tenants by construction, so for them there is no predicate at
  all. That widening is a property of the console, not of the row, which is why
  it sits above the predicate rather than inside it. The Export Centre reads the
  predicate directly and never takes the widening: an export always covers your
  own organisation, which is what
  ``vs_audit.export_datasets._translate_events`` tells the caller in so many
  words.
"""
from __future__ import annotations

from django.db.models import Count, Max, Min, Q


def tenant_event_predicate(tenant) -> Q:
    """The ``Q`` matching every ``AuditEvent`` that belongs to ``tenant``.

    Two arms, because the table holds two shapes of the same fact.

    ``AuditEvent.tenant`` is nullable and only started being populated in
    d1ceccb (2026-08-19), which deliberately did not backfill. A closed
    historical set of rows therefore sits at ``tenant = NULL`` while still
    recording the owning tenant's pk in ``metadata['tenant_id']``. Matching on
    the column alone hides a school's own history from its own auditor; widening
    to every null row hands her somebody else's. Matching the id recorded at the
    time returns exactly the rows that are hers, recovered rather than inferred.

    Only three writers ever recorded that id: ``vs_user.services.audit`` (the
    whole IDENTITY stream, since 661a73a), ``vs_rbac.signals`` and
    ``vs_rbac.services``. Finance, procurement, payments, imports and tickets
    never did, so their pre-d1ceccb rows carry no id and stay platform-only, as
    do post-d1ceccb rows written under a PLATFORM assertion (see
    :func:`vs_audit.services.resolve_event_tenant`). Both are the safe direction
    to be wrong in: a tenant sees less of its own history than it might, never
    another tenant's.

    ``tenant`` of ``None`` fails closed rather than matching the null rows, which
    would be the whole platform's history handed to a caller who is inside no
    tenant at all.
    """
    if tenant is None:
        return Q(pk__in=[])
    return (
        Q(tenant=tenant)
        | Q(tenant__isnull=True, metadata__tenant_id=str(tenant.pk))
    )


def audit_scope_predicate(request):
    """The ``Q`` confining an ``AuditEvent`` read to the caller's own tenant.

    Returns ``None`` for a platform-tenant caller, who reads across tenants by
    construction: that is what the console exists for, and it is the one
    audience the key's ``platform.`` namespace actually describes.

    Everyone else is confined by :func:`tenant_event_predicate`.
    ``platform.audit.view`` is deliberately ``PermissionScope.TENANT`` -
    ``vs_rbac.models.PermissionScope`` and
    ``seed_platform_permissions.TENANT_HOLDABLE_KEYS`` both say it belongs to
    "audit officers working inside a tenant" - so holding the key says nothing
    about whose rows the holder may read. Only the queryset can say that, and
    until 1da5c2a it did not: Bright Star's audit officer opening the Event
    Explorer was handed Greenfield's purchase-order approvals, Greenfield's
    staff names and Greenfield's password resets. The ``tenant_slug`` filter is
    a narrowing convenience, not a boundary - it narrows if she asks and does
    nothing if she does not.

    The gate is the caller's *home* tenant kind, which no grant and no
    ``?tenant=`` can change. Under impersonation ``request.user`` is the
    effective (target) user, so a Codex staffer proxied as a Bright Star account
    is confined to Bright Star - which is what being proxied means.
    """
    from vs_tenants.models import Tenant

    user = getattr(request, "user", None)
    home = getattr(user, "tenant", None)
    if getattr(home, "kind", None) == Tenant.Kind.PLATFORM:
        return None

    # A caller who is inside no tenant cannot be inside this one. Every
    # authenticated request carries a tenant today, so ``None`` here is
    # unreachable; tenant_event_predicate fails closed so that it stays
    # unreachable if that ever changes.
    return tenant_event_predicate(getattr(request, "tenant", None) or home)


def scope_events_to_caller(queryset, request):
    """Confine an ``AuditEvent`` queryset with :func:`audit_scope_predicate`."""
    predicate = audit_scope_predicate(request)
    return queryset if predicate is None else queryset.filter(predicate)


def visible_trail_counters(trails, request):
    """Recount ``EntityAuditTrail`` rollups over only the events this caller reads.

    ``EntityAuditTrail`` is keyed on ``(entity_type, entity_id)`` with no tenant
    column, so ``event_count``, ``first_event_at`` and ``last_event_at`` are a
    stored rollup over every event on that entity from every tenant. 1da5c2a
    bounded *which* trails a caller may list; it left the three numbers on each
    listed row global. Handing a school a count it cannot reconcile with the
    events underneath it is a small disclosure and a large confusion: the trail
    says 40 and the page below lists 12.

    Returns ``None`` for a platform caller, who keeps the stored rollup - the
    global figure is the true one and the platform console is what it is for.
    Otherwise returns ``{(entity_type, entity_id): {...}}`` for the trails
    handed in, and a missing key means the caller can read no event on that
    entity at all.

    **Cost: exactly one query for the whole page, or none for a platform
    caller.** This feeds a list endpoint, so the obvious shape - ask per row at
    serialization - is an N+1 by construction. The pairs are collected first and
    aggregated in one grouped query instead, the same bulk-then-attach shape
    ``AuditEventListView.list`` already uses to resolve entity users.
    """
    from .models import AuditEvent

    predicate = audit_scope_predicate(request)
    if predicate is None:
        return None

    pairs = {(t.entity_type, t.entity_id) for t in trails}
    if not pairs:
        return {}

    match = Q()
    for entity_type, entity_id in pairs:
        match |= Q(entity_type=entity_type, entity_id=entity_id)

    rows = (
        AuditEvent.objects.filter(predicate)
        .filter(match)
        .values("entity_type", "entity_id")
        .annotate(
            visible_count=Count("id"),
            visible_first=Min("event_at"),
            visible_last=Max("event_at"),
        )
    )
    return {
        (row["entity_type"], row["entity_id"]): {
            "event_count": row["visible_count"],
            "first_event_at": row["visible_first"],
            "last_event_at": row["visible_last"],
        }
        for row in rows
    }
