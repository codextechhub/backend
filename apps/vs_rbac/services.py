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
    PlatformUserRoleAssignment,
    Permission,
    SchoolRoleChangeDeltaItem,
    SchoolRoleChangeRequest,
    SchoolRoleGroup,
    SchoolRolePermission,
    SchoolRoleTemplate,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
)
from .validators import validate_role_permissions


def provision_role_from_prebuilt(*, school, branch=None, prebuilt_key: str, created_by=None):
    """
    Get or create a SchoolRoleTemplate from a PrebuiltRoleTemplate, copying
    its default permissions into the new role if it is freshly created.

    Returns the SchoolRoleTemplate, or None if the prebuilt key is not found.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    if not isinstance(created_by, User):
        created_by = None

    prebuilt = PrebuiltRoleTemplate.objects.filter(key=prebuilt_key, is_active=True).first()
    if not prebuilt:
        return None

    role, created = SchoolRoleTemplate.objects.get_or_create(
        school=school,
        branch=branch,
        prebuilt_from=prebuilt,
        defaults={
            "name": prebuilt.name,
            "description": prebuilt.description,
            "is_system_role": True,
            "is_locked": True,
            "created_by": created_by,
        },
    )

    if created:
        prebuilt_perms = PrebuiltRolePermission.objects.filter(
            prebuilt_role=prebuilt
        ).select_related("permission")
        SchoolRolePermission.objects.bulk_create(
            [
                SchoolRolePermission(
                    role=role,
                    permission=p.permission,
                    granted=True,
                    granted_by=created_by,
                )
                for p in prebuilt_perms
            ],
            ignore_conflicts=True,
        )

    return role


def apply_school_role_change_request(obj: SchoolRoleChangeRequest, reviewer, notes: str = ""):
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
            SchoolRolePermission.objects.filter(
                role=target_role,
                granted=True
            ).values_list('permission_id', flat=True)
        )
        before_keys = sorted(current_keys)  # captured before mutations

        # Apply delta items
        delta_items = obj.delta_items.select_related('permission').all()

        for item in delta_items:
            if item.operation == SchoolRoleChangeDeltaItem.Operation.ADD:
                current_keys.add(item.permission_id)
            elif item.operation == SchoolRoleChangeDeltaItem.Operation.REMOVE:
                current_keys.discard(item.permission_id)

        # Validate final permission set — include permissions coming from any
        # groups already attached to the role so dependency checks pass for
        # permissions provided via groups rather than direct grants.
        final_keys = sorted(current_keys)
        attached_group_ids = list(
            SchoolRoleGroup.objects.filter(role=target_role).values_list("group_id", flat=True)
        )
        validate_role_permissions(
            permission_keys=final_keys,
            group_ids=attached_group_ids,
        )  # Raises ValidationError if invalid

        # Apply changes
        SchoolRolePermission.objects.filter(role=target_role).delete()
        
        perms = Permission.objects.filter(key__in=final_keys)
        SchoolRolePermission.objects.bulk_create([
            SchoolRolePermission(
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
            entity_type="SchoolRoleTemplate",
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


SUPER_ADMIN_ROLE_ID   = "xvs_super_admin"
PLATFORM_ADMIN_ROLE_ID = "xvs_platform_admin"


@transaction.atomic
def transfer_super_admin(from_user, to_user):
    """
    Transfer the Vision Super Admin role from `from_user` to `to_user`.

    - `from_user` must currently hold the xvs_super_admin assignment.
    - `to_user` must be VISION_STAFF and different from `from_user`.
    - After transfer, `from_user` is demoted to vision-platform-admin.
    - Any existing active platform role on `to_user` is revoked first.
    - Both users' `is_superuser` flags are updated accordingly.

    Raises ValueError on any validation failure.
    """
    from django.conf import settings
    User = settings.AUTH_USER_MODEL
    from django.apps import apps
    UserModel = apps.get_model(*User.split("."))

    if from_user.pk == to_user.pk:
        raise ValueError("Cannot transfer super admin to yourself.")

    if getattr(to_user, "user_type", None) != "CX_STAFF":
        raise ValueError("The new super admin must be a Vision Staff member.")

    # Verify from_user actually holds the super admin role.
    active_assignment = PlatformUserRoleAssignment.objects.filter(
        user=from_user,
        role_id=SUPER_ADMIN_ROLE_ID,
        assignment_status=PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
    ).first()
    if not active_assignment:
        raise ValueError("You do not hold the Vision Super Admin role.")

    try:
        super_admin_role   = PlatformRoleTemplate.objects.get(id=SUPER_ADMIN_ROLE_ID)
        platform_admin_role = PlatformRoleTemplate.objects.get(id=PLATFORM_ADMIN_ROLE_ID)
    except PlatformRoleTemplate.DoesNotExist as exc:
        raise ValueError(f"Required platform role not found: {exc}") from exc

    now = timezone.now()

    # Revoke from_user's super admin.
    active_assignment.revoke(by_user=from_user, reason="Super admin role transferred to another user.")
    active_assignment.save(update_fields=["assignment_status", "revoked_at", "revoked_by", "reason_note", "updated_at"])

    # Revoke any existing active platform role on to_user.
    PlatformUserRoleAssignment.objects.filter(
        user=to_user,
        assignment_status=PlatformUserRoleAssignment.AssignmentStatus.ACTIVE,
    ).update(
        assignment_status=PlatformUserRoleAssignment.AssignmentStatus.REVOKED,
        revoked_at=now,
        revoked_by=from_user,
        reason_note="Role revoked as part of super admin transfer.",
    )

    # Assign from_user to platform admin.
    PlatformUserRoleAssignment.objects.create(
        user=from_user,
        role=platform_admin_role,
        assigned_by=from_user,
    )

    # Assign to_user to super admin.
    PlatformUserRoleAssignment.objects.create(
        user=to_user,
        role=super_admin_role,
        assigned_by=from_user,
    )

    # Sync is_superuser flag.
    UserModel.objects.filter(pk=from_user.pk).update(is_superuser=False)
    UserModel.objects.filter(pk=to_user.pk).update(is_superuser=True)

    emit_audit_event(
        actor_user=from_user,
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.ROLE_CHANGED,
        entity_type="PlatformUserRoleAssignment",
        entity_id=str(to_user.pk),
        entity_label=getattr(to_user, "email", str(to_user.pk)),
        summary=f"Super admin role transferred from {from_user.email} to {to_user.email}",
        metadata={"from_user_id": str(from_user.pk), "to_user_id": str(to_user.pk)},
    )


@transaction.atomic
def create_role_from_suggestion(suggestion_key: str, school, created_by) -> SchoolRoleTemplate:
    """
    Creates a SchoolRoleTemplate for a school based on a PrebuiltRoleTemplate.

    Looks up the suggestion by key, creates a SchoolRoleTemplate scoped to the school
    with the suggestion's name/scope, then bulk-copies the default permissions.
    The PrebuiltRoleTemplate is never modified.

    Raises PrebuiltRoleTemplate.DoesNotExist if the key is not found.
    Raises ValueError if the school already has a role with this name.
    """
    suggestion = PrebuiltRoleTemplate.objects.get(key=suggestion_key, is_active=True)

    if SchoolRoleTemplate.objects.filter(school=school, name=suggestion.name).exists():
        raise ValueError(
            f'This school already has a role named "{suggestion.name}". '
            f'Rename the existing role before creating another with this name.'
        )

    role = SchoolRoleTemplate.objects.create(
        name=suggestion.name,
        school=school,
        prebuilt_from=suggestion,
        created_by=created_by,
    )

    default_permissions = suggestion.default_permissions.select_related('permission').all()
    SchoolRolePermission.objects.bulk_create([
        SchoolRolePermission(role=role, permission=dp.permission)
        for dp in default_permissions
    ], ignore_conflicts=True)

    return role