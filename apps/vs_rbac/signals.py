from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import (
    GroupPermission,
    Permission,
    PermissionAction,
    PermissionDependency,
    PermissionModule,
    PermissionResource,
    PlatformRoleChangeRequest,
    PlatformRoleGroup,
    PlatformRoleTemplate,
    PlatformUserRoleAssignment,
    SchoolRoleChangeRequest,
    SchoolRoleGroup,
    SchoolRoleTemplate,
    SchoolUserRoleAssignment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Snapshot status before save so lifecycle receivers can audit real transitions.
def _capture_old_status(sender, instance, **kwargs):
    """Attach the pre-save status to the instance so post_save can diff it."""
    if not instance.pk:
        instance._pre_save_status = None
        return
    try:
        instance._pre_save_status = sender.objects.values_list("status", flat=True).get(pk=instance.pk)
    except sender.DoesNotExist:
        instance._pre_save_status = None


# Snapshot active state before save so deactivation audits can show the prior value.
def _capture_old_is_active(sender, instance, **kwargs):
    """Attach the pre-save is_active flag to the instance for diff checks."""
    if not instance.pk:
        instance._pre_save_is_active = None
        return
    try:
        instance._pre_save_is_active = sender.objects.values_list("is_active", flat=True).get(pk=instance.pk)
    except sender.DoesNotExist:
        instance._pre_save_is_active = None


# ---------------------------------------------------------------------------
# PlatformUserRoleAssignment — sync User.role + audit
# (existing signals preserved unchanged)
# ---------------------------------------------------------------------------

@receiver(post_save, sender=PlatformUserRoleAssignment)
# Mirror the active platform role onto User.role for legacy consumers.
def sync_user_role_on_platform_assignment(sender, instance, **kwargs):
    """Keep User.role in sync with the user's active platform role assignment."""
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


@receiver(post_save, sender=PlatformUserRoleAssignment)
# Audit platform role assignments and status changes.
def audit_platform_role_assignment(sender, instance, created, **kwargs):
    """Emit an AuditEvent whenever a platform role is assigned or its status changes."""
    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    action_type = AuditActionType.ROLE_ASSIGNED if created else AuditActionType.ROLE_CHANGED
    role_name = getattr(getattr(instance, "role", None), "name", "")
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

    current_user_role = getattr(user, "role", "")

    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=action_type,
        actor_user=active.assigned_by if active else None,
        entity_type="User",
        entity_id=str(user.pk),
        entity_label=getattr(user, "email", str(user.pk)),
        summary=f"Platform role '{role_name}' {'assigned' if created else 'updated'} for {getattr(user, 'email', user.pk)}",
        before_data={"role_name": "" if created else current_user_role},
        diff_data={
            "role_name": {
                "before": "" if created else current_user_role,
                "after": role_name,
            },
            "assignment_status": {
                "before": None if created else "previous",
                "after": getattr(instance, "assignment_status", ""),
            },
        },
        metadata={
            "assignment_id": str(instance.pk),
        },
    )


# ---------------------------------------------------------------------------
# SchoolUserRoleAssignment — school-level role assignment / revocation
# ---------------------------------------------------------------------------

@receiver(post_save, sender=SchoolUserRoleAssignment)
# Audit school-scoped role assignment and revocation events.
def audit_school_role_assignment(sender, instance, created, **kwargs):
    """Emit an audit event when a school role is assigned or revoked."""
    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    role_name = getattr(getattr(instance, "role", None), "name", "")
    user = instance.user

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.ROLE_ASSIGNED,
            actor_user=instance.assigned_by,
            entity_type="User",
            entity_id=str(user.pk),
            entity_label=getattr(user, "email", str(user.pk)),
            summary=f"School role '{role_name}' assigned to {getattr(user, 'email', user.pk)}",
            diff_data={"role_name": {"before": None, "after": role_name}},
            metadata={
                "assignment_id": str(instance.pk),
                "school_id": str(instance.school_id),
                "role_id": str(instance.role_id),
            },
        )
        return

    if instance.assignment_status == SchoolUserRoleAssignment.AssignmentStatus.REVOKED:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.ROLE_CHANGED,
            actor_user=instance.revoked_by,
            entity_type="User",
            entity_id=str(user.pk),
            entity_label=getattr(user, "email", str(user.pk)),
            summary=f"School role '{role_name}' revoked for {getattr(user, 'email', user.pk)}",
            diff_data={
                "assignment_status": {
                    "before": SchoolUserRoleAssignment.AssignmentStatus.ACTIVE,
                    "after": SchoolUserRoleAssignment.AssignmentStatus.REVOKED,
                }
            },
            metadata={
                "assignment_id": str(instance.pk),
                "school_id": str(instance.school_id),
                "role_id": str(instance.role_id),
                "reason_note": instance.reason_note,
            },
        )


