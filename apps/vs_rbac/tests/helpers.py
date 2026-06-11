"""
Shared test helpers and fixture factories for vs_rbac tests.
"""
import itertools
from django.utils import timezone
from vs_schools.models import School, Branch
from vs_user.models import User
from vs_rbac.models import (
    Permission,
    PermissionDependency,
    SchoolRoleTemplate,
    SchoolRolePermission,
    SchoolUserRoleAssignment,
    SchoolRoleChangeRequest,
    SchoolRoleChangeDeltaItem,
    PlatformRoleTemplate,
    PlatformRolePermission,
    PlatformUserRoleAssignment,
    PlatformRoleChangeRequest,
    PlatformRoleChangeDeltaItem,
)


_school_counter = itertools.count(1)


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
        role, _ = PlatformRoleTemplate.objects.get_or_create(
            id="xvs_super_admin",
            defaults={"name": "Vision Super Admin", "status": "ACTIVE"},
        )
        PlatformUserRoleAssignment.objects.get_or_create(
            user=user,
            role=role,
            defaults={"assignment_status": "ACTIVE"},
        )
    return user


def make_school_admin(branch, email="admin@test.com", password="testpass123", **kwargs):
    defaults = {
        "user_type": "SCHOOL_ADMIN",
        "status": "ACTIVE",
        "school": branch.school,
        "branch": branch,
        "first_name": "School",
        "last_name": "Admin",
    }
    defaults.update(kwargs)
    return User.objects.create_user(email=email, password=password, **defaults)


def make_staff_user(branch, email="staff@test.com", password="testpass123", **kwargs):
    defaults = {
        "user_type": "STAFF",
        "status": "ACTIVE",
        "school": branch.school,
        "branch": branch,
        "first_name": "Staff",
        "last_name": "User",
    }
    defaults.update(kwargs)
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


def make_role(school, name="Test Role", **kwargs):
    defaults = {"status": "ACTIVE"}
    defaults.update(kwargs)
    return SchoolRoleTemplate.objects.create(school=school, name=name, **defaults)


def make_role_permission(role, permission, granted=True, **kwargs):
    return SchoolRolePermission.objects.create(
        role=role, permission=permission, granted=granted, **kwargs
    )


def make_assignment(school, user, role, **kwargs):
    defaults = {"assignment_status": "ACTIVE"}
    defaults.update(kwargs)
    return SchoolUserRoleAssignment.objects.create(
        school=school, user=user, role=role, **defaults
    )


def make_role_change_request(school, user, role, justification="Test justification", **kwargs):
    defaults = {"status": "PENDING"}
    defaults.update(kwargs)
    return SchoolRoleChangeRequest.objects.create(
        school=school,
        requested_by=user,
        target_role=role,
        justification=justification,
        **defaults,
    )


def make_platform_role(name="Platform Role", **kwargs):
    defaults = {"status": "ACTIVE"}
    defaults.update(kwargs)
    return PlatformRoleTemplate.objects.create(name=name, **defaults)


def make_platform_role_permission(role, permission, granted=True, **kwargs):
    return PlatformRolePermission.objects.create(
        role=role, permission=permission, granted=granted, **kwargs
    )


def make_platform_assignment(user, role, **kwargs):
    defaults = {"assignment_status": "ACTIVE"}
    defaults.update(kwargs)
    return PlatformUserRoleAssignment.objects.create(
        user=user, role=role, **defaults
    )


def make_platform_change_request(user, role, justification="Test justification", **kwargs):
    defaults = {"status": "PENDING"}
    defaults.update(kwargs)
    return PlatformRoleChangeRequest.objects.create(
        requested_by=user,
        target_role=role,
        justification=justification,
        **defaults,
    )
