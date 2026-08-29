from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ..exceptions import CapabilityDependencyError, CapabilityNotEntitled
from ..models import (
    Capability,
    CapabilityEntitlement,
    CapabilityOverride,
)
from .audit import record_configuration_event
from .scopes import normalize_scope


UNSET = object()


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


def entitlement_resolution(capability, tenant):
    """Return the winning entitlement and its time-aware effective state."""
    if not capability.requires_entitlement:
        return None, True, "not_required"
    scope = Q(tenant__isnull=True)
    if tenant is not None:
        scope |= Q(tenant=tenant)
    rows = list(CapabilityEntitlement.all_objects.filter(capability=capability).filter(scope))
    specific = next((row for row in rows if row.tenant_id), None)
    row = specific or next((item for item in rows if not item.tenant_id), None)
    if row is None:
        return None, False, "not_granted"
    if row.state != row.State.GRANTED:
        return row, False, "denied"
    now = timezone.now()
    if row.starts_at and row.starts_at > now:
        return row, False, "scheduled"
    if row.ends_at and row.ends_at <= now:
        return row, False, "expired"
    return row, True, "active"


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
    # so a plan-gated capability is ON - being in the plan is what switches it
    # on (a DISABLED override is the lever to suppress it). Ungated
    # capabilities fall back to the catalogue default.
    if capability.requires_entitlement:
        return True
    return capability.default_enabled


# Update the grant that allows a tenant or platform scope to use a capability.
@transaction.atomic
def set_entitlement(
    *, capability, tenant, state, source, actor, starts_at=None, ends_at=None, reason=""
):
    scope_key = f"tenant:{tenant.pk}" if tenant else "platform"
    # Entitlements are tenant/platform only; branch enablement is handled by overrides.
    current = CapabilityEntitlement.all_objects.filter(
        capability=capability, scope_key=scope_key
    ).first()
    # Capture the previous grant state for immutable configuration audit history.
    before = {
        "state": current.state,
        "source": current.source,
        "starts_at": current.starts_at.isoformat() if current.starts_at else None,
        "ends_at": current.ends_at.isoformat() if current.ends_at else None,
    } if current else {}
    row, _ = CapabilityEntitlement.all_objects.update_or_create(
        capability=capability, scope_key=scope_key,
        defaults={
            "tenant": tenant, "state": state, "source": source,
            "starts_at": starts_at, "ends_at": ends_at, "updated_by": actor,
        },
    )
    after = {
        "state": state,
        "source": source,
        "starts_at": starts_at.isoformat() if starts_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
    }
    record_configuration_event(
        action="config.entitlement.updated", target=row, actor=actor, tenant=tenant,
        before=before, after=after, reason=reason,
    )
    return row