# ---------------------------------------------------------------------------
# Permission — creation and deactivation
# ---------------------------------------------------------------------------

pre_save.connect(_capture_old_is_active, sender=Permission)


@receiver(post_save, sender=Permission)
# Audit creation and deactivation of permission keys.
def audit_permission_change(sender, instance, created, **kwargs):
    """Emit an audit event when a permission is created or deactivated."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.CREATE,
            entity_type="Permission",
            entity_id=str(instance.pk),
            entity_label=str(instance),
            severity=AuditSeverity.WARNING if instance.sensitivity_level != "NORMAL" else AuditSeverity.INFO,
            summary=f"Permission '{instance.key}' created (sensitivity={instance.sensitivity_level})",
            metadata={
                "key": instance.key,
                "sensitivity_level": instance.sensitivity_level,
                "is_restricted": instance.is_restricted,
            },
        )
        return

    old_active = getattr(instance, "_pre_save_is_active", None)
    if old_active is True and instance.is_active is False:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            entity_type="Permission",
            entity_id=str(instance.pk),
            entity_label=str(instance),
            severity=AuditSeverity.WARNING,
            summary=f"Permission '{instance.key}' deactivated",
            diff_data={"is_active": {"before": True, "after": False}},
            metadata={"key": instance.key},
        )


# ---------------------------------------------------------------------------
# PermissionDependency — dependency created / removed
# ---------------------------------------------------------------------------

@receiver(post_save, sender=PermissionDependency)
# Audit newly introduced permission prerequisites.
def audit_permission_dependency_created(sender, instance, created, **kwargs):
    """Emit an audit event when a permission dependency is created."""
    if not created:
        return

    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.CREATE,
        entity_type="PermissionDependency",
        entity_id=str(instance.pk),
        entity_label=str(instance),
        severity=AuditSeverity.WARNING,
        summary=f"Permission dependency added: '{instance.permission_id}' now requires '{instance.depends_on_id}'",
        metadata={
            "permission_key": instance.permission_id,
            "depends_on_key": instance.depends_on_id,
        },
    )


@receiver(post_delete, sender=PermissionDependency)
# Audit removal of permission prerequisites.
def audit_permission_dependency_removed(sender, instance, **kwargs):
    """Emit an audit event when a permission dependency is removed."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.DELETE,
        entity_type="PermissionDependency",
        entity_id=str(instance.pk),
        entity_label=str(instance),
        severity=AuditSeverity.WARNING,
        summary=f"Permission dependency removed: '{instance.permission_id}' no longer requires '{instance.depends_on_id}'",
        metadata={
            "permission_key": instance.permission_id,
            "depends_on_key": instance.depends_on_id,
        },
    )


# ---------------------------------------------------------------------------
# GroupPermission — permission added to / removed from a group
# ---------------------------------------------------------------------------

@receiver(post_save, sender=GroupPermission)
# Audit permission grants added through reusable groups.
def audit_group_permission_added(sender, instance, created, **kwargs):
    """Emit an audit event when a permission is added to a group."""
    if not created:
        return

    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    group_name = getattr(getattr(instance, "group", None), "name", str(instance.group_id))
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.PERMISSION_CHANGED,
        entity_type="PermissionGroup",
        entity_id=str(instance.group_id),
        entity_label=group_name,
        summary=f"Permission '{instance.permission_id}' added to group '{group_name}'",
        diff_data={"permission_key": {"before": None, "after": instance.permission_id}},
        metadata={"group_id": str(instance.group_id), "permission_key": instance.permission_id},
    )


