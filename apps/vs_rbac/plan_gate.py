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

    The flag governs the refusal, not the reading. :func:`plan_reader`, which
    the role builder shows its greyed-out boxes from, answers what the school
    bought whether or not refusals are being issued - see its own docstring
    for why the two must not be tied together.

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
from vs_config.services.capabilities import (
    BulkCapabilityEvaluator,
    effective_capability,
)
from vs_config.services.depth import resolved_depth

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


def capability_for_row(permission, cache=None):
    """The capability governing one already-loaded permission row.

    Reads the capability recorded on the row itself, falling back to
    :mod:`vs_rbac.capability_map` for keys nobody has classified yet. The
    fallback is what lets the mapping move from code to data one module at a
    time instead of in one unreviewable change.

    Callers that hold rows in bulk should select ``capability``,
    ``capability__parent`` and ``resource``, and pass a ``cache`` dict they own
    for the duration of the request. The fallback is the only path that
    queries, and until every permission carries its capability it is the path a
    third of them still take.
    """
    from .capability_map import capability_for

    if permission.capability_id and permission.capability.is_active:
        return permission.capability
    # The map is keyed on the resource's slug. ``resource_id`` is a surrogate
    # integer, so it has to be read through the row.
    resource = permission.resource.name if permission.resource_id else ""
    mapped = capability_for(permission.module_id, resource)
    if not mapped:
        return None
    if cache is None:
        return Capability.objects.filter(key=mapped, is_active=True).first()
    if mapped not in cache:
        cache[mapped] = Capability.objects.filter(
            key=mapped, is_active=True,
        ).first()
    return cache[mapped]


def capability_for_permission(permission_key):
    """The capability governing one permission key, or None when it is core."""
    from .models import Permission

    row = (
        Permission.objects.filter(key=permission_key, is_active=True)
        .select_related("capability", "capability__parent", "resource")
        .first()
    )
    if row is None:
        return None
    return capability_for_row(row)


def plan_reader(tenant):
    """A function answering, of a permission row, what this gate would do to it.

    The role builder has to show the same verdict this gate enforces, for every
    key on the platform at once. Asking :func:`plan_refusal` once per key would
    re-ask whether the school is provisioned, and re-evaluate a capability,
    three hundred times over; this asks once and memoises per capability,
    because three hundred keys share a few dozen bands.

    It returns ``(capability, allowed, reason)``. ``reason`` is empty whenever
    ``allowed``, so a caller can render it without a second condition.

    ``platform.entitlements.enforce`` is deliberately not read here. That flag
    decides whether a refusal is issued at the door; it does not decide what a
    school bought, and the builder answers the second question. So this stays
    the stricter of the two whenever the flag is off, which is the safe
    direction: the builder may hide something the gate would currently let
    through, and never the reverse. It also keeps the builder agreeing with
    the navigation, which reads entitlements directly and has never consulted
    the flag - a school shown no Procurement menu should not be offered
    Procurement permissions the same afternoon.
    """
    enforcing = tenant is not None and tenant_is_provisioned(tenant)
    verdicts: dict[int, tuple[bool, str]] = {}
    mapped: dict[str, object] = {}
    evaluator = None

    def read(permission):
        nonlocal evaluator
        capability = capability_for_row(permission, cache=mapped)
        if capability is None or not enforcing:
            return capability, True, ""
        if capability.pk not in verdicts:
            if evaluator is None:
                # Built on first need and not before: a school whose keys are
                # all unclassified never pays for it. It loads the catalogue,
                # its dependencies, entitlements, overrides and uplifts in a
                # fixed handful of queries, which is the difference between a
                # picker costing five queries and one costing a hundred.
                evaluator = BulkCapabilityEvaluator(
                    list(
                        Capability.objects.all().prefetch_related(
                            "dependency_links",
                        )
                    ),
                    tenant=tenant,
                )
            allowed = bool(evaluator.evaluate(capability.pk))
            held = (
                evaluator.resolved_depth(capability.parent_id)
                if capability.parent_id else None
            )
            verdicts[capability.pk] = (
                allowed, "" if allowed else _describe(capability, held),
            )
        allowed, reason = verdicts[capability.pk]
        return capability, allowed, reason

    return read


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
            held = (
                resolved_depth(capability.parent, tenant)
                if capability.parent_id else None
            )
            first_refusal = _describe(capability, held)
    return first_refusal


def _describe(capability, held):
    """The sentence a school reads, for one closed capability.

    It never names a depth. Core, Plus and Advanced are how CodeX prices the
    product, and a bursar refused mid-task has no use for the vocabulary: told
    "this school reaches Core" she learns a word from our price list and not
    what to do next. Depth still travels in the refusal, as the structured
    ``band`` and ``depth_label`` the console and the role builder read; it just
    does not travel in the prose.

    ``held`` is kept in the signature because a caller has already resolved it
    and a future message may want it. It is deliberately unused in the text.

    Naming the thing: a band seeded from a module is labelled "Finance: Plus",
    so saying its label would say the tier after all - those answer with the
    module's name instead. A band with a name of its own, like "Bulk Data
    Import", already reads as a feature and answers with that.
    """
    module = capability.parent if capability.parent_id else capability
    name = capability.label
    if capability.parent_id and name.startswith(module.label):
        name = module.label
    return (
        f"{name} is not part of this school's plan. "
        f"Contact CodeX to add it."
    )
