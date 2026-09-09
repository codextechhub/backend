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
import logging
from datetime import datetime, time

from django.db import transaction
from django.utils import timezone

from vs_config.models import Capability, CapabilityEntitlement
from vs_config.services.capabilities import set_entitlement

logger = logging.getLogger("vs_schools")


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


@transaction.atomic
def change_plan(*, school, plan, actor, reason="", expires_at=None):
    """Move a school onto another plan, and re-grant in the same transaction.

    The two halves are inseparable on purpose. Changing ``package_plan`` on its
    own leaves the school holding the old plan's depths: the row says Standard
    and the product behaves like Basic, which is the state an operator would
    read as the gate misbehaving. There is no path here that writes one without
    the other.

    An uplift given to this school is deliberately left alone. It lives in its
    own table precisely so a tier change underneath it does not disturb it, and
    it stops mattering on its own once the tier passes it.
    """
    setup = school.package_setup
    previous = setup.package_plan
    setup.package_plan = plan
    if expires_at is not None:
        setup.subscription_expires_at = expires_at
    setup.save()

    rows = apply_plan_entitlements(
        school=school,
        plan=plan,
        expires_at=setup.subscription_expires_at,
        actor=actor,
        reason=reason or f"Moved from {previous.name} to {plan.name}.",
    )
    _top_up_onboarding(school, actor)
    return setup, previous, rows


def _top_up_onboarding(school, actor):
    """Give a school still in onboarding any step its new plan has opened.

    Some checklist steps exist only for schools whose plan can perform them, so
    a school that moves up mid-onboarding has earned a card it was not given at
    creation. Provisioning adds and never removes, so this cannot cost a school
    a step it has already completed, and a school that moves down keeps what it
    has done.

    Skipped once a school is live: the checklist is a record of how it got
    there, and adding a card to it afterwards would reopen a finished thing.
    Best effort, like provisioning itself - a plan change must not fail because
    of a checklist.
    """
    from schools.vs_onboarding.constants import ReadinessState
    from schools.vs_onboarding.models import OnboardingProgress
    from schools.vs_onboarding.services.provisioning import provision_onboarding

    progress = OnboardingProgress.all_objects.filter(tenant=school.tenant).first()
    if progress is None or progress.readiness_state == ReadinessState.LIVE:
        return
    try:
        provision_onboarding(school.tenant, actor=actor)
    except Exception:  # noqa: BLE001 - a checklist must not cost a plan change
        logger.exception(
            "change_plan: could not top up onboarding for %s", school.slug,
        )


def plan_overview(school):
    """What this school is on, module by module, and where each depth came from.

    The read the console opens with. A support call starts "what is this school
    on?", and until now nothing could answer it: the effective-capability read
    returns on or off per capability and says nothing about depth, so an
    operator could see that a school was refused something without being able
    to see why.

    Every module answers with three facts rather than one. The depth it
    reaches, because that is what was sold. Where that depth came from - the
    plan's own default, an exception the plan makes for this module, or an
    uplift given to this school and ending on a date - because an operator
    asked to change something needs to know which of the three to change. And
    the bands themselves with a reached flag, because "Plus" means nothing to
    the person on the phone and "fee structures, bank reconciliation, budgets"
    means everything.
    """
    from vs_config.models import Capability, CapabilityDepthGrant, CapabilityEntitlement
    from vs_config.services.depth import UNLIMITED, depth_allows, depth_label, resolved_depth

    setup = getattr(school, "package_setup", None)
    tenant = school.tenant

    entitlements = {
        row.capability_id: row
        for row in CapabilityEntitlement.all_objects.filter(tenant=tenant)
    }
    uplifts = {
        row.capability_id: row
        for row in CapabilityDepthGrant.all_objects.filter(tenant=tenant)
    }
    bands = {}
    for band in Capability.objects.filter(
        parent__isnull=False, is_active=True
    ).order_by("depth", "key"):
        bands.setdefault(band.parent_id, []).append(band)

    modules = []
    for module in sellable_modules().order_by("label"):
        grant = entitlements.get(module.pk)
        uplift = uplifts.get(module.pk)
        plan_depth = setup.package_plan.depth_for(module.key) if setup else None
        reached = resolved_depth(module, tenant)

        # Which of the three decided it. Checked in the order resolution reads
        # them, so the answer here cannot disagree with the gate.
        if uplift is not None and _is_live(uplift) and reached != plan_depth:
            source = "uplift"
        elif setup and setup.package_plan.module_depths.filter(
            capability_key=module.key
        ).exists():
            source = "plan exception"
        elif setup:
            source = "plan default"
        else:
            source = "no plan"

        modules.append({
            "key": module.key,
            "label": module.label,
            "granted": bool(grant and grant.state == CapabilityEntitlement.State.GRANTED),
            "depth": reached,
            "depth_label": depth_label(reached),
            "source": source,
            "plan_depth": plan_depth,
            "plan_depth_label": depth_label(plan_depth) if setup else None,
            "expires_at": grant.ends_at if grant else None,
            "uplift": None if uplift is None else {
                "depth": uplift.depth,
                "depth_label": depth_label(uplift.depth),
                "starts_at": uplift.starts_at,
                "ends_at": uplift.ends_at,
                "live": _is_live(uplift),
                "reason": uplift.reason,
            },
            "bands": [
                {
                    "key": band.key,
                    "label": band.label,
                    "depth": band.depth,
                    "depth_label": depth_label(band.depth),
                    "reached": depth_allows(band.depth, reached),
                }
                for band in bands.get(module.pk, [])
            ],
        })

    return {
        "school": school.slug,
        "plan": None if not setup else {
            "code": setup.package_plan.code,
            "name": setup.package_plan.name,
            "default_depth": setup.package_plan.default_depth,
            "default_depth_label": (
                setup.package_plan.get_default_depth_display() or "Unlimited"
            ),
            "subscription_expires_at": setup.subscription_expires_at,
            "is_active": setup.is_active,
        },
        # A school with no grants at all is unprovisioned rather than
        # unentitled, and the gate never refuses it. An operator looking at a
        # school that reaches everything needs to know which of the two it is.
        "provisioned": bool(entitlements),
        "modules": modules,
    }


def _is_live(row, now=None):
    from django.utils import timezone

    now = now or timezone.now()
    if row.starts_at and row.starts_at > now:
        return False
    if row.ends_at and row.ends_at <= now:
        return False
    return True