@receiver(post_delete, sender=GroupPermission)
# Audit permission grants removed from reusable groups.
def audit_group_permission_removed(sender, instance, **kwargs):
    """Emit an audit event when a permission is removed from a group."""
    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    group_name = getattr(getattr(instance, "group", None), "name", str(instance.group_id))
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.PERMISSION_CHANGED,
        entity_type="PermissionGroup",
        entity_id=str(instance.group_id),
        entity_label=group_name,
        summary=f"Permission '{instance.permission_id}' removed from group '{group_name}'",
        diff_data={"permission_key": {"before": instance.permission_id, "after": None}},
        metadata={"group_id": str(instance.group_id), "permission_key": instance.permission_id},
    )


# ---------------------------------------------------------------------------
# SchoolRoleTemplate — creation and status changes
# ---------------------------------------------------------------------------

pre_save.connect(_capture_old_status, sender=SchoolRoleTemplate)


@receiver(post_save, sender=SchoolRoleTemplate)
# Audit school role template creation and lifecycle status changes.
def audit_school_role_template(sender, instance, created, **kwargs):
    """Emit an audit event when a school role template is created or its status changes."""
    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.CREATE,
            actor_user=instance.created_by,
            entity_type="SchoolRoleTemplate",
            entity_id=str(instance.pk),
            entity_label=instance.name,
            summary=f"School role template '{instance.name}' created",
            metadata={
                "school_id": str(instance.school_id),
                "is_system_role": instance.is_system_role,
            },
        )
        return

    old_status = getattr(instance, "_pre_save_status", None)
    if old_status and old_status != instance.status:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            entity_type="SchoolRoleTemplate",
            entity_id=str(instance.pk),
            entity_label=instance.name,
            summary=f"School role template '{instance.name}' status changed from '{old_status}' to '{instance.status}'",
            diff_data={"status": {"before": old_status, "after": instance.status}},
            metadata={"school_id": str(instance.school_id)},
        )


# ---------------------------------------------------------------------------
# PlatformRoleTemplate — creation and status changes
# ---------------------------------------------------------------------------

pre_save.connect(_capture_old_status, sender=PlatformRoleTemplate)


@receiver(post_save, sender=PlatformRoleTemplate)
# Audit platform role template creation and lifecycle status changes.
def audit_platform_role_template(sender, instance, created, **kwargs):
    """Emit an audit event when a platform role template is created or its status changes."""
    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.CREATE,
            actor_user=instance.created_by,
            entity_type="PlatformRoleTemplate",
            entity_id=str(instance.pk),
            entity_label=instance.name,
            summary=f"Platform role template '{instance.name}' created",
            metadata={"is_system_role": instance.is_system_role},
        )
        return

    old_status = getattr(instance, "_pre_save_status", None)
    if old_status and old_status != instance.status:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            entity_type="PlatformRoleTemplate",
            entity_id=str(instance.pk),
            entity_label=instance.name,
            summary=f"Platform role template '{instance.name}' status changed from '{old_status}' to '{instance.status}'",
            diff_data={"status": {"before": old_status, "after": instance.status}},
        )


# ---------------------------------------------------------------------------
# SchoolRoleGroup — group attached to / detached from a school role
# ---------------------------------------------------------------------------

@receiver(post_save, sender=SchoolRoleGroup)
# Audit permission groups attached to school roles.
def audit_school_role_group_attached(sender, instance, created, **kwargs):
    """Emit an audit event when a permission group is attached to a school role."""
    if not created:
        return

    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    role_name = getattr(getattr(instance, "role", None), "name", str(instance.role_id))
    group_name = getattr(getattr(instance, "group", None), "name", str(instance.group_id))
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.PERMISSION_CHANGED,
        actor_user=instance.attached_by,
        entity_type="SchoolRoleTemplate",
        entity_id=str(instance.role_id),
        entity_label=role_name,
        summary=f"Permission group '{group_name}' attached to school role '{role_name}'",
        metadata={"group_id": str(instance.group_id), "role_id": str(instance.role_id)},
    )


