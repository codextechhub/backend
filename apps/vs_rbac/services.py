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

import logging

from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from rest_framework.exceptions import PermissionDenied, ValidationError

from vs_audit.models import AuditModuleKey, AuditActionType
from vs_rbac.audit import record_rbac_audit as emit_audit_event

from .models import (
    Permission,
    platform_only_keys,
    PrebuiltRolePermission,
    PrebuiltRoleTemplate,
    TenantRoleChangeDeltaItem,
    TenantRoleChangeRequest,
    TenantRoleGroup,
    TenantRolePermission,
    TenantRoleTemplate,
    TenantUserRoleAssignment,
    tenant_is_platform,
)
from .exceptions import PrebuiltRoleMissing
from .validators import validate_role_permissions

logger = logging.getLogger(__name__)


SUPER_ADMIN_ROLE_KEY = "xvs_super_admin"
PLATFORM_ADMIN_ROLE_KEY = "xvs_platform_admin"


_UNSET = object()


def _role_access_snapshot(*, permission_keys, denied_permission_keys, group_ids):
    """Build the stable configuration snapshot stored in the durable audit."""
    from .validators import group_permission_keys

    direct_keys = set(permission_keys)
    denied_keys = set(denied_permission_keys)
    attached_group_ids = set(group_ids)
    return {
        "direct_permission_keys": sorted(direct_keys),
        "denied_permission_keys": sorted(denied_keys),
        "group_ids": sorted(str(group_id) for group_id in attached_group_ids),
        "combined_permission_keys": sorted(
            (direct_keys | group_permission_keys(attached_group_ids)) - denied_keys
        ),
    }


