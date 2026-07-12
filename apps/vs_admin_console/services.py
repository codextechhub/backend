from django.db.models import Q
from django.utils import timezone

from .models import ImpersonationSession


def end_impersonations_for_user(user):
    """End active sessions where the user is either actor or effective user."""
    return ImpersonationSession.objects.filter(
        Q(staff_user=user) | Q(target_user=user), status="ACTIVE",
    ).update(status="ENDED", ended_at=timezone.now())


def end_impersonations_for_tenant(tenant):
    """End every ACTIVE impersonation session scoped to the given tenant."""
    return ImpersonationSession.objects.filter(
        tenant=tenant, status="ACTIVE",
    ).update(status="ENDED", ended_at=timezone.now())