@receiver(post_delete, sender=SchoolRoleGroup)
# Audit permission groups detached from school roles.
def audit_school_role_group_detached(sender, instance, **kwargs):
    """Emit an audit event when a permission group is detached from a school role."""
    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    role_name = getattr(getattr(instance, "role", None), "name", str(instance.role_id))
    group_name = getattr(getattr(instance, "group", None), "name", str(instance.group_id))
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.PERMISSION_CHANGED,
        entity_type="SchoolRoleTemplate",
        entity_id=str(instance.role_id),
        entity_label=role_name,
        summary=f"Permission group '{group_name}' detached from school role '{role_name}'",
        metadata={"group_id": str(instance.group_id), "role_id": str(instance.role_id)},
    )


# ---------------------------------------------------------------------------
# PlatformRoleGroup — group attached to / detached from a platform role
# ---------------------------------------------------------------------------

@receiver(post_save, sender=PlatformRoleGroup)
# Audit permission groups attached to platform roles.
def audit_platform_role_group_attached(sender, instance, created, **kwargs):
    """Emit an audit event when a permission group is attached to a platform role."""
    if not created:
        return

    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    role_name = getattr(getattr(instance, "role", None), "name", str(instance.role_id))
    group_name = getattr(getattr(instance, "group", None), "name", str(instance.group_id))
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.PERMISSION_CHANGED,
        actor_user=instance.attached_by,
        entity_type="PlatformRoleTemplate",
        entity_id=str(instance.role_id),
        entity_label=role_name,
        summary=f"Permission group '{group_name}' attached to platform role '{role_name}'",
        metadata={"group_id": str(instance.group_id), "role_id": str(instance.role_id)},
    )


@receiver(post_delete, sender=PlatformRoleGroup)
# Audit permission groups detached from platform roles.
def audit_platform_role_group_detached(sender, instance, **kwargs):
    """Emit an audit event when a permission group is detached from a platform role."""
    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    role_name = getattr(getattr(instance, "role", None), "name", str(instance.role_id))
    group_name = getattr(getattr(instance, "group", None), "name", str(instance.group_id))
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.PERMISSION_CHANGED,
        entity_type="PlatformRoleTemplate",
        entity_id=str(instance.role_id),
        entity_label=role_name,
        summary=f"Permission group '{group_name}' detached from platform role '{role_name}'",
        metadata={"group_id": str(instance.group_id), "role_id": str(instance.role_id)},
    )


# ---------------------------------------------------------------------------
# SchoolRoleChangeRequest — submission and denial / apply-failure
# (approval + permission diff is already audited in services.apply_school_role_change_request)
# ---------------------------------------------------------------------------

pre_save.connect(_capture_old_status, sender=SchoolRoleChangeRequest)


