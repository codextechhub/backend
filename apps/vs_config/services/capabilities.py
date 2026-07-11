from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..exceptions import CapabilityDependencyError, CapabilityNotEntitled
from ..models import (
    Capability,
    CapabilityEntitlement,
    CapabilityOverride,
)
from .audit import record_configuration_event
from .scopes import normalize_scope


# Determine whether a capability is entitled at the most specific available school scope.
def _active_entitlement(capability, school):
    if not capability.requires_entitlement:
        return True
    now = timezone.now()
    scope = Q(school__isnull=True)
    if school is not None:
        scope |= Q(school=school)
    rows = list(CapabilityEntitlement.all_objects.filter(capability=capability).filter(scope))
    # School-specific grants override the platform grant for entitlement checks.
    specific = next((row for row in rows if row.school_id), None)
    entitlement = specific or next((row for row in rows if not row.school_id), None)
    if entitlement is None or entitlement.state != entitlement.State.GRANTED:
        return False
    # Future grants and expired grants are treated as inactive even if the row exists.
    if entitlement.starts_at and entitlement.starts_at > now:
        return False
    if entitlement.ends_at and entitlement.ends_at <= now:
        return False
    return True


# Resolve the final feature gate after entitlement, dependencies, and scoped overrides.
def effective_capability(capability, *, school=None, branch=None, _seen=None):
    school, branch = normalize_scope(school=school, branch=branch)
    if not capability.is_active or not _active_entitlement(capability, school):
        return False
    # Track dependency traversal so cyclic capability graphs fail loudly.
    seen = set(_seen or ())
    if capability.pk in seen:
        raise CapabilityDependencyError(f"Dependency cycle detected at '{capability.key}'.")
    seen.add(capability.pk)
    for link in capability.dependency_links.select_related("requires"):
        # A capability cannot be effective unless every required capability is effective too.
        if not effective_capability(link.requires, school=school, branch=branch, _seen=seen):
            return False

    # More specific overrides win: branch, then school, then platform.
    keys = []
    if branch is not None:
        keys.append(f"branch:{branch.pk}")
    if school is not None:
        keys.append(f"school:{school.pk}")
    keys.append("platform")
    overrides = {
        row.scope_key: row.state
        for row in CapabilityOverride.all_objects.filter(
            capability=capability, scope_key__in=keys
        )
    }
    for key in keys:
        state = overrides.get(key)
        if state and state != CapabilityOverride.State.INHERIT:
            return state == CapabilityOverride.State.ENABLED
    # No concrete override. Reaching here means the entitlement gate passed,
    # so a plan-gated capability is ON — being in the plan is what switches it
    # on (a DISABLED override is the lever to suppress it). Ungated
    # capabilities fall back to the catalogue default.
    if capability.requires_entitlement:
        return True
    return capability.default_enabled


# Update the grant that allows a school or platform scope to use a capability.
@transaction.atomic
def set_entitlement(*, capability, school, state, source, actor, reason=""):
    scope_key = f"school:{school.pk}" if school else "platform"
    # Entitlements are school/platform only; branch enablement is handled by overrides.
    current = CapabilityEntitlement.all_objects.filter(
        capability=capability, scope_key=scope_key
    ).first()
    # Capture the previous grant state for immutable configuration audit history.
    before = {"state": current.state, "source": current.source} if current else {}
    row, _ = CapabilityEntitlement.all_objects.update_or_create(
        capability=capability, scope_key=scope_key,
        defaults={"school": school, "state": state, "source": source, "updated_by": actor},
    )
    record_configuration_event(
        action="config.entitlement.updated", target=row, actor=actor, school=school,
        before=before, after={"state": state, "source": source}, reason=reason,
    )
    return row


# Update a scoped capability override after confirming entitlement constraints.
@transaction.atomic
def set_override(*, capability, state, actor, school=None, branch=None, reason=""):
    school, branch = normalize_scope(school=school, branch=branch)
    # Enabling is blocked when the scope has no active entitlement for the capability.
    if state == CapabilityOverride.State.ENABLED and not _active_entitlement(capability, school):
        raise CapabilityNotEntitled(
            f"'{capability.key}' cannot be enabled because it is not entitled."
        )
    scope_key = (
        f"branch:{branch.pk}" if branch else f"school:{school.pk}" if school else "platform"
    )
    # Scope keys are the stable lookup path used by both reads and writes.
    current = CapabilityOverride.all_objects.filter(
        capability=capability, scope_key=scope_key
    ).first()
    before = {"state": current.state} if current else {}
    # Preserve a before/after trail for every override flip, including INHERIT resets.
    row, _ = CapabilityOverride.all_objects.update_or_create(
        capability=capability,
        scope_key=scope_key,
        defaults={
            "school": school, "branch": branch, "state": state,
            "reason": reason, "updated_by": actor,
        },
    )
    record_configuration_event(
        action="config.override.updated", target=row, actor=actor, school=school,
        branch=branch, before=before, after={"state": state}, reason=reason,
    )
    return row
