from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from .models import (
    GroupPermission,
    Permission,
    PermissionAction,
    PermissionDependency,
    PermissionModule,
    PermissionResource,
    TenantRoleChangeRequest,
    TenantRoleGroup,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
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


# ===========================================================================
# Unified tenant RBAC audit (canonical tables — mirror the legacy receivers
# with entity types updated to the tenant models)
# ===========================================================================

# ---------------------------------------------------------------------------
# TenantUserRoleAssignment — role assignment / revocation
# ---------------------------------------------------------------------------

@receiver(post_save, sender=TenantUserRoleAssignment)
# Audit tenant-scoped role assignment and revocation events.
def audit_tenant_role_assignment(sender, instance, created, **kwargs):
    """Emit an audit event when a tenant role is assigned or revoked."""
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
            summary=f"Role '{role_name}' assigned to {getattr(user, 'email', user.pk)}",
            diff_data={"role_name": {"before": None, "after": role_name}},
            metadata={
                "assignment_id": str(instance.pk),
                "tenant_id": str(instance.tenant_id),
                "role_id": str(instance.role_id),
            },
        )
        return

    if instance.assignment_status == TenantUserRoleAssignment.AssignmentStatus.REVOKED:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.ROLE_CHANGED,
            actor_user=instance.revoked_by,
            entity_type="User",
            entity_id=str(user.pk),
            entity_label=getattr(user, "email", str(user.pk)),
            summary=f"Role '{role_name}' revoked for {getattr(user, 'email', user.pk)}",
            diff_data={
                "assignment_status": {
                    "before": TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
                    "after": TenantUserRoleAssignment.AssignmentStatus.REVOKED,
                }
            },
            metadata={
                "assignment_id": str(instance.pk),
                "tenant_id": str(instance.tenant_id),
                "role_id": str(instance.role_id),
                "reason_note": instance.reason_note,
            },
        )


# ---------------------------------------------------------------------------
# TenantRoleTemplate — creation and status changes
# ---------------------------------------------------------------------------

pre_save.connect(_capture_old_status, sender=TenantRoleTemplate)


@receiver(post_save, sender=TenantRoleTemplate)
# Audit tenant role template creation and lifecycle status changes.
def audit_tenant_role_template(sender, instance, created, **kwargs):
    """Emit an audit event when a tenant role template is created or its status changes."""
    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.CREATE,
            actor_user=instance.created_by,
            entity_type="TenantRoleTemplate",
            entity_id=str(instance.pk),
            entity_label=instance.name,
            summary=f"Role template '{instance.name}' created",
            metadata={
                "tenant_id": str(instance.tenant_id),
                "is_system_role": instance.is_system_role,
            },
        )
        return

    old_status = getattr(instance, "_pre_save_status", None)
    if old_status and old_status != instance.status:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            entity_type="TenantRoleTemplate",
            entity_id=str(instance.pk),
            entity_label=instance.name,
            summary=f"Role template '{instance.name}' status changed from '{old_status}' to '{instance.status}'",
            diff_data={"status": {"before": old_status, "after": instance.status}},
            metadata={"tenant_id": str(instance.tenant_id)},
        )


# ---------------------------------------------------------------------------
# TenantRoleGroup — group attached to / detached from a tenant role
# ---------------------------------------------------------------------------

@receiver(post_save, sender=TenantRoleGroup)
# Audit permission groups attached to tenant roles.
def audit_tenant_role_group_attached(sender, instance, created, **kwargs):
    """Emit an audit event when a permission group is attached to a tenant role."""
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
        entity_type="TenantRoleTemplate",
        entity_id=str(instance.role_id),
        entity_label=role_name,
        summary=f"Permission group '{group_name}' attached to role '{role_name}'",
        metadata={"group_id": str(instance.group_id), "role_id": str(instance.role_id)},
    )


@receiver(post_delete, sender=TenantRoleGroup)
# Audit permission groups detached from tenant roles.
def audit_tenant_role_group_detached(sender, instance, **kwargs):
    """Emit an audit event when a permission group is detached from a tenant role."""
    from vs_audit.models import AuditActionType, AuditModuleKey
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    role_name = getattr(getattr(instance, "role", None), "name", str(instance.role_id))
    group_name = getattr(getattr(instance, "group", None), "name", str(instance.group_id))
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.PERMISSION_CHANGED,
        entity_type="TenantRoleTemplate",
        entity_id=str(instance.role_id),
        entity_label=role_name,
        summary=f"Permission group '{group_name}' detached from role '{role_name}'",
        metadata={"group_id": str(instance.group_id), "role_id": str(instance.role_id)},
    )


# ---------------------------------------------------------------------------
# TenantRoleChangeRequest — submission and denial / apply-failure
# (approval + permission diff is audited in services.apply_role_change_request)
# ---------------------------------------------------------------------------

pre_save.connect(_capture_old_status, sender=TenantRoleChangeRequest)


@receiver(post_save, sender=TenantRoleChangeRequest)
# Audit tenant role change request submission and failed/denied outcomes.
def audit_tenant_role_change_request(sender, instance, created, **kwargs):
    """Emit audit events for tenant role change request lifecycle transitions."""
    from vs_audit.models import AuditActionType, AuditModuleKey, AuditSeverity, AuditStatus
    from vs_rbac.audit import record_rbac_audit as emit_audit_event

    role_name = getattr(getattr(instance, "target_role", None), "name", str(instance.target_role_id))

    if created:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            actor_user=instance.requested_by,
            entity_type="TenantRoleChangeRequest",
            entity_id=str(instance.pk),
            entity_label=role_name,
            summary=f"Role change request submitted for role '{role_name}'",
            metadata={
                "tenant_id": str(instance.tenant_id),
                "role_id": str(instance.target_role_id),
                "justification": instance.justification,
            },
        )
        return

    old_status = getattr(instance, "_pre_save_status", None)
    if not old_status or old_status == instance.status:
        return

    if instance.status == TenantRoleChangeRequest.Status.DENIED:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            actor_user=instance.reviewer,
            entity_type="TenantRoleChangeRequest",
            entity_id=str(instance.pk),
            entity_label=role_name,
            severity=AuditSeverity.WARNING,
            status=AuditStatus.DENIED,
            summary=f"Role change request for '{role_name}' denied",
            diff_data={"status": {"before": old_status, "after": instance.status}},
            metadata={
                "tenant_id": str(instance.tenant_id),
                "reviewer_notes": instance.reviewer_notes,
            },
        )

    elif instance.status == TenantRoleChangeRequest.Status.APPLY_FAILED:
        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.UPDATE,
            actor_user=instance.reviewer,
            entity_type="TenantRoleChangeRequest",
            entity_id=str(instance.pk),
            entity_label=role_name,
            severity=AuditSeverity.CRITICAL,
            status=AuditStatus.FAILED,
            summary=f"Role change request for '{role_name}' failed to apply",
            diff_data={"status": {"before": old_status, "after": instance.status}},
            metadata={
                "tenant_id": str(instance.tenant_id),
                "reviewer_notes": instance.reviewer_notes,
            },
        )