@receiver(post_save, sender=SchoolRoleChangeRequest)
# Audit school role change request submission and failed/denied outcomes.
def audit_school_role_change_request(sender, instance, created, **kwargs):
    """Emit audit events for school role change request lifecycle transitions."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity, AuditStatus
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    role_name = getattr(getattr(instance, "target_role", None), "name", str(instance.target_role_id))

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            actor_user=instance.requested_by,
            entity_type="SchoolRoleChangeRequest",
            entity_id=str(instance.pk),
            entity_label=role_name,
            summary=f"School role change request submitted for role '{role_name}'",
            metadata={
                "school_id": str(instance.school_id),
                "role_id": str(instance.target_role_id),
                "justification": instance.justification,
            },
        )
        return

    old_status = getattr(instance, "_pre_save_status", None)
    if not old_status or old_status == instance.status:
        return

    if instance.status == SchoolRoleChangeRequest.Status.DENIED:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            actor_user=instance.reviewer,
            entity_type="SchoolRoleChangeRequest",
            entity_id=str(instance.pk),
            entity_label=role_name,
            severity=AuditSeverity.WARNING,
            status=AuditStatus.DENIED,
            summary=f"School role change request for '{role_name}' denied",
            diff_data={"status": {"before": old_status, "after": instance.status}},
            metadata={
                "school_id": str(instance.school_id),
                "reviewer_notes": instance.reviewer_notes,
            },
        )

    elif instance.status == SchoolRoleChangeRequest.Status.APPLY_FAILED:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            actor_user=instance.reviewer,
            entity_type="SchoolRoleChangeRequest",
            entity_id=str(instance.pk),
            entity_label=role_name,
            severity=AuditSeverity.CRITICAL,
            status=AuditStatus.FAILED,
            summary=f"School role change request for '{role_name}' failed to apply",
            diff_data={"status": {"before": old_status, "after": instance.status}},
            metadata={
                "school_id": str(instance.school_id),
                "reviewer_notes": instance.reviewer_notes,
            },
        )


# ---------------------------------------------------------------------------
# PlatformRoleChangeRequest — submission and denial / apply-failure
# (approval + permission diff is already audited in services.apply_platform_role_change_request)
# ---------------------------------------------------------------------------

pre_save.connect(_capture_old_status, sender=PlatformRoleChangeRequest)


@receiver(post_save, sender=PlatformRoleChangeRequest)
# Audit platform role change request submission and failed/denied outcomes.
def audit_platform_role_change_request(sender, instance, created, **kwargs):
    """Emit audit events for platform role change request lifecycle transitions."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity, AuditStatus
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    role_name = getattr(getattr(instance, "target_role", None), "name", str(instance.target_role_id))

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            actor_user=instance.requested_by,
            entity_type="PlatformRoleChangeRequest",
            entity_id=str(instance.pk),
            entity_label=role_name,
            summary=f"Platform role change request submitted for role '{role_name}'",
            metadata={
                "role_id": str(instance.target_role_id),
                "justification": instance.justification,
            },
        )
        return

    old_status = getattr(instance, "_pre_save_status", None)
    if not old_status or old_status == instance.status:
        return

    if instance.status == PlatformRoleChangeRequest.Status.DENIED:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            actor_user=instance.reviewer,
            entity_type="PlatformRoleChangeRequest",
            entity_id=str(instance.pk),
            entity_label=role_name,
            severity=AuditSeverity.WARNING,
            status=AuditStatus.DENIED,
            summary=f"Platform role change request for '{role_name}' denied",
            diff_data={"status": {"before": old_status, "after": instance.status}},
            metadata={"reviewer_notes": instance.reviewer_notes},
        )

    elif instance.status == PlatformRoleChangeRequest.Status.APPLY_FAILED:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            actor_user=instance.reviewer,
            entity_type="PlatformRoleChangeRequest",
            entity_id=str(instance.pk),
            entity_label=role_name,
            severity=AuditSeverity.CRITICAL,
            status=AuditStatus.FAILED,
            summary=f"Platform role change request for '{role_name}' failed to apply",
            diff_data={"status": {"before": old_status, "after": instance.status}},
            metadata={"reviewer_notes": instance.reviewer_notes},
        )


# ---------------------------------------------------------------------------
# PermissionModule — created, updated (is_active), deleted
# ---------------------------------------------------------------------------

pre_save.connect(_capture_old_is_active, sender=PermissionModule)


@receiver(post_save, sender=PermissionModule)
# Audit module-level permission vocabulary changes.
def audit_permission_module(sender, instance, created, **kwargs):
    """Emit an audit event on PermissionModule create or is_active change."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.CREATE,
            entity_type="PermissionModule",
            entity_id=instance.name,
            entity_label=instance.name,
            severity=AuditSeverity.WARNING,
            summary=f"Permission module '{instance.name}' created",
            metadata={"name": instance.name},
        )
        return

    old_active = getattr(instance, "_pre_save_is_active", None)
    if old_active is None or old_active == instance.is_active:
        return

    severity = AuditSeverity.CRITICAL if not instance.is_active else AuditSeverity.WARNING
    verb = "deactivated" if not instance.is_active else "reactivated"
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.UPDATE,
        entity_type="PermissionModule",
        entity_id=instance.name,
        entity_label=instance.name,
        severity=severity,
        summary=f"Permission module '{instance.name}' {verb} — all permissions under this module are affected",
        diff_data={"is_active": {"before": old_active, "after": instance.is_active}},
        metadata={"name": instance.name},
    )


@receiver(post_delete, sender=PermissionModule)
# Audit hard deletion of a permission module and its cascade impact.
def audit_permission_module_deleted(sender, instance, **kwargs):
    """Emit an audit event when a PermissionModule is hard-deleted."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.DELETE,
        entity_type="PermissionModule",
        entity_id=instance.name,
        entity_label=instance.name,
        severity=AuditSeverity.CRITICAL,
        summary=f"Permission module '{instance.name}' deleted — all associated permissions and resources are cascade-removed",
        metadata={"name": instance.name},
    )


