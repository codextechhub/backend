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


@receiver(post_save, sender="vs_rbac.PlatformUserRoleAssignment")
def audit_platform_role_assignment(sender, instance, created, **kwargs):
    """Emit an AuditEvent whenever a platform role is assigned or its status changes."""
    from vs_audit.models import AuditModuleKey, AuditActionType
    from vs_audit.services import emit_audit_event

    action_type = AuditActionType.ROLE_ASSIGNED if created else AuditActionType.ROLE_CHANGED
    role_name = getattr(getattr(instance, "role", None), "name", "")
    user = instance.user

    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=action_type,
        actor_user=None,  # system-driven; no request context in signals
        entity_type="User",
        entity_id=str(user.pk),
        entity_label=getattr(user, "email", str(user.pk)),
        summary=f"Platform role '{role_name}' {'assigned' if created else 'updated'} for {getattr(user, 'email', user.pk)}",
        metadata={
            "role_name": role_name,
            "assignment_status": getattr(instance, "assignment_status", ""),
            "assignment_id": str(instance.pk),
        },
    )
