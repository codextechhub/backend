"""
Service layer for role change approval workflows.

Handles:
- School role change request approval and application
- Platform role change request approval and application
- Dependency validation before applying changes
- Audit trail generation
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from vs_audit.models import AuditModuleKey, AuditActionType
from vs_audit.services import emit_audit_event

from .models import (
    PlatformRoleChangeDeltaItem,
    PlatformRoleChangeRequest,
    PlatformRoleGroup,
    PlatformRolePermission,
    PlatformRoleTemplate,
    Permission,
    RoleChangeDeltaItem,
    RoleChangeRequest,
    RoleGroup,
    RolePermission,
    RoleTemplate,
)
from .validators import validate_role_permissions


def apply_school_role_change_request(obj: RoleChangeRequest, reviewer, notes: str = ""):
    """
    Apply approved school role change request.
    
    This atomically:
    1. Validates dependencies
    2. Applies ADD/REMOVE operations
    3. Bumps role version
    4. Marks request as approved
    5. Creates audit trail
    
    Raises exception if validation fails or apply fails.
    """
    with transaction.atomic():
        target_role = obj.target_role
        
        # Get current permission keys — snapshot before any changes are applied
        current_keys = set(
            RolePermission.objects.filter(
                role=target_role,
                granted=True
            ).values_list('permission_id', flat=True)
        )
        before_keys = sorted(current_keys)  # captured before mutations

        # Apply delta items
        delta_items = obj.delta_items.select_related('permission').all()

        for item in delta_items:
            if item.operation == RoleChangeDeltaItem.Operation.ADD:
                current_keys.add(item.permission_id)
            elif item.operation == RoleChangeDeltaItem.Operation.REMOVE:
                current_keys.discard(item.permission_id)

        # Validate final permission set — include permissions coming from any
        # groups already attached to the role so dependency checks pass for
        # permissions provided via groups rather than direct grants.
        final_keys = sorted(current_keys)
        attached_group_ids = list(
            RoleGroup.objects.filter(role=target_role).values_list("group_id", flat=True)
        )
        validate_role_permissions(
            permission_keys=final_keys,
            group_ids=attached_group_ids,
        )  # Raises ValidationError if invalid

        # Apply changes
        RolePermission.objects.filter(role=target_role).delete()
        
        perms = Permission.objects.filter(key__in=final_keys)
        RolePermission.objects.bulk_create([
            RolePermission(
                role=target_role,
                permission=perm,
                granted=True,
                granted_by=reviewer,
                granted_at=timezone.now(),
            )
            for perm in perms
        ])
        
        # Bump role version to invalidate caches
        target_role.bump_version()
        target_role.save(update_fields=['version', 'updated_at'])
        
        # Mark request as approved
        obj.mark_approved(reviewer=reviewer, notes=notes)
        obj.save(update_fields=[
            'status',
            'reviewer',
            'reviewer_notes',
            'decided_at',
            'updated_at',
        ])

        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.PERMISSION_CHANGED,
            actor_user=reviewer,
            entity_type="RoleTemplate",
            entity_id=str(target_role.pk),
            entity_label=getattr(target_role, "name", str(target_role.pk)),
            summary=f"School role '{getattr(target_role, 'name', target_role.pk)}' permissions updated via approved change request",
            before_data={"permission_keys": before_keys},
            diff_data={"permission_keys": {"before": before_keys, "after": final_keys}},
            metadata={
                "change_request_id": str(obj.pk),
                "reviewer_notes": notes,
            },
        )


def apply_platform_role_change_request(obj: PlatformRoleChangeRequest, reviewer, notes: str = ""):
    """
    Apply approved platform role change request.
    
    Same logic as school role changes but for platform roles.
    """
    with transaction.atomic():
        target_role = obj.target_role
        
        # Get current permission keys — snapshot before any changes are applied
        current_keys = set(
            PlatformRolePermission.objects.filter(
                role=target_role,
                granted=True
            ).values_list('permission_id', flat=True)
        )
        before_keys = sorted(current_keys)  # captured before mutations

        # Apply delta items
        delta_items = obj.delta_items.select_related('permission').all()

        for item in delta_items:
            if item.operation == PlatformRoleChangeDeltaItem.Operation.ADD:
                current_keys.add(item.permission_id)
            elif item.operation == PlatformRoleChangeDeltaItem.Operation.REMOVE:
                current_keys.discard(item.permission_id)

        # Validate final permission set (includes group-derived permissions)
        final_keys = sorted(current_keys)
        attached_group_ids = list(
            PlatformRoleGroup.objects.filter(role=target_role).values_list(
                "group_id", flat=True
            )
        )
        validate_role_permissions(
            permission_keys=final_keys,
            group_ids=attached_group_ids,
        )

        # Apply changes
        PlatformRolePermission.objects.filter(role=target_role).delete()
        
        perms = Permission.objects.filter(key__in=final_keys)
        PlatformRolePermission.objects.bulk_create([
            PlatformRolePermission(
                role=target_role,
                permission=perm,
                granted=True,
                granted_by=reviewer,
                granted_at=timezone.now(),
            )
            for perm in perms
        ])
        
        # Bump version
        target_role.bump_version()
        target_role.save(update_fields=['version', 'updated_at'])
        
        # Mark approved
        obj.mark_approved(reviewer=reviewer, notes=notes)
        obj.save(update_fields=[
            'status',
            'reviewer',
            'reviewer_notes',
            'decided_at',
            'updated_at',
        ])

        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.PERMISSION_CHANGED,
            actor_user=reviewer,
            entity_type="PlatformRoleTemplate",
            entity_id=str(target_role.pk),
            entity_label=getattr(target_role, "name", str(target_role.pk)),
            summary=f"Platform role '{getattr(target_role, 'name', target_role.pk)}' permissions updated via approved change request",
            before_data={"permission_keys": before_keys},
            diff_data={"permission_keys": {"before": before_keys, "after": final_keys}},
            metadata={
                "change_request_id": str(obj.pk),
                "reviewer_notes": notes,
            },
        )