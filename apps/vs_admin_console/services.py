from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from .models import ImpersonationSession


# Close ACTIVE sessions the actor abandoned without exiting or logging out.
def sweep_stale_impersonations():
    """Expire ACTIVE sessions past their deadline or idle beyond the limit."""
    now = timezone.now()
    idle_cutoff = now - timezone.timedelta(
        minutes=settings.IMPERSONATION_IDLE_TIMEOUT_MINUTES,
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
