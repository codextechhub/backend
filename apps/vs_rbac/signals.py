from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender="vs_rbac.PlatformUserRoleAssignment")
def sync_user_role_on_platform_assignment(sender, instance, **kwargs):
    """Keep User.role in sync with the user's active platform role assignment."""
    from .models import PlatformUserRoleAssignment

    user = instance.user
    active = (
        PlatformUserRoleAssignment.objects.filter(
            user=user,
            assignment_status=PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
        )
        .select_related("role")
        .order_by("-assigned_at")
        .first()
    )

    new_role = active.role.name if active else ""
    if user.role != new_role:
        user.role = new_role
        user.save(update_fields=["role"])
