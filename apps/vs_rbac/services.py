"""
Service layer for tenant RBAC role change approval workflows.

Handles:
- Tenant role change request approval and application (unified school + platform)
- Dependency validation before applying changes
- Prebuilt/suggested role provisioning onto the tenant tables
- Super-admin transfer on the codex platform tenant
- Audit trail generation
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from vs_audit.models import AuditModuleKey, AuditActionType
from vs_rbac.audit import record_rbac_audit as emit_audit_event

from .models import (
    Permission,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRoleChangeDeltaItem,
    TenantRoleChangeRequest,
    TenantRoleGroup,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
)
from .validators import validate_role_permissions


SUPER_ADMIN_ROLE_KEY = "xvs_super_admin"
PLATFORM_ADMIN_ROLE_KEY = "xvs_platform_admin"


# Build a slug key unique within a tenant (roles are addressed by key).
def _unique_tenant_role_key(tenant, name, exclude_pk=None) -> str:
    base = slugify(name) or "role"
    slug = base
    n = 1
    while True:
        qs = TenantRoleTemplate.objects.filter(tenant=tenant, key=slug)
        if exclude_pk is not None:
            qs = qs.exclude(pk=exclude_pk)
        if not qs.exists():
            return slug
        slug = f"{base}-{n}"
        n += 1


# Provision a locked tenant role from Vision's prebuilt role library.
def provision_role_from_prebuilt(*, tenant, branch=None, prebuilt_key: str, created_by=None):
    """
    Get or create a TenantRoleTemplate from a PrebuiltRoleTemplate, copying its
    default permissions into the new role if it is freshly created.

    ``tenant`` is the owning tenant (derive from ``school.tenant`` at call
    sites). Returns the TenantRoleTemplate, or None if the prebuilt key is not
    found.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    if not isinstance(created_by, User):
        created_by = None

    prebuilt = PrebuiltRoleTemplate.objects.filter(key=prebuilt_key, is_active=True).first()
    if not prebuilt:
        return None

    # Branch-scoped roles get a per-branch key/name so several branches can each
    # carry their own copy without violating the per-tenant key/name uniqueness.
    if branch is None:
        key = prebuilt.key
        name = prebuilt.name
    else:
        key = f"{prebuilt.key}-{branch.pk}"
        name = f"{prebuilt.name} — {branch.name}"

    role, created = TenantRoleTemplate.objects.get_or_create(
        tenant=tenant,
        key=key,
        defaults={
            "branch": branch,
            "name": name,
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
        TenantRolePermission.objects.bulk_create(
            [
                TenantRolePermission(
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


# Apply an approved tenant role permission-change request.
def apply_role_change_request(obj: TenantRoleChangeRequest, reviewer, notes: str = ""):
    """
    Apply an approved tenant role change request.

    This atomically:
    1. Validates dependencies (against the flattened effective set)
    2. Applies ADD/REMOVE operations
    3. Bumps role version
    4. Marks request as approved
    5. Creates audit trail

    Raises if validation fails or apply fails.
    """
    with transaction.atomic():
        target_role = obj.target_role

        # Snapshot current grants so the durable audit shows the exact before/after set.
        current_keys = set(
            TenantRolePermission.objects.filter(
                role=target_role, granted=True
            ).values_list("permission_id", flat=True)
        )
        before_keys = sorted(current_keys)

        # Replay the requested delta in memory before replacing stored grants.
        delta_items = obj.delta_items.select_related("permission").all()
        for item in delta_items:
            if item.operation == TenantRoleChangeDeltaItem.Operation.ADD:
                current_keys.add(item.permission_id)
            elif item.operation == TenantRoleChangeDeltaItem.Operation.REMOVE:
                current_keys.discard(item.permission_id)

        # Validate final permission set — include group-derived permissions so
        # dependency checks pass for permissions provided via groups.
        final_keys = sorted(current_keys)
        attached_group_ids = list(
            TenantRoleGroup.objects.filter(role=target_role).values_list("group_id", flat=True)
        )
        validate_role_permissions(
            permission_keys=final_keys,
            group_ids=attached_group_ids,
        )  # Raises ValidationError if invalid

        # Replace direct grants atomically so removed permissions cannot linger.
        TenantRolePermission.objects.filter(role=target_role).delete()

        perms = Permission.objects.filter(key__in=final_keys)
        TenantRolePermission.objects.bulk_create([
            TenantRolePermission(
                role=target_role,
                permission=perm,
                granted=True,
                granted_by=reviewer,
                granted_at=timezone.now(),
            )
            for perm in perms
        ])

        # Version bump invalidates downstream effective-permission caches.
        target_role.version = (target_role.version or 1) + 1
        target_role.save(update_fields=["version", "updated_at"])

        # Mark the approval only after validation and grant replacement succeed.
        obj.mark_approved(reviewer=reviewer, notes=notes)
        obj.save(update_fields=[
            "status", "reviewer", "reviewer_notes", "decided_at", "updated_at",
        ])

        emit_audit_event(
            module_key=AuditModuleKey.RBAC,
            action_type=AuditActionType.PERMISSION_CHANGED,
            actor_user=reviewer,
            entity_type="TenantRoleTemplate",
            entity_id=str(target_role.pk),
            entity_label=getattr(target_role, "name", str(target_role.pk)),
            summary=f"Role '{getattr(target_role, 'name', target_role.pk)}' permissions updated via approved change request",
            before_data={"permission_keys": before_keys},
            diff_data={"permission_keys": {"before": before_keys, "after": final_keys}},
            metadata={
                "change_request_id": str(obj.pk),
                "tenant_id": str(target_role.tenant_id),
                "reviewer_notes": notes,
            },
        )


# Transfer the single Vision super-admin assignment and demote the previous holder.
@transaction.atomic
def transfer_super_admin(from_user, to_user):
    """
    Transfer the Vision Super Admin role from `from_user` to `to_user` on the
    codex platform tenant.

    - `from_user` must currently hold the active xvs_super_admin assignment.
    - `to_user` must be Vision (CX) staff and different from `from_user`.
    - After transfer, `from_user` is demoted to xvs_platform_admin.
    - Any existing active tenant role on `to_user` is revoked first.
    - Both users' `is_superuser` flags are updated accordingly.

    Raises ValueError on any validation failure.
    """
    from django.conf import settings
    from django.apps import apps
    from vs_tenants.models import Tenant

    UserModel = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    if from_user.pk == to_user.pk:
        raise ValueError("Cannot transfer super admin to yourself.")

    if getattr(to_user, "user_type", None) != "CX_STAFF":
        raise ValueError("The new super admin must be a Vision Staff member.")

    try:
        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
    except Tenant.DoesNotExist as exc:
        raise ValueError("Codex platform tenant not found.") from exc

    # Guard the transfer authority with the active super-admin assignment itself.
    active_assignment = TenantUserRoleAssignment.objects.filter(
        tenant=codex,
        user=from_user,
        role__key=SUPER_ADMIN_ROLE_KEY,
        role__tenant=codex,
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
    ).first()
    if not active_assignment:
        raise ValueError("You do not hold the Vision Super Admin role.")

    try:
        super_admin_role = TenantRoleTemplate.objects.get(tenant=codex, key=SUPER_ADMIN_ROLE_KEY)
        platform_admin_role = TenantRoleTemplate.objects.get(tenant=codex, key=PLATFORM_ADMIN_ROLE_KEY)
    except TenantRoleTemplate.DoesNotExist as exc:
        raise ValueError(f"Required platform role not found: {exc}") from exc

    now = timezone.now()

    # Revoke the old super-admin assignment before issuing replacements.
    active_assignment.revoke(by_user=from_user, reason="Super admin role transferred to another user.")
    active_assignment.save(update_fields=["assignment_status", "revoked_at", "revoked_by", "reason_note", "updated_at"])

    # Clear existing tenant roles so the new holder has exactly the super-admin role.
    TenantUserRoleAssignment.objects.filter(
        tenant=codex,
        user=to_user,
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
    ).update(
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.REVOKED,
        revoked_at=now,
        revoked_by=from_user,
        reason_note="Role revoked as part of super admin transfer.",
    )

    # Keep the previous holder in platform administration after demotion.
    TenantUserRoleAssignment.objects.create(
        tenant=codex,
        user=from_user,
        role=platform_admin_role,
        assigned_by=from_user,
    )

    # Grant the sole super-admin role to the incoming Vision staff user.
    TenantUserRoleAssignment.objects.create(
        tenant=codex,
        user=to_user,
        role=super_admin_role,
        assigned_by=from_user,
    )

    # Keep Django's coarse superuser flag aligned with RBAC ownership.
    UserModel.objects.filter(pk=from_user.pk).update(is_superuser=False)
    UserModel.objects.filter(pk=to_user.pk).update(is_superuser=True)

    emit_audit_event(
        actor_user=from_user,
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.ROLE_CHANGED,
        entity_type="TenantUserRoleAssignment",
        entity_id=str(to_user.pk),
        entity_label=getattr(to_user, "email", str(to_user.pk)),
        summary=f"Super admin role transferred from {from_user.email} to {to_user.email}",
        metadata={"from_user_id": str(from_user.pk), "to_user_id": str(to_user.pk)},
    )


@transaction.atomic
# Create a tenant-local role from a prebuilt suggestion.
def create_role_from_suggestion(suggestion_key: str, tenant, created_by) -> TenantRoleTemplate:
    """
    Create a TenantRoleTemplate for a tenant based on a PrebuiltRoleTemplate.

    Looks up the suggestion by key, creates a TenantRoleTemplate scoped to the
    tenant with the suggestion's name, then bulk-copies the default permissions.
    The PrebuiltRoleTemplate is never modified.

    Raises PrebuiltRoleTemplate.DoesNotExist if the key is not found.
    Raises ValueError if the tenant already has a role with this name.
    """
    suggestion = PrebuiltRoleTemplate.objects.get(key=suggestion_key, is_active=True)

    if TenantRoleTemplate.objects.filter(tenant=tenant, name__iexact=suggestion.name).exists():
        raise ValueError(
            f'This tenant already has a role named "{suggestion.name}". '
            f'Rename the existing role before creating another with this name.'
        )

    role = TenantRoleTemplate.objects.create(
        tenant=tenant,
        key=_unique_tenant_role_key(tenant, suggestion.name),
        name=suggestion.name,
        created_by=created_by,
    )

    # Copy grants, not the template row, so the tenant can own later role edits.
    default_permissions = suggestion.default_permissions.select_related("permission").all()
    TenantRolePermission.objects.bulk_create([
        TenantRolePermission(role=role, permission=dp.permission)
        for dp in default_permissions
    ], ignore_conflicts=True)

    return role
