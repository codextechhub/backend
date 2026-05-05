"""
Shared test helpers and fixture factories for vs_rbac tests.
"""
import itertools
from django.utils import timezone
from vs_schools.models import School, Branch
from vs_user.models import UserAccount
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


def make_vision_user(email="vision@test.com", password="testpass123", **kwargs):
    return UserAccount.objects.create_user(
        email=email,
        password=password,
        user_type="VS_STAFF",
        status="ACTIVE",
        full_name="Vision Staff",
        **kwargs,
    )


def make_school_admin(branch, email="admin@test.com", password="testpass123", **kwargs):
    defaults = {
        "user_type": "SCHOOL_ADMIN",
        "status": "ACTIVE",
        "branch": branch,
        "full_name": "School Admin",
    }
    defaults.update(kwargs)
    return UserAccount.objects.create_user(email=email, password=password, **defaults)


def make_staff_user(branch, email="staff@test.com", password="testpass123", **kwargs):
    defaults = {
        "user_type": "STAFF",
        "status": "ACTIVE",
        "branch": branch,
        "full_name": "Staff User",
    }
    defaults.update(kwargs)
    return UserAccount.objects.create_user(email=email, password=password, **defaults)


def make_permission(key, module_key=None, action=None, **kwargs):
    if module_key is None:
        parts = key.rsplit(".", 1)
        module_key = parts[0] if len(parts) > 1 else "general"
    if action is None:
        parts = key.rsplit(".", 1)
        action = parts[-1]
    return Permission.objects.create(
        key=key, module_key=module_key, action=action, **kwargs
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