# ---------------------------------------------------------------------------
# PermissionResource — created, updated (is_active), deleted
# ---------------------------------------------------------------------------

pre_save.connect(_capture_old_is_active, sender=PermissionResource)


@receiver(post_save, sender=PermissionResource)
# Audit resource-level permission vocabulary changes.
def audit_permission_resource(sender, instance, created, **kwargs):
    """Emit an audit event on PermissionResource create or is_active change."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    label = str(instance)  # "module.resource"

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.CREATE,
            entity_type="PermissionResource",
            entity_id=label,
            entity_label=label,
            severity=AuditSeverity.WARNING,
            summary=f"Permission resource '{label}' created",
            metadata={"module": instance.module_id, "resource": instance.name},
        )
        return

    old_active = getattr(instance, "_pre_save_is_active", None)
    if old_active is None or old_active == instance.is_active:
        return

    severity = AuditSeverity.CRITICAL if not instance.is_active else AuditSeverity.WARNING
    verb = "deactivated" if not instance.is_active else "reactivated"
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.UPDATE,
        entity_type="PermissionResource",
        entity_id=label,
        entity_label=label,
        severity=severity,
        summary=f"Permission resource '{label}' {verb} — all permissions under this resource are affected",
        diff_data={"is_active": {"before": old_active, "after": instance.is_active}},
        metadata={"module": instance.module_id, "resource": instance.name},
    )


@receiver(post_delete, sender=PermissionResource)
# Audit hard deletion of a permission resource and its cascade impact.
def audit_permission_resource_deleted(sender, instance, **kwargs):
    """Emit an audit event when a PermissionResource is hard-deleted."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    label = str(instance)
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.DELETE,
        entity_type="PermissionResource",
        entity_id=label,
        entity_label=label,
        severity=AuditSeverity.CRITICAL,
        summary=f"Permission resource '{label}' deleted — all permissions under this resource are cascade-removed",
        metadata={"module": instance.module_id, "resource": instance.name},
    )


# ---------------------------------------------------------------------------
# PermissionAction — created, updated (is_active), deleted
# ---------------------------------------------------------------------------

pre_save.connect(_capture_old_is_active, sender=PermissionAction)


@receiver(post_save, sender=PermissionAction)
# Audit action-verb permission vocabulary changes.
def audit_permission_action(sender, instance, created, **kwargs):
    """Emit an audit event on PermissionAction create or is_active change."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.CREATE,
            entity_type="PermissionAction",
            entity_id=instance.name,
            entity_label=instance.name,
            severity=AuditSeverity.WARNING,
            summary=f"Permission action '{instance.name}' created",
            metadata={"name": instance.name},
        )
        return

    old_active = getattr(instance, "_pre_save_is_active", None)
    if old_active is None or old_active == instance.is_active:
        return

    severity = AuditSeverity.CRITICAL if not instance.is_active else AuditSeverity.WARNING
    verb = "deactivated" if not instance.is_active else "reactivated"
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.UPDATE,
        entity_type="PermissionAction",
        entity_id=instance.name,
        entity_label=instance.name,
        severity=severity,
        summary=f"Permission action '{instance.name}' {verb} — all permissions using this action verb are affected",
        diff_data={"is_active": {"before": old_active, "after": instance.is_active}},
        metadata={"name": instance.name},
    )


@receiver(post_delete, sender=PermissionAction)
# Audit hard deletion of a permission action and its cascade impact.
def audit_permission_action_deleted(sender, instance, **kwargs):
    """Emit an audit event when a PermissionAction is hard-deleted."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.DELETE,
        entity_type="PermissionAction",
        entity_id=instance.name,
        entity_label=instance.name,
        severity=AuditSeverity.CRITICAL,
        summary=f"Permission action '{instance.name}' deleted — all permissions using this action verb are cascade-removed",
        metadata={"name": instance.name},
    )
