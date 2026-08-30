from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import ImpersonationSession


# Close ACTIVE sessions the actor abandoned without exiting or logging out.
def sweep_stale_impersonations():
    """Expire ACTIVE sessions past their deadline or idle beyond the limit."""
    from vs_config.runtime_settings import get_security_value

    now = timezone.now()
    idle_cutoff = now - timezone.timedelta(
        minutes=get_security_value("proxy_idle_timeout_minutes"),
    )
    return ImpersonationSession.objects.filter(
        Q(ends_at__lte=now) | Q(ends_at__isnull=True, last_activity_at__lt=idle_cutoff),
        status="ACTIVE",
    ).update(status="EXPIRED", ended_at=now)


# End every active proxy session involving a user whose access changed.
def end_impersonations_for_user(user):
    """End active sessions where the user is either actor or effective user."""
    # User disablement affects both the staff actor and the impersonated account.
    return ImpersonationSession.objects.filter(
        Q(staff_user=user) | Q(target_user=user), status="ACTIVE",
    ).update(status="ENDED", ended_at=timezone.now())


# End active proxy sessions when a tenant is suspended or deactivated.
def end_impersonations_for_tenant(tenant):
    """End every ACTIVE impersonation session scoped to the given tenant."""
    # Tenant-level shutdown is bulk and idempotent because sessions may already be ended.
    return ImpersonationSession.objects.filter(
        tenant=tenant, status="ACTIVE",
    ).update(status="ENDED", ended_at=timezone.now())


@transaction.atomic
def transition_tenant_status(
    tenant,
    *,
    to_status,
    activated_at=None,
    deactivated_at=None,
    updated_at=None,
):
    """Change one tenant's lifecycle state and enforce its access shutdown.

    Status writers call this service instead of relying on model signals. The
    tenant row is locked while its pending-spell stamps are derived, and the
    status write and impersonation shutdown commit or roll back together.

    ``activated_at`` and ``deactivated_at`` are explicit because they are part
    of the lifecycle state mirrored by the schools product. Other tenant
    metadata remains the owning product's responsibility.
    """
    from vs_tenants.models import Tenant

    if to_status not in Tenant.Status.values:
        raise ValueError(f"Unknown tenant status: {to_status}")

    tenant_id = getattr(tenant, "pk", tenant)
    locked = Tenant.objects.select_for_update().get(pk=tenant_id)
    pending_since, expiry_warned_at = Tenant.pending_stamps_for(
        new_status=to_status,
        previous_status=locked.status,
        pending_since=locked.pending_since,
        warned_at=locked.expiry_warned_at,
    )
    changed_at = updated_at or timezone.now()
    Tenant.objects.filter(pk=locked.pk).update(
        status=to_status,
        activated_at=activated_at,
        deactivated_at=deactivated_at,
        pending_since=pending_since,
        expiry_warned_at=expiry_warned_at,
        updated_at=changed_at,
    )

    # Leaving ACTIVE is a permanent session boundary, not a temporary request
    # gate. An old proxy session must never revive if the tenant later returns.
    if to_status != Tenant.Status.ACTIVE:
        end_impersonations_for_tenant(locked)

    # Keep an instance supplied by the caller coherent without another query.
    if getattr(tenant, "pk", None) is not None:
        tenant.status = to_status
        tenant.activated_at = activated_at
        tenant.deactivated_at = deactivated_at
        tenant.pending_since = pending_since
        tenant.expiry_warned_at = expiry_warned_at
        tenant.updated_at = changed_at

    return locked.status
