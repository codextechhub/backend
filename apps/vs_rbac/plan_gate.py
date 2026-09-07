"""The second gate: not what your role allows, but what your school bought.

Every route on the platform already asks one question through
:class:`HasRBACPermission` - does this user hold this permission key. This asks
the other one, and keeps it separate on purpose.

Why the two refusals must not share a message
    Corona's bursar opens Payroll and is refused. Told "you do not have
    permission", she calls her school admin, who opens her role, finds Payroll
    ticked, and calls our support line; we spend an hour proving nothing is
    broken. Told "Payroll is part of Advanced, and Corona is on Standard", she
    walks to the proprietor and the proprietor calls our sales line. Same
    refusal, opposite direction of travel.

    So a plan refusal carries its own code, the module it concerns, and the
    depth the school would need. A role refusal is left exactly as it was.

Off unless switched on
    ``platform.entitlements.enforce`` is False until the permission-to-band
    map is complete, because a half-mapped catalogue refuses real work for no
    commercial reason. It is settable at platform scope only, and that is a
    security property rather than an oversight: a school-scoped switch would
    let a school with ``config.value.update`` turn off its own plan gate.
    While it is off this costs one cached config read and asks the database
    nothing, which is why the check is ordered flag-first.

Two ways a school is never locked out
    A permission with no capability behind it is available to every school.
    That is the safe direction and it matches the convention the capability
    map already used: a module nobody has classified is core until somebody
    decides otherwise. Defaulting the other way would hide working routes from
    paying schools the day a new module ships.

    And a school with no package grants at all is unprovisioned, not
    unentitled. Those are different facts and only one of them should close a
    door. Schools created before grants were written reliably have no rows, so
    gating them would take away a product they are paying for. The rule
    retires itself: the moment a school's plan is applied it has grants, and
    the ordinary answer takes over with no change here.

What this does not cover
    Only ``rbac_permission`` is gated. A view gated solely by
    ``rbac_group_permission`` or by :class:`HasAnyModuleAccess`'s
    ``rbac_modules`` passes untouched, because neither names a permission key
    this can look a capability up from. Both are rare - one shared reference
    endpoint uses ``rbac_modules`` and nothing uses group permissions - and
    both read data that is meaningless without a module the caller already
    reached through a gated route. Extend this before either becomes a way to
    reach something sold by depth.
"""
from vs_config.conf import get_config
from vs_config.models import Capability, CapabilityEntitlement
from vs_config.services.capabilities import effective_capability
from vs_config.services.depth import depth_label, resolved_depth

#: The setting that turns this gate on, platform scope only. See the module
#: docstring for why it is not settable per school.
ENFORCEMENT_KEY = "platform.entitlements.enforce"


def enforcement_enabled():
    return bool(get_config(ENFORCEMENT_KEY, default=False))


def tenant_is_provisioned(tenant):
    """Whether this school has been given its plan's grants at all."""
    return CapabilityEntitlement.all_objects.filter(
        tenant=tenant, source=CapabilityEntitlement.Source.PACKAGE,
    ).exists()


def capability_for_permission(permission_key):
    """The capability governing one permission key, or None when it is core.

    Reads the capability recorded on the permission row itself, falling back
    to :mod:`vs_rbac.capability_map` for keys nobody has classified yet. The
    fallback is what lets the mapping move from code to data one module at a
    time instead of in one unreviewable change.
    """
    from .capability_map import capability_for
    from .models import Permission

    row = (
        Permission.objects.filter(key=permission_key, is_active=True)
        .select_related("capability", "capability__parent")
        .first()
    )
    if row is None:
        return None
    if row.capability_id and row.capability.is_active:
        return row.capability
    mapped = capability_for(row.module_id, row.resource_id or "")
    if not mapped:
        return None
    return Capability.objects.filter(key=mapped, is_active=True).first()


def plan_refusal(permission_keys, tenant):
    """Why the plan refuses every one of these keys, or "" when one is allowed.

    Permission keys on a view are any-of, so the caller gets through if the
    plan covers a single one of them. The message describes the first key
    that was refused, which is the one the screen was built around.
    """
    if tenant is None or not enforcement_enabled():
        return ""
    if not tenant_is_provisioned(tenant):
        return ""

    first_refusal = ""
    for key in permission_keys:
        capability = capability_for_permission(key)
        if capability is None:
            return ""
        # The capability object is already in hand, with its parent selected,
        # so it is evaluated directly rather than looked up again by key.
        if effective_capability(capability, tenant=tenant):
            return ""
        if not first_refusal:
            first_refusal = _describe(capability, tenant)
    return first_refusal


def _describe(capability, tenant):
    """The sentence a proprietor can act on, for one closed capability."""
    module = capability.parent if capability.parent_id else capability
    if capability.parent_id:
        held = resolved_depth(module, tenant)
        return (
            f"{capability.label} is part of {capability.get_depth_display()} "
            f"depth in {module.label}. This school reaches "
            f"{depth_label(held)}. Upgrading the plan opens it."
        )
    return (
        f"{module.label} is not part of this school's plan. "
        f"Adding it to the plan opens it."
    )
