"""How far into a module a tenant reaches, and whether one band is inside it.

A module is sold whole and used in slices. Every tenant entitled to Finance
holds all of Finance as far as the catalogue is concerned; how much of it
answers is a second question, asked here.

The chain is short on purpose, because a salesperson has to be able to say it
out loud:

    1. a live CapabilityDepthGrant for this tenant and module - the deal
    2. the depth on the tenant's own CapabilityEntitlement - the tier
    3. the depth on a platform-wide entitlement - the house default
    4. no limit

Step 4 is what a grant written before depth existed means, and it is why a
missing depth can never be read as Core: doing so would take Advanced work
away from every school on the platform the moment depth shipped, without a
single row changing.

The deal only ever adds. Resolution takes the deeper of the deal and the
tier, so a school that upgrades past an old uplift is not quietly demoted by
a row somebody forgot to delete.
"""
from django.db.models import Q
from django.utils import timezone

from ..models import Capability, CapabilityDepthGrant, CapabilityEntitlement


#: Returned by :func:`resolved_depth` for a grant that names no depth. Deeper
#: than any real band, so every ``band.depth <= UNLIMITED`` comparison passes
#: without the callers having to special-case None.
UNLIMITED = None


def _is_live(row, now):
    if row.starts_at and row.starts_at > now:
        return False
    if row.ends_at and row.ends_at <= now:
        return False
    return True


def resolved_depth(capability, tenant, *, now=None):
    """The deepest band of ``capability`` this tenant may reach.

    Returns one of :class:`Capability.Depth`'s values, or ``UNLIMITED`` when
    nothing limits the tenant. Accepts a module or a band and answers for the
    module either way, so callers never have to walk the parent themselves.

    Says nothing about whether the module is entitled at all. A tenant with
    no grant resolves to ``UNLIMITED`` here and is still refused by
    ``effective_capability``, which asks the entitlement question separately.
    Keeping the two apart is what lets the refusal say which wall was hit.
    """
    module = capability.parent if capability.parent_id else capability
    now = now or timezone.now()

    tier = UNLIMITED
    scope = Q(tenant__isnull=True)
    if tenant is not None:
        scope |= Q(tenant=tenant)
    rows = list(
        CapabilityEntitlement.all_objects.filter(capability=module).filter(scope)
    )
    # A tenant's own entitlement answers for it; the platform row is the
    # fallback, matching how the entitlement state itself resolves.
    winner = next((row for row in rows if row.tenant_id), None)
    if winner is None:
        winner = next((row for row in rows if not row.tenant_id), None)
    if winner is not None:
        tier = winner.depth

    if tenant is None:
        return tier

    deal = CapabilityDepthGrant.all_objects.filter(
        capability=module, tenant=tenant,
    ).first()
    if deal is None or not _is_live(deal, now):
        return tier
    if tier is UNLIMITED:
        return UNLIMITED
    return max(tier, deal.depth)


def depth_allows(required, held):
    """Whether a grant reaching ``held`` covers something needing ``required``.

    ``held`` of ``UNLIMITED`` covers everything, and ``required`` of
    ``UNLIMITED`` is something no band asked for, so it needs nothing.
    """
    if required is UNLIMITED:
        return True
    if held is UNLIMITED:
        return True
    return required <= held


def depth_label(value):
    """The word a person reads, for a depth or for no limit at all."""
    if value is UNLIMITED:
        return "Unlimited"
    return Capability.Depth(value).label
