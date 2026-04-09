"""
Service layer for role change approval workflows.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import (
    RoleChangeRequest,
    RoleChangeDeltaItem,
    RolePermission,
    Permission,
    PlatformRoleChangeRequest,
    PlatformRoleChangeDeltaItem,
    PlatformRolePermission,
)
from .validators import validate_role_permissions


def apply_institution_role_change_request(obj: RoleChangeRequest, reviewer, notes: str = ""):
    """
    Atomically apply an approved institution role change request.
    Validates dependencies, applies ADD/REMOVE deltas, bumps version.
    """
    with transaction.atomic():
        target_role = obj.target_role

        current_keys = set(
            RolePermission.objects.filter(role=target_role, granted=True)
            .values_list('permission_id', flat=True)
        )

        for item in obj.delta_items.select_related('permission').all():
            if item.operation == RoleChangeDeltaItem.Operation.ADD:
                current_keys.add(item.permission_id)
            elif item.operation == RoleChangeDeltaItem.Operation.REMOVE:
                current_keys.discard(item.permission_id)

        final_keys = sorted(current_keys)
        validate_role_permissions(final_keys)

        RolePermission.objects.filter(role=target_role).delete()
        perms = Permission.objects.filter(key__in=final_keys)
        RolePermission.objects.bulk_create([
            RolePermission(
                role=target_role, permission=perm, granted=True,
                granted_by=reviewer, granted_at=timezone.now(),
            )
            for perm in perms
        ])

        target_role.bump_version()
        target_role.save(update_fields=['version', 'updated_at'])

        obj.mark_approved(reviewer=reviewer, notes=notes)
        obj.save(update_fields=['status', 'reviewer', 'reviewer_notes', 'decided_at', 'updated_at'])


def apply_platform_role_change_request(obj: PlatformRoleChangeRequest, reviewer, notes: str = ""):
    """
    Atomically apply an approved platform role change request.
    """
    with transaction.atomic():
        target_role = obj.target_role

        current_keys = set(
            PlatformRolePermission.objects.filter(role=target_role, granted=True)
            .values_list('permission_id', flat=True)
        )

        for item in obj.delta_items.select_related('permission').all():
            if item.operation == PlatformRoleChangeDeltaItem.Operation.ADD:
                current_keys.add(item.permission_id)
            elif item.operation == PlatformRoleChangeDeltaItem.Operation.REMOVE:
                current_keys.discard(item.permission_id)

        final_keys = sorted(current_keys)
        validate_role_permissions(final_keys)

        PlatformRolePermission.objects.filter(role=target_role).delete()
        perms = Permission.objects.filter(key__in=final_keys)
        PlatformRolePermission.objects.bulk_create([
            PlatformRolePermission(
                role=target_role, permission=perm, granted=True,
                granted_by=reviewer, granted_at=timezone.now(),
            )
            for perm in perms
        ])

        target_role.bump_version()
        target_role.save(update_fields=['version', 'updated_at'])

        obj.mark_approved(reviewer=reviewer, notes=notes)
        obj.save(update_fields=['status', 'reviewer', 'reviewer_notes', 'decided_at', 'updated_at'])
