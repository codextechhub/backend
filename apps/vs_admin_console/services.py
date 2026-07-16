from django.db.models import Q
from django.utils import timezone

from .models import ImpersonationSession


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
