"""Reusable helpers for app-level permission seeds.

The finance, procurement, and payments apps each register their own
``module.resource.action`` permission keys and grant them to the platform
admin roles. Rather than copy the same module → resource → permission → grant
loop into every management command, that logic lives here once.

A seed command supplies a compact spec::

    from vs_rbac.seed_utils import register_app_permissions

    register_app_permissions(
        module_name="payments",
        module_description="Payment gateway collections and payouts.",
        resources=[
            ("collection", "collections", [
                ("view",   "NORMAL"),
                ("create", "CRITICAL"),
            ]),
            ...
        ],
        role_ids=["xvs_super_admin", "xvs_platform_admin"],
        stdout=self.stdout,
        style=self.style,
    )

``sensitivity`` is one of ``NORMAL`` / ``SENSITIVE`` / ``CRITICAL``;
``is_restricted`` is derived as ``sensitivity != "NORMAL"`` so restricted
keys surface in the approval/audit queues that key off those flags.

Everything is idempotent (``get_or_create`` throughout) so the seeds are safe
to re-run on any environment.
"""
from __future__ import annotations

from django.db import transaction

# sensitivity → whether the permission must flow through approvals / audit
_RESTRICTED = {"SENSITIVE", "CRITICAL"}


def _ensure_actions(action_names, stdout=None):
    """Defensively create any PermissionAction the spec relies on.

    ``seed_actions`` is the canonical source of action verbs and normally runs
    first (it owns the rich descriptions). This is a safety net so an app seed
    still works if invoked standalone — get_or_create never overwrites an
    existing row, so canonical descriptions always win.
    """
    from vs_rbac.models import PermissionAction

    for name in sorted(action_names):
        _, created = PermissionAction.objects.get_or_create(
            name=name,
            defaults={
                "description": f"Auto-registered action verb '{name}'.",
                "is_active": True,
            },
        )
        if created and stdout is not None:
            stdout.write(f"  + action '{name}' (auto-registered — run seed_actions for full description)")


@transaction.atomic
def register_app_permissions(
    *,
    module_name,
    module_description,
    resources,
    role_ids,
    stdout=None,
    style=None,
):
    """Register a module's permission keys and grant them to platform roles.

    Args:
        module_name: PermissionModule slug, e.g. ``"finance"``.
        module_description: Human description for the module bucket.
        resources: list of ``(resource_name, resource_label, actions)`` where
            ``actions`` is a list of ``(action_name, sensitivity)``. The
            permission description is auto-built from the verb + label.
        role_ids: PlatformRoleTemplate ids to grant every key to.
        stdout / style: optional management-command writers for progress output.

    Returns:
        (created_perms, total_perms, granted_links)
    """
    from vs_rbac.models import (
        Permission,
        PermissionAction,
        PermissionModule,
        PermissionResource,
        PlatformRolePermission,
        PlatformRoleTemplate,
    )

    def _say(msg, kind=None):
        if stdout is None:
            return
        if style is not None and kind is not None:
            stdout.write(getattr(style, kind)(msg))
        else:
            stdout.write(msg)

    # Collect every action verb the spec needs and make sure they exist.
    needed_actions = {a for _, _, acts in resources for a, _ in acts}
    _ensure_actions(needed_actions, stdout=stdout)

    module, created = PermissionModule.objects.get_or_create(
        name=module_name,
        defaults={"description": module_description, "is_active": True},
    )
    _say(f"  module '{module_name}' " + ("created" if created else "exists"))

    created_perms = 0
    all_perms = []

    for resource_name, resource_label, actions in resources:
        resource, _ = PermissionResource.objects.get_or_create(
            module=module,
            name=resource_name,
            defaults={
                "description": f"{resource_label.capitalize()} ({module_name}).",
                "is_active": True,
            },
        )

        for action_name, sensitivity in actions:
            action = PermissionAction.objects.get(name=action_name)
            expected_key = f"{module_name}.{resource_name}.{action_name}"
            verb = action_name.replace("_", " ")
            description = f"{verb.capitalize()} {resource_label}."

            perm = Permission.objects.filter(key=expected_key).first()
            if perm is None:
                perm = Permission(
                    module=module,
                    resource=resource,
                    action=action,
                    description=description,
                    sensitivity_level=sensitivity,
                    is_restricted=sensitivity in _RESTRICTED,
                    is_active=True,
                )
                perm.save()
                created_perms += 1
                _say(f"  + {perm.key}  [{sensitivity}]")
            all_perms.append(perm)

    # ── Grant every key to the platform admin roles ───────────────────────────
    granted_links = 0
    for role_id in role_ids:
        try:
            role = PlatformRoleTemplate.objects.get(id=role_id)
        except PlatformRoleTemplate.DoesNotExist:
            _say(
                f"  ⚠  role '{role_id}' not found — run create_superuser first; grants skipped.",
                kind="WARNING",
            )
            continue

        granted = 0
        for perm in all_perms:
            _, link_created = PlatformRolePermission.objects.get_or_create(
                role=role,
                permission=perm,
                defaults={"granted": True, "granted_by": None},
            )
            if link_created:
                granted += 1
                granted_links += 1
        _say(
            f"  {role_id}: granted {granted} new key(s)." if granted
            else f"  {role_id}: all keys already assigned."
        )

    _say(
        f"\n  Done. {created_perms} new permission(s), {len(all_perms)} total "
        f"'{module_name}' keys registered.\n",
        kind="SUCCESS",
    )
    return created_perms, len(all_perms), granted_links
