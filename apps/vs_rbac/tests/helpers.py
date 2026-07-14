"""
Shared test helpers and fixture factories for vs_rbac tests.

Role / assignment / change-request factories single-write the canonical
**tenant** RBAC tables (``TenantRoleTemplate`` / ``TenantRolePermission`` /
``TenantUserRoleAssignment`` / ``TenantRoleChangeRequest``). Function names are
preserved for existing callers, but they now return the tenant objects.
"""
import itertools
from django.utils.text import slugify
from vs_schools.models import School, Branch
from vs_user.models import User
from vs_tenants.models import Tenant
from vs_rbac.models import (
    Permission,
    PermissionDependency,
    TenantRoleTemplate,
    TenantRolePermission,
    TenantRoleGroup,
    TenantUserRoleAssignment,
    TenantRoleChangeRequest,
    TenantRoleChangeDeltaItem,
)


_school_counter = itertools.count(1)
_role_key_counter = itertools.count(1)


def _as_tenant(school_or_tenant):
    """Accept a School or a Tenant and return the Tenant."""
    if isinstance(school_or_tenant, Tenant):
        return school_or_tenant
    return school_or_tenant.tenant


def _unique_role_key(tenant, name):
    base = slugify(name) or "role"
    key = base
    while TenantRoleTemplate.objects.filter(tenant=tenant, key=key).exists():
        key = f"{base}-{next(_role_key_counter)}"
    return key


def codex_tenant():
    """Return the codex platform tenant (created by the vs_tenants migrations)."""
    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


def make_school(slug="test-school", name="Test School", **kwargs):
    defaults = {"status": "ACTIVE"}
    defaults.update(kwargs)
    if "code" not in defaults:
        defaults["code"] = f"SC-{next(_school_counter):04d}"
    return School.objects.create(slug=slug, name=name, **defaults)


def make_branch(school, name="Main Branch", is_main=True, **kwargs):
    defaults = {"status": "ACTIVE"}
    defaults.update(kwargs)
    return Branch.objects.create(school=school, name=name, is_main=is_main, **defaults)


def make_vision_user(email="vision@test.com", password="testpass123",
                     super_admin=False, **kwargs):
    """Create a CX_STAFF user. With ``super_admin=True`` also grant the
    ``xvs_super_admin`` platform role so the user bypasses RBAC checks
    (mirrors how real Vision admins operate the platform)."""
    defaults = {
        "user_type": "CX_STAFF",
        "status": "ACTIVE",
        "first_name": "Vision",
        "last_name": "Staff",
    }
    defaults.update(kwargs)
    user = User.objects.create_user(email=email, password=password, **defaults)
    if super_admin:
        # Single-write the tenant tables: the xvs_super_admin codex role grants
        # the RBAC bypass that is_vision_super_admin() checks.
        tenant_role, _ = TenantRoleTemplate.objects.get_or_create(
            tenant=user.tenant, key="xvs_super_admin",
            defaults={"name": "Vision Super Admin", "status": "ACTIVE",
                      "is_system_role": True},
        )
        TenantUserRoleAssignment.objects.get_or_create(
            tenant=user.tenant, user=user, role=tenant_role,
            defaults={"assignment_status": "ACTIVE"},
        )
    return user


def make_school_admin(branch, email="admin@test.com", password="testpass123", **kwargs):
    defaults = {
        "user_type": "SCHOOL_ADMIN",
        "status": "ACTIVE",
        "branch": branch,
        "first_name": "School",
        "last_name": "Admin",
    }
    defaults.update(kwargs)
    # Legacy callers still pass school=; the column is gone — the tenant
    # derives from the branch (or an explicit tenant= kwarg).
    school = defaults.pop("school", None)
    if school is not None and "tenant" not in defaults and defaults.get("branch") is None:
        defaults["tenant"] = school.tenant
    return User.objects.create_user(email=email, password=password, **defaults)


def make_staff_user(branch, email="staff@test.com", password="testpass123", **kwargs):
    defaults = {
        "user_type": "STAFF",
        "status": "ACTIVE",
        "branch": branch,
        "first_name": "Staff",
        "last_name": "User",
    }
    defaults.update(kwargs)
    # Legacy callers still pass school=; the column is gone — the tenant
    # derives from the branch (or an explicit tenant= kwarg).
    school = defaults.pop("school", None)
    if school is not None and "tenant" not in defaults and defaults.get("branch") is None:
        defaults["tenant"] = school.tenant
    return User.objects.create_user(email=email, password=password, **defaults)


