"""Turning the plan a school pays for into the grants that make it work.

One school, one plan, one depth per module. This is the only place that
translation happens, so the answer cannot differ between the school that was
created with a package and the school that changed plan afterwards.

What changed, and why
    Onboarding used to hand-pick modules: the wizard sent a list, and a school
    got exactly what was ticked. That made the plan and the product two
    unrelated facts about the same school, and a school on Basic could be
    ticked into everything. It also meant a school never saw the modules
    nobody thought to sell it, so every upsell had to start from us.

    Now the plan decides. Every school is granted every module, and the plan
    says how far into each one it reaches. Nothing is hidden; some of it is
    shallow. A school that wants more meets a wall it can see and asks.

Expiry
    The grant carries the subscription's own end date. It used to be written
    with no end at all while the expiry sat unread on the package setup two
    lines above, which is why a school that stopped paying kept everything
    switched on. The capability evaluator has always honoured ``ends_at``; it
    was simply never given one.
"""
from datetime import datetime, time

from django.utils import timezone

from vs_config.models import Capability, CapabilityEntitlement
from vs_config.services.capabilities import set_entitlement


def subscription_ends_at(expires_at):
    """The moment a subscription dated ``expires_at`` stops covering anything.

    ``ends_at`` is exclusive and the stored expiry is a date, so a school paid
    up to the 31st keeps the 31st: the grant ends at midnight opening the 1st,
    not at midnight opening the 31st. An expiry that is already a datetime is
    handed back untouched, since something more precise than a date has
    already been decided elsewhere.
    """
    if expires_at is None:
        return None
    if isinstance(expires_at, datetime):
        return expires_at
    naive = datetime.combine(expires_at, time.min) + timezone.timedelta(days=1)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def sellable_modules():
    """Every module a plan grants: top-level, active, and entitlement-gated.

    Bands are excluded because nobody buys one. A band becomes reachable when
    the depth on its module's grant reaches it, so granting bands separately
    would create a second answer to a question the depth already settles.
    """
    return Capability.objects.filter(
        parent__isnull=True, is_active=True, requires_entitlement=True,
    )


def apply_plan_entitlements(*, school, plan, expires_at, actor, reason=""):
    """Grant this school every module, each at the depth its plan reaches.

    Idempotent: it writes the same rows every time, so running it after a plan
    change moves the school without leaving the old plan's depths behind. It
    does not touch :class:`vs_config.models.CapabilityDepthGrant`, which is
    where a deal lives; an uplift given on top of Basic survives the school
    moving to Standard and stops mattering once the tier passes it.

    Returns the entitlement rows written, in catalogue order.
    """
    ends_at = subscription_ends_at(expires_at)
    reason = reason or f"Package plan {plan.name} for {school.name}"
    rows = []
    for module in sellable_modules():
        rows.append(set_entitlement(
            capability=module,
            tenant=school.tenant,
            state=CapabilityEntitlement.State.GRANTED,
            source=CapabilityEntitlement.Source.PACKAGE,
            actor=actor,
            depth=plan.depth_for(module.key),
            ends_at=ends_at,
            reason=reason,
        ))
    return rows