@transaction.atomic
def bulk_schedule_entitlements(
    *, targets, actor, reason, starts_at=UNSET, ends_at=UNSET,
):
    """Apply dates without changing an existing entitlement decision or source."""
    target_keys = [
        (capability.pk, f"tenant:{tenant.pk}" if tenant else "platform")
        for capability, tenant in targets
    ]
    capability_ids = [key[0] for key in target_keys]
    scope_keys = [key[1] for key in target_keys]
    current_rows = {
        (row.capability_id, row.scope_key): row
        for row in CapabilityEntitlement.all_objects.select_for_update().filter(
            capability_id__in=capability_ids,
            scope_key__in=scope_keys,
        )
    }
    for capability, tenant in targets:
        scope_key = f"tenant:{tenant.pk}" if tenant else "platform"
        row = current_rows.get((capability.pk, scope_key))
        if row and row.state == CapabilityEntitlement.State.DENIED:
            raise ValidationError({
                "items": (
                    f"Cannot schedule {capability.label} at "
                    f"{tenant.name if tenant else 'platform'} scope because it is "
                    "explicitly denied. Grant the entitlement first."
                )
            })
    results = []
    for capability, tenant in targets:
        scope_key = f"tenant:{tenant.pk}" if tenant else "platform"
        row = current_rows.get((capability.pk, scope_key))
        before = {
            "state": row.state,
            "source": row.source,
            "starts_at": row.starts_at.isoformat() if row.starts_at else None,
            "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        } if row else {}
        effective_starts = (
            row.starts_at if starts_at is UNSET and row else
            None if starts_at is UNSET else starts_at
        )
        effective_ends = (
            row.ends_at if ends_at is UNSET and row else
            None if ends_at is UNSET else ends_at
        )
        if effective_starts and effective_ends and effective_starts >= effective_ends:
            raise ValidationError({
                "ends_at": (
                    f"Expiry must be after activation for {capability.label} "
                    f"at {tenant.name if tenant else 'platform'} scope."
                )
            })
        if row is None:
            row = CapabilityEntitlement(
                capability=capability,
                tenant=tenant,
                scope_key=scope_key,
                state=CapabilityEntitlement.State.GRANTED,
                source=CapabilityEntitlement.Source.MANUAL,
            )
        row.starts_at = effective_starts
        row.ends_at = effective_ends
        row.updated_by = actor
        row.save()
        after = {
            "state": row.state,
            "source": row.source,
            "starts_at": row.starts_at.isoformat() if row.starts_at else None,
            "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        }
        record_configuration_event(
            action="config.entitlement.updated",
            target=row,
            actor=actor,
            tenant=tenant,
            before=before,
            after=after,
            reason=reason,
            metadata={"bulk_schedule": True},
        )
        results.append(row)
    return results


@transaction.atomic
def clear_entitlement(*, capability, tenant, actor, reason=""):
    """Delete only the selected entitlement layer so its parent can take over."""
    scope_key = f"tenant:{tenant.pk}" if tenant else "platform"
    row = CapabilityEntitlement.all_objects.filter(
        capability=capability, scope_key=scope_key,
    ).first()
    if row is None:
        return False
    before = {
        "state": row.state,
        "source": row.source,
        "starts_at": row.starts_at.isoformat() if row.starts_at else None,
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
    }
    row.delete()
    record_configuration_event(
        action="config.entitlement.cleared", target=capability, actor=actor,
        tenant=tenant, before=before, after={}, reason=reason,
    )
    return True


class BulkCapabilityEvaluator:
    """Evaluate a catalogue using a fixed set of preloaded database queries."""

    def __init__(self, capabilities, *, tenant=None, branch=None):
        self.tenant, self.branch = normalize_scope(tenant=tenant, branch=branch)
        self.capabilities = {item.pk: item for item in capabilities}
        capability_ids = list(self.capabilities)
        self.dependencies = {
            item.pk: [link.requires_id for link in item.dependency_links.all()]
            for item in self.capabilities.values()
        }
        entitlement_scope = Q(tenant__isnull=True)
        if self.tenant is not None:
            entitlement_scope |= Q(tenant=self.tenant)
        self.entitlements = {}
        for row in CapabilityEntitlement.all_objects.filter(
            capability_id__in=capability_ids,
        ).filter(entitlement_scope):
            current = self.entitlements.get(row.capability_id)
            if current is None or (row.tenant_id and not current.tenant_id):
                self.entitlements[row.capability_id] = row

        self.scope_keys = []
        if self.branch is not None:
            self.scope_keys.append(f"branch:{self.branch.pk}")
        if self.tenant is not None:
            self.scope_keys.append(f"tenant:{self.tenant.pk}")
        self.scope_keys.append("platform")
        self.overrides = {
            (row.capability_id, row.scope_key): row.state
            for row in CapabilityOverride.all_objects.filter(
                capability_id__in=capability_ids, scope_key__in=self.scope_keys,
            )
        }
        self.memo = {}

    def _entitled(self, capability):
        if not capability.requires_entitlement:
            return True
        row = self.entitlements.get(capability.pk)
        if row is None or row.state != row.State.GRANTED:
            return False
        now = timezone.now()
        return not (
            (row.starts_at and row.starts_at > now)
            or (row.ends_at and row.ends_at <= now)
        )

    def evaluate(self, capability_id, seen=None):
        if capability_id in self.memo:
            return self.memo[capability_id]
        capability = self.capabilities[capability_id]
        path = set(seen or ())
        if capability_id in path:
            raise CapabilityDependencyError(f"Dependency cycle detected at '{capability.key}'.")
        path.add(capability_id)
        if not capability.is_active or not self._entitled(capability):
            self.memo[capability_id] = False
            return False
        for requirement_id in self.dependencies.get(capability_id, []):
            if not self.evaluate(requirement_id, path):
                self.memo[capability_id] = False
                return False
        for scope_key in self.scope_keys:
            state = self.overrides.get((capability_id, scope_key))
            if state and state != CapabilityOverride.State.INHERIT:
                result = state == CapabilityOverride.State.ENABLED
                self.memo[capability_id] = result
                return result
        result = True if capability.requires_entitlement else capability.default_enabled
        self.memo[capability_id] = result
        return result


def bulk_effective_capabilities(*, tenant=None, branch=None):
    """Return all active capability states without request-per-row evaluation."""
    capabilities = list(
        Capability.objects.all().prefetch_related("dependency_links")
    )
    evaluator = BulkCapabilityEvaluator(capabilities, tenant=tenant, branch=branch)
    return [
        {"key": item.key, "enabled": evaluator.evaluate(item.pk)}
        for item in capabilities if item.is_active
    ]


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
