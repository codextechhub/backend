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


# Determine whether a capability is entitled at the most specific available tenant scope.
def _active_entitlement(capability, tenant):
    if not capability.requires_entitlement:
        return True
    now = timezone.now()
    scope = Q(tenant__isnull=True)
    if tenant is not None:
        scope |= Q(tenant=tenant)
    rows = list(CapabilityEntitlement.all_objects.filter(capability=capability).filter(scope))
    # Tenant-specific grants override the platform grant for entitlement checks.
    specific = next((row for row in rows if row.tenant_id), None)
    entitlement = specific or next((row for row in rows if not row.tenant_id), None)
    if entitlement is None or entitlement.state != entitlement.State.GRANTED:
        return False
    # Future grants and expired grants are treated as inactive even if the row exists.
    if entitlement.starts_at and entitlement.starts_at > now:
        return False
    if entitlement.ends_at and entitlement.ends_at <= now:
        return False
    return True


# Resolve the final feature gate after entitlement, dependencies, and scoped overrides.
def effective_capability(capability, *, tenant=None, branch=None, _seen=None):
    tenant, branch = normalize_scope(tenant=tenant, branch=branch)
    if not capability.is_active or not _active_entitlement(capability, tenant):
        return False
    # Track dependency traversal so cyclic capability graphs fail loudly.
    seen = set(_seen or ())
    if capability.pk in seen:
        raise CapabilityDependencyError(f"Dependency cycle detected at '{capability.key}'.")
    seen.add(capability.pk)
    for link in capability.dependency_links.select_related("requires"):
        # A capability cannot be effective unless every required capability is effective too.
        if not effective_capability(link.requires, tenant=tenant, branch=branch, _seen=seen):
            return False

    # More specific overrides win: branch, then tenant, then platform.
    keys = []
    if branch is not None:
        keys.append(f"branch:{branch.pk}")
    if tenant is not None:
        keys.append(f"tenant:{tenant.pk}")
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


# Update the grant that allows a tenant or platform scope to use a capability.
@transaction.atomic
def set_entitlement(*, capability, tenant, state, source, actor, reason=""):
    scope_key = f"tenant:{tenant.pk}" if tenant else "platform"
    # Entitlements are tenant/platform only; branch enablement is handled by overrides.
    current = CapabilityEntitlement.all_objects.filter(
        capability=capability, scope_key=scope_key
    ).first()
    # Capture the previous grant state for immutable configuration audit history.
    before = {"state": current.state, "source": current.source} if current else {}
    row, _ = CapabilityEntitlement.all_objects.update_or_create(
        capability=capability, scope_key=scope_key,
        defaults={"tenant": tenant, "state": state, "source": source, "updated_by": actor},
    )
    record_configuration_event(
        action="config.entitlement.updated", target=row, actor=actor, tenant=tenant,
        before=before, after={"state": state, "source": source}, reason=reason,
    )
    return row


# Update a scoped capability override after confirming entitlement constraints.
@transaction.atomic
def set_override(*, capability, state, actor, tenant=None, branch=None, reason=""):
    tenant, branch = normalize_scope(tenant=tenant, branch=branch)
    # Enabling is blocked when the scope has no active entitlement for the capability.
    if state == CapabilityOverride.State.ENABLED and not _active_entitlement(capability, tenant):
        raise CapabilityNotEntitled(
            f"'{capability.key}' cannot be enabled because it is not entitled."
        )
    scope_key = (
        f"branch:{branch.pk}" if branch else f"tenant:{tenant.pk}" if tenant else "platform"
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
            "tenant": tenant, "branch": branch, "state": state,
            "reason": reason, "updated_by": actor,
        },
    )
    record_configuration_event(
        action="config.override.updated", target=row, actor=actor, tenant=tenant,
        branch=branch, before=before, after={"state": state}, reason=reason,
    )
    return row