@transaction.atomic
def set_role_access(
    *,
    role,
    actor,
    reason: str,
    permission_keys=_UNSET,
    denied_permission_keys=_UNSET,
    group_ids=_UNSET,
    approval_reference=None,
    allow_restricted=False,
    source="direct_edit",
    audit_metadata=None,
):
    """Replace a role's access configuration and audit it as one transaction.

    ``permission_keys``, ``denied_permission_keys`` and ``group_ids`` are
    independently optional. An omitted dimension is preserved, while an
    explicitly empty iterable clears it. For backward compatibility, supplying
    grants without an explicit deny set clears the denies. The durable audit
    write is inside the same transaction, so an audit failure rolls the access
    change back.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "A reason is required for a role access change."})

    locked_role = (
        TenantRoleTemplate.objects.select_for_update()
        .select_related("tenant")
        .get(pk=role.pk)
    )

    permission_rows = {
        row.permission_id: row
        for row in TenantRolePermission.objects.filter(role=locked_role)
    }
    current_permission_keys = {
        key for key, row in permission_rows.items() if row.granted
    }
    current_denied_permission_keys = {
        key for key, row in permission_rows.items() if not row.granted
    }
    current_group_ids = set(
        TenantRoleGroup.objects.filter(role=locked_role).values_list(
            "group_id", flat=True,
        )
    )
    desired_permission_keys = (
        current_permission_keys
        if permission_keys is _UNSET
        else {key for key in permission_keys if key}
    )
    if denied_permission_keys is _UNSET:
        desired_denied_permission_keys = (
            current_denied_permission_keys if permission_keys is _UNSET else set()
        )
    else:
        desired_denied_permission_keys = {
            key for key in denied_permission_keys if key
        }
    conflicting_permission_keys = (
        desired_permission_keys & desired_denied_permission_keys
    )
    if conflicting_permission_keys:
        raise ValidationError({
            "permission_keys": (
                "Permissions cannot be both granted and denied: "
                f"{', '.join(sorted(conflicting_permission_keys))}."
            ),
        })
    desired_group_ids = (
        current_group_ids
        if group_ids is _UNSET
        else {group_id for group_id in group_ids if group_id}
    )

    desired_direct_keys = desired_permission_keys | desired_denied_permission_keys
    existing_permission_keys = set(
        Permission.objects.filter(key__in=desired_direct_keys).values_list(
            "key", flat=True,
        )
    )
    missing_permission_keys = desired_direct_keys - existing_permission_keys
    if missing_permission_keys:
        raise ValidationError({
            "permission_keys": (
                "Unknown permission keys: "
                f"{', '.join(sorted(missing_permission_keys))}"
            ),
        })

    from .models import PermissionGroup

    existing_group_ids = set(
        PermissionGroup.objects.filter(id__in=desired_group_ids).values_list(
            "id", flat=True,
        )
    )
    missing_group_ids = desired_group_ids - existing_group_ids
    if missing_group_ids:
        raise ValidationError({
            "group_ids": (
                "Unknown permission group ids: "
                f"{', '.join(sorted(str(group_id) for group_id in missing_group_ids))}"
            ),
        })

    before = _role_access_snapshot(
        permission_keys=current_permission_keys,
        denied_permission_keys=current_denied_permission_keys,
        group_ids=current_group_ids,
    )
    after = _role_access_snapshot(
        permission_keys=desired_permission_keys,
        denied_permission_keys=desired_denied_permission_keys,
        group_ids=desired_group_ids,
    )

    if not allow_restricted:
        from .validators import restricted_permission_keys

        added_restricted = restricted_permission_keys(
            set(after["combined_permission_keys"])
            - set(before["combined_permission_keys"])
        )
        if added_restricted:
            raise ValidationError({
                "permission_keys": (
                    "Restricted permissions require an approved role change request: "
                    f"{', '.join(sorted(added_restricted))}."
                ),
            })

    validate_role_permissions(permission_keys=after["combined_permission_keys"])

    TenantRolePermission.objects.filter(role=locked_role).exclude(
        permission_id__in=desired_direct_keys,
    ).delete()

    rows_to_reactivate = [
        row
        for key, row in permission_rows.items()
        if key in desired_permission_keys and not row.granted
    ]
    if rows_to_reactivate:
        now = timezone.now()
        for row in rows_to_reactivate:
            row.granted = True
            row.granted_by = actor
            row.granted_at = now
            row.updated_at = now
        TenantRolePermission.objects.bulk_update(
            rows_to_reactivate,
            ["granted", "granted_by", "granted_at", "updated_at"],
        )

    rows_to_deny = [
        row
        for key, row in permission_rows.items()
        if key in desired_denied_permission_keys and row.granted
    ]
    if rows_to_deny:
        now = timezone.now()
        for row in rows_to_deny:
            row.granted = False
            row.granted_by = actor
            row.granted_at = now
            row.updated_at = now
        TenantRolePermission.objects.bulk_update(
            rows_to_deny,
            ["granted", "granted_by", "granted_at", "updated_at"],
        )

    TenantRolePermission.objects.bulk_create([
        TenantRolePermission(
            role=locked_role,
            permission_id=key,
            granted=key in desired_permission_keys,
            granted_by=actor,
        )
        for key in desired_direct_keys - set(permission_rows)
    ])

    TenantRoleGroup.objects.filter(role=locked_role).exclude(
        group_id__in=desired_group_ids,
    ).delete()
    TenantRoleGroup.objects.bulk_create([
        TenantRoleGroup(role=locked_role, group_id=group_id, attached_by=actor)
        for group_id in desired_group_ids - current_group_ids
    ])

    access_changed = before != after
    if access_changed:
        locked_role.version = (locked_role.version or 1) + 1
        locked_role.save(update_fields=["version", "updated_at"])

    metadata = {
        "tenant_id": str(locked_role.tenant_id),
        "reason": reason,
        "approval_reference": (
            str(approval_reference) if approval_reference is not None else None
        ),
        "source": source,
        "access_changed": access_changed,
    }
    metadata.update(audit_metadata or {})
    emit_audit_event(
        module_key=AuditModuleKey.RBAC,
        action_type=AuditActionType.PERMISSION_CHANGED,
        actor_user=actor,
        entity_type="TenantRoleTemplate",
        entity_id=str(locked_role.pk),
        entity_label=locked_role.name,
        summary=f"Role '{locked_role.name}' access configuration updated",
        before_data=before,
        diff_data={
            field: {"before": before[field], "after": after[field]}
            for field in before
        },
        metadata=metadata,
    )
    return locked_role


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


def branch_role_name(library_name: str, *, branch, siblings: int) -> str:
    """What a branch's own copy of a library role is called.

    The NAME is what a person reads, and it names the branch only where the
    branch tells them something. A school with one site reading "Branch Admin -
    Main Branch" on its roles screen is told "Branch" twice and nothing else,
    so where a school has one branch the dimension recedes, the same way a
    switcher with one entry or a filter with one option does. Where it has
    several, the name says which, because there it changes meaning.

    ``siblings`` is how many branches the tenant has, counted by the caller
    that already has to know.
    """
    if siblings <= 1:
        return library_name
    return f"{library_name} - {branch.name}"


def missing_prebuilt_keys(keys) -> list:
    """Which of *keys* the prebuilt library cannot provision, in the order given.

    Asked before provisioning rather than discovered one role at a time, so a
    caller refusing over an incomplete library can name every missing role at
    once and an operator seeds them in one go.
    """
    live = set(
        PrebuiltRoleTemplate.objects
        .filter(key__in=list(keys), is_active=True)
        .values_list("key", flat=True)
    )
    return [key for key in keys if key not in live]


@transaction.atomic
def sync_branch_role_names(branch) -> list:
    """Bring a branch's own role copies back in step with the branch's name.

    The name is stored and rewritten here rather than composed on read.
    ``TenantRoleTemplate.name`` is unique per tenant, is what the roles screen,
    every grant row, the approver preview and the school's own search read, and
    is what a school renames when it wants its own wording; deriving it at read
    time would leave the stored value wrong, its uniqueness meaningless, and
    every reader holding a second copy of the rule. So it is written in the
    same transaction as the rename that made it stale.

    **Only a name this code composed is rewritten.** A school that renamed
    "Branch Admin - Ikeja" to "Ikeja Head" has named its own role, and renaming
    the branch must not take that away.

    A name another role in the tenant already holds is left alone: the column
    is unique per tenant, and two branches sharing a name is an arrangement for
    the school to settle rather than a reason to fail its rename.

    Returns the roles whose names changed.
    """
    from vs_tenants.models import Branch

    siblings = Branch.all_objects.filter(tenant_id=branch.tenant_id).count()
    suffix = f"-{branch.pk}"
    renamed = []
    for role in TenantRoleTemplate.objects.filter(
        tenant_id=branch.tenant_id, branch_id=branch.pk, is_system_role=True,
    ):
        if not role.key.endswith(suffix):
            continue
        library = PrebuiltRoleTemplate.objects.filter(
            key=role.key[: -len(suffix)],
        ).first()
        if library is None:
            continue
        composed_here = (
            role.name == library.name
            or role.name.startswith(f"{library.name} - ")
        )
        if not composed_here:
            continue
        wanted = branch_role_name(library.name, branch=branch, siblings=siblings)
        if wanted == role.name:
            continue
        if TenantRoleTemplate.objects.filter(
            tenant_id=branch.tenant_id, name=wanted,
        ).exclude(pk=role.pk).exists():
            logger.warning(
                "sync_branch_role_names: tenant %s already has a role named "
                "%r, so %r keeps its name after branch %s was renamed",
                branch.tenant_id, wanted, role.name, branch.pk,
            )
            continue
        role.name = wanted
        role.save(update_fields=["name"])
        renamed.append(role)
    return renamed


# Provision a locked tenant role from Vision's prebuilt role library.
@transaction.atomic
def provision_role_from_prebuilt(*, tenant, branch=None, prebuilt_key: str,
                                 created_by=None, required: bool = True):
    """
    Get or create a TenantRoleTemplate from a PrebuiltRoleTemplate, copying its
    default permissions into the new role if it is freshly created.

    ``tenant`` is the owning tenant (derive from ``school.tenant`` at call
    sites).

    A key the library cannot provision raises :class:`PrebuiltRoleMissing`. It
    answered ``None`` instead, which reads exactly like success at any call site
    that does not check, and that is how a school was created holding two of the
    five roles the product ships with nobody being told. ``required=False`` is
    for a caller that genuinely treats the role as optional and says so.

    A library default the target tenant may not hold is **dropped, not
    refused**, and logged. The caller here never chose these keys - creating a
    school asks for Finance Admin, not for a list of permissions - so refusing
    the copy takes the whole school down over a key that would confer nothing
    anyway: the evaluator filters a non-tenant key out of every effective set
    outside the platform. That is not a licence to leave the library wrong;
    ``audit_permission_scope`` reports any such row, and
    :func:`vs_rbac.scope_withdrawal.withdraw_from_tenants` takes it back.
    """
    from django.contrib.auth import get_user_model

    from vs_tenants.models import Branch

    User = get_user_model()
    if not isinstance(created_by, User):
        created_by = None

    prebuilt = PrebuiltRoleTemplate.objects.filter(key=prebuilt_key, is_active=True).first()
    if not prebuilt:
        if required:
            raise PrebuiltRoleMissing([prebuilt_key])
        return None

    # Branch-scoped roles get a per-branch KEY so several branches can each carry
    # their own copy without violating per-tenant uniqueness. The key is
    # machinery and always carries the branch; the name is what a person reads,
    # and :func:`branch_role_name` settles it.
    if branch is None:
        key = prebuilt.key
        name = prebuilt.name
    else:
        key = f"{prebuilt.key}-{branch.pk}"
        siblings = Branch.all_objects.filter(tenant=tenant).count()
        name = branch_role_name(prebuilt.name, branch=branch, siblings=siblings)

    role, created = TenantRoleTemplate.objects.get_or_create(
        tenant=tenant,
        key=key,
        defaults={
            "branch": branch,
            "name": name,
            "description": prebuilt.description,
            "is_system_role": True,
            "is_locked": False,
            "created_by": created_by,
        },
    )

    # A school that has just gained its second branch has a role named for no
    # branch sitting beside one named for a branch, and no way to tell which
    # site the first one runs. The moment the dimension starts to mean
    # something, every sibling says which branch it is for.
    if branch is not None and siblings > 1:
        for sibling in TenantRoleTemplate.objects.filter(
            tenant=tenant, key__startswith=f"{prebuilt.key}-",
            name=prebuilt.name, branch__isnull=False,
        ).exclude(pk=role.pk).select_related("branch"):
            sibling.name = branch_role_name(
                prebuilt.name, branch=sibling.branch, siblings=siblings,
            )
            sibling.save(update_fields=["name"])

    if created:
        permission_keys = list(PrebuiltRolePermission.objects.filter(
            prebuilt_role=prebuilt
        ).values_list("permission_id", flat=True))
        if not tenant_is_platform(tenant):
            refused = platform_only_keys(permission_keys)
            if refused:
                logger.warning(
                    "Prebuilt role '%s' carries %s, which %s may not hold. "
                    "Provisioned without them; withdraw them from the library.",
                    prebuilt.key, ", ".join(sorted(refused)), tenant,
                )
                permission_keys = [k for k in permission_keys if k not in refused]
        role = set_role_access(
            role=role,
            actor=created_by,
            reason=f"Provisioned from prebuilt role '{prebuilt.key}'.",
            permission_keys=permission_keys,
            group_ids=[],
            allow_restricted=True,
            source="prebuilt_role_provisioning",
        )

    return role


# Raise a role change request and start its approval ladder.
@transaction.atomic
def raise_role_change_request(*, tenant, requested_by, target_role, justification,
                              deltas) -> TenantRoleChangeRequest:
    """Create a role change request and submit it for approval, as one act.

    Raising a request and starting its ladder are not two steps a caller may do
    separately. A request with no workflow instance is one nobody can decide:
    the roles screen lists it as waiting and the engine has never heard of it,
    and no amount of pressing Approve will move it. So they are written in one
    transaction, and a ladder that cannot be resolved refuses the whole thing
    rather than leaving the row behind.

    ``deltas`` is an iterable of ``{"permission_key": str, "operation": "ADD" |
    "REMOVE"}``. Submission happens after they are written because the handler
    validates against them: a request naming no permission is not a request.

    Raises whatever the engine raises - :class:`TemplateNotFoundError` when the
    document type has no ladder, :class:`ApprovalNotConfiguredError` when the
    one it finds has no steps.
    """
    from vs_workflow.services.submission import submit_for_approval

    request = TenantRoleChangeRequest.objects.create(
        tenant=tenant,
        requested_by=requested_by,
        target_role=target_role,
        justification=justification,
    )
    for delta in deltas:
        TenantRoleChangeDeltaItem.objects.create(
            request=request,
            permission_id=delta["permission_key"],
            operation=delta.get("operation", TenantRoleChangeDeltaItem.Operation.ADD),
        )
    submit_for_approval(request, requested_by)
    return request


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

    Who was allowed to decide this is settled before the call: the workflow
    engine resolved the approvers, recorded their votes and only then fired the
    callback that lands here. So this applies the delta and does not re-litigate
    the decision - see :mod:`vs_rbac.workflow_handlers`.

    Raises if validation fails or apply fails.
    """
    with transaction.atomic():
        obj = (
            TenantRoleChangeRequest.objects.select_for_update()
            .select_related("target_role", "requested_by", "tenant")
            .get(pk=obj.pk)
        )
        if obj.status != TenantRoleChangeRequest.Status.PENDING:
            raise ValidationError(f"Request already decided ({obj.status}).")

        # A school may approve its own request, and the audit says so.
        #
        # Two people deciding a permission grant is the better arrangement and
        # is not one most schools can staff: a school whose only holder of
        # ``school.roles.approve`` is the head teacher who raised the request
        # cannot complete it at all, and a rule that cannot be followed is
        # followed by asking CodeX to reach into the tenant instead - which is
        # a worse position than the one it was protecting against. So it is
        # allowed and recorded: a distinct audit source and an explicit flag,
        # both filterable, so "who approved their own grant" is a question the
        # log answers rather than one it hides.
        self_approved = obj.requested_by_id == getattr(reviewer, "pk", None)

        target_role = TenantRoleTemplate.objects.select_for_update().get(
            pk=obj.target_role_id,
        )

        # The role's grants as they stand, to replay the delta against.
        # ``set_role_access`` writes its own before/after into the durable audit.
        current_keys = set(
            TenantRolePermission.objects.filter(
                role=target_role, granted=True
            ).values_list("permission_id", flat=True)
        )

        # Replay the requested delta in memory before replacing stored grants.
        delta_items = list(obj.delta_items.select_related("permission").all())
        for item in delta_items:
            if item.operation == TenantRoleChangeDeltaItem.Operation.ADD:
                current_keys.add(item.permission_id)
            elif item.operation == TenantRoleChangeDeltaItem.Operation.REMOVE:
                current_keys.discard(item.permission_id)

        # Include group-derived permissions when the shared service validates
        # the final set.
        final_keys = sorted(current_keys)
        attached_group_ids = list(
            TenantRoleGroup.objects.filter(role=target_role).values_list("group_id", flat=True)
        )
        target_role = set_role_access(
            role=target_role,
            actor=reviewer,
            reason=obj.justification,
            permission_keys=final_keys,
            group_ids=attached_group_ids,
            approval_reference=obj.pk,
            allow_restricted=True,
            source=(
                "self_approved_change_request" if self_approved
                else "approved_change_request"
            ),
            audit_metadata={
                "change_request_id": str(obj.pk),
                "reviewer_notes": notes,
                "self_approved": self_approved,
                "requested_by_id": obj.requested_by_id,
                "reviewer_id": getattr(reviewer, "pk", None),
            },
        )

        # Mark the approval only after validation and grant replacement succeed.
        obj.mark_approved(reviewer=reviewer, notes=notes)
        obj.save(update_fields=[
            "status", "reviewer", "reviewer_notes", "decided_at", "updated_at",
        ])


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

    if not getattr(to_user, "is_platform_user", False):
        raise ValueError("The new super admin must be a Vision Staff member.")

    try:
        codex = Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)
    except Tenant.DoesNotExist as exc:
        raise ValueError("Codex platform tenant not found.") from exc

    # Guard the transfer authority with the active super-admin assignment itself.
    # A queryset rather than ``.first()``: the split unique constraints allow a
    # whole-tenant grant and a branch-pinned one of the same role to coexist, and
    # revoking only the first one found would demote the outgoing holder on
    # paper while ``is_vision_super_admin`` - a branch-blind ``.exists()`` -
    # still answered yes for the grant left behind, leaving two super admins.
    outgoing_grants = TenantUserRoleAssignment.objects.filter(
        tenant=codex,
        user=from_user,
        role__key=SUPER_ADMIN_ROLE_KEY,
        role__tenant=codex,
        assignment_status=TenantUserRoleAssignment.AssignmentStatus.ACTIVE,
    )
    if not outgoing_grants.exists():
        raise ValueError("You do not hold the Vision Super Admin role.")

    try:
        super_admin_role = TenantRoleTemplate.objects.get(tenant=codex, key=SUPER_ADMIN_ROLE_KEY)
        platform_admin_role = TenantRoleTemplate.objects.get(tenant=codex, key=PLATFORM_ADMIN_ROLE_KEY)
    except TenantRoleTemplate.DoesNotExist as exc:
        raise ValueError(f"Required platform role not found: {exc}") from exc

    now = timezone.now()

    # Revoke every old super-admin assignment before issuing replacements, the
    # same way the incoming holder's roles are cleared below.
    for active_assignment in outgoing_grants:
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
    permission_keys = list(
        suggestion.default_permissions.values_list("permission_id", flat=True)
    )
    role = set_role_access(
        role=role,
        actor=created_by,
        reason=f"Created from role suggestion '{suggestion.key}'.",
        permission_keys=permission_keys,
        group_ids=[],
        allow_restricted=True,
        source="role_suggestion",
    )

    return role
