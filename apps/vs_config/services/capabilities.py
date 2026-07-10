from django.db import transaction
from django.utils import timezone

from ..exceptions import CapabilityDependencyError, CapabilityNotEntitled
from ..models import (
    Capability,
    CapabilityEntitlement,
    CapabilityOverride,
)
from .audit import record_configuration_event
from .scopes import normalize_scope


def _active_entitlement(capability, school):
    if not capability.requires_entitlement:
        return True
    now = timezone.now()
    candidates = CapabilityEntitlement.all_objects.filter(capability=capability)
    specific = candidates.filter(school=school).first() if school is not None else None
    entitlement = specific or candidates.filter(school__isnull=True).first()
    if entitlement is None or entitlement.state != entitlement.State.GRANTED:
        return False
    if entitlement.starts_at and entitlement.starts_at > now:
        return False
    if entitlement.ends_at and entitlement.ends_at <= now:
        return False
    return True


def effective_capability(capability, *, school=None, branch=None, _seen=None):
    school, branch = normalize_scope(school=school, branch=branch)
    if not capability.is_active or not _active_entitlement(capability, school):
        return False
    seen = set(_seen or ())
    if capability.pk in seen:
        raise CapabilityDependencyError(f"Dependency cycle detected at '{capability.key}'.")
    seen.add(capability.pk)
    for link in capability.dependency_links.select_related("requires"):
        if not effective_capability(link.requires, school=school, branch=branch, _seen=seen):
            return False

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
    return capability.default_enabled


@transaction.atomic
def set_entitlement(*, capability, school, state, source, actor, reason=""):
    scope_key = f"school:{school.pk}" if school else "platform"
    current = CapabilityEntitlement.all_objects.filter(
        capability=capability, scope_key=scope_key
    ).first()
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


@transaction.atomic
def set_override(*, capability, state, actor, school=None, branch=None, reason=""):
    school, branch = normalize_scope(school=school, branch=branch)
    if state == CapabilityOverride.State.ENABLED and not _active_entitlement(capability, school):
        raise CapabilityNotEntitled(
            f"'{capability.key}' cannot be enabled because it is not entitled."
        )
    scope_key = (
        f"branch:{branch.pk}" if branch else f"school:{school.pk}" if school else "platform"
    )
    current = CapabilityOverride.all_objects.filter(
        capability=capability, scope_key=scope_key
    ).first()
    before = {"state": current.state} if current else {}
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