def make_permission(key, module_key=None, action=None, **kwargs):
    """Create a Permission from a dotted key like 'finance.invoice.view'.

    The registry is fully relational (module → resource → action FKs), so the
    key is split into its parts and each level is get_or_create'd.
    """
    from vs_rbac.models import PermissionAction, PermissionModule, PermissionResource

    parts = key.split(".")
    if len(parts) == 3:
        module_name, resource_name, action_name = parts
    else:  # fall back: treat everything before the last dot as the module
        module_name = module_key or (parts[0] if len(parts) > 1 else "general")
        resource_name = parts[-2] if len(parts) > 2 else "general"
        action_name = action or parts[-1]

    module, _ = PermissionModule.objects.get_or_create(name=module_name)
    resource, _ = PermissionResource.objects.get_or_create(
        module=module, name=resource_name
    )
    action_obj, _ = PermissionAction.objects.get_or_create(name=action_name)
    existing = Permission.objects.filter(key=key).first()
    if existing:
        return existing
    return Permission.objects.create(
        module=module, resource=resource, action=action_obj, **kwargs
    )


def make_permission_set(*keys):
    return [make_permission(k) for k in keys]


def make_dependency(permission_key, depends_on_key):
    return PermissionDependency.objects.create(
        permission_id=permission_key,
        depends_on_id=depends_on_key,
    )


def make_role(school_or_tenant, name="Test Role", **kwargs):
    """Create a TenantRoleTemplate for a school's tenant (or a tenant directly)."""
    tenant = _as_tenant(school_or_tenant)
    defaults = {"status": "ACTIVE"}
    defaults.update(kwargs)
    key = defaults.pop("key", None) or _unique_role_key(tenant, name)
    return TenantRoleTemplate.objects.create(
        tenant=tenant, key=key, name=name, **defaults
    )


def make_role_permission(role, permission, granted=True, **kwargs):
    return TenantRolePermission.objects.create(
        role=role, permission=permission, granted=granted, **kwargs
    )


def make_role_group(role, group, **kwargs):
    return TenantRoleGroup.objects.create(role=role, group=group, **kwargs)


def make_assignment(school_or_tenant, user, role, **kwargs):
    tenant = _as_tenant(school_or_tenant)
    defaults = {"assignment_status": "ACTIVE"}
    defaults.update(kwargs)
    return TenantUserRoleAssignment.objects.create(
        tenant=tenant, user=user, role=role, branch=role.branch, **defaults
    )


def make_role_change_request(school_or_tenant, user, role, justification="Test justification", **kwargs):
    tenant = _as_tenant(school_or_tenant)
    defaults = {"status": "PENDING"}
    defaults.update(kwargs)
    return TenantRoleChangeRequest.objects.create(
        tenant=tenant,
        requested_by=user,
        target_role=role,
        justification=justification,
        **defaults,
    )


def make_platform_role(name="Platform Role", **kwargs):
    """Create a TenantRoleTemplate on the codex platform tenant."""
    codex = codex_tenant()
    defaults = {"status": "ACTIVE", "is_system_role": True}
    defaults.update(kwargs)
    key = defaults.pop("key", None) or _unique_role_key(codex, name)
    return TenantRoleTemplate.objects.create(
        tenant=codex, key=key, name=name, **defaults
    )


def make_platform_role_permission(role, permission, granted=True, **kwargs):
    return TenantRolePermission.objects.create(
        role=role, permission=permission, granted=granted, **kwargs
    )


def make_platform_assignment(user, role, **kwargs):
    defaults = {"assignment_status": "ACTIVE"}
    defaults.update(kwargs)
    return TenantUserRoleAssignment.objects.create(
        tenant=role.tenant, user=user, role=role, **defaults
    )


def make_platform_change_request(user, role, justification="Test justification", **kwargs):
    defaults = {"status": "PENDING"}
    defaults.update(kwargs)
    return TenantRoleChangeRequest.objects.create(
        tenant=role.tenant,
        requested_by=user,
        target_role=role,
        justification=justification,
        **defaults,
    )
