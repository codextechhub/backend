"""
Permission dependency validation logic.

Validates that:
- All dependencies are satisfied before granting permissions
- No circular dependencies exist
- Hard vs soft dependencies are enforced
"""
from __future__ import annotations

from typing import Set, List, Dict
from django.core.exceptions import ValidationError

from .models import (
    GroupPermission,
    Permission,
    PermissionDependency,
    TenantRoleGroup,
    TenantRolePermission,
)


RESTRICTED_ROLE_CHANGE_MESSAGE = (
    "Restricted permissions cannot be granted directly. Submit a role change "
    "request for approval."
)


def restricted_permission_keys(permission_keys) -> Set[str]:
    """Return the restricted subset of a permission-key iterable."""
    keys = {key for key in permission_keys or [] if key}
    if not keys:
        return set()
    return set(
        Permission.objects.filter(key__in=keys, is_restricted=True).values_list(
            "key", flat=True,
        )
    )


def group_permission_keys(group_ids, *, include_restricted=True) -> Set[str]:
    """Flatten group membership, optionally ignoring forbidden legacy members."""
    if not group_ids:
        return set()
    qs = GroupPermission.objects.filter(group_id__in=group_ids)
    if not include_restricted:
        qs = qs.filter(permission__is_restricted=False)
    return set(qs.values_list("permission_id", flat=True))


def role_permission_keys(role, *, include_restricted_groups=True) -> Set[str]:
    """Return a role's direct and group-derived permission keys."""
    direct = set(
        TenantRolePermission.objects.filter(role=role, granted=True).values_list(
            "permission_id", flat=True,
        )
    )
    group_ids = TenantRoleGroup.objects.filter(role=role).values_list(
        "group_id", flat=True,
    )
    return direct | group_permission_keys(
        group_ids, include_restricted=include_restricted_groups,
    )


def role_restricted_permission_keys(role) -> Set[str]:
    """Return every restricted key a role would hand to an assignee."""
    return restricted_permission_keys(role_permission_keys(role))


def missing_restricted_grant_authority(actor, permission_keys) -> Set[str]:
    """Restricted keys outside the actor's effective grant ceiling.

    A restricted grant may be approved or assigned only by somebody who
    already holds that key. The Vision super admin is the bootstrap authority
    and may provision the first holder.
    """
    restricted = restricted_permission_keys(permission_keys)
    if not restricted:
        return set()

    from .permissions import is_vision_super_admin

    if is_vision_super_admin(actor):
        return set()

    from .evaluator import get_effective_permissions

    tenant = getattr(actor, "tenant", None)
    if tenant is None:
        return restricted
    held = get_effective_permissions(actor, tenant=tenant)
    return restricted - held


# Validate role permission sets against the dependency graph.
class PermissionDependencyValidator:
    """
    Validates permission dependencies before role assignment.
    
    Usage:
        validator = PermissionDependencyValidator()
        validator.validate_permission_set(permission_keys=['finance.invoice.approve'])
    """
    
    # Load dependencies once so a role change can validate many keys cheaply.
    def __init__(self):
        self._dependency_cache: Dict[str, Set[str]] = {}
        self._load_dependencies()
    
    # Build an in-memory permission -> required-permissions map.
    def _load_dependencies(self):
        """Load all dependencies into memory for fast validation."""
        dependencies = PermissionDependency.objects.select_related(
            'permission', 'depends_on'
        ).all()
        
        for dep in dependencies:
            perm_key = dep.permission_id
            depends_key = dep.depends_on_id
            
            if perm_key not in self._dependency_cache:
                self._dependency_cache[perm_key] = set()
            
            self._dependency_cache[perm_key].add(depends_key)
    
    # Return prerequisites directly attached to one permission.
    def get_dependencies(self, permission_key: str) -> Set[str]:
        """Get all direct dependencies for a permission."""
        return self._dependency_cache.get(permission_key, set())
    
    # Resolve the full prerequisite chain for one permission.
    def get_all_dependencies(self, permission_key: str, visited: Set[str] = None) -> Set[str]:
        """
        Recursively get all dependencies (direct + transitive).
        
        Returns set of all permission keys that must be granted before this one.
        """
        if visited is None:
            visited = set()
        
        if permission_key in visited:
            # A cycle would make the permission impossible to satisfy safely.
            raise ValidationError(
                f"Circular dependency detected for permission: {permission_key}"
            )
        
        visited.add(permission_key)
        
        all_deps = set()
        direct_deps = self.get_dependencies(permission_key)
        
        for dep_key in direct_deps:
            all_deps.add(dep_key)
            # Transitive dependencies must be granted too, not just the direct parent.
            all_deps.update(self.get_all_dependencies(dep_key, visited.copy()))
        
        return all_deps
    
    # Check a proposed role grant set before it can be persisted.
    def validate_permission_set(self, permission_keys: List[str]) -> Dict[str, any]:
        """
        Validate that a set of permissions satisfies all dependencies.
        
        Returns:
            {
                'valid': bool,
                'missing_dependencies': {
                    'permission_key': ['missing_dep1', 'missing_dep2']
                },
                'errors': ['error message 1', ...]
            }
        """
        permission_set = set(permission_keys)
        missing_dependencies = {}
        errors = []
        
        for perm_key in permission_keys:
            try:
                required_deps = self.get_all_dependencies(perm_key)
            except ValidationError as e:
                errors.append(str(e))
                continue
            
            missing = required_deps - permission_set
            
            if missing:
                missing_dependencies[perm_key] = sorted(missing)
        
        return {
            'valid': len(missing_dependencies) == 0 and len(errors) == 0,
            'missing_dependencies': missing_dependencies,
            'errors': errors,
        }
    
    # Scan the whole dependency graph for configuration mistakes.
    def detect_circular_dependencies(self) -> List[str]:
        """
        Detect all circular dependencies in the permission graph.
        
        Returns list of error messages describing circular dependencies.
        """
        errors = []
        
        for perm_key in self._dependency_cache.keys():
            try:
                self.get_all_dependencies(perm_key)
            except ValidationError as e:
                errors.append(str(e))
        
        return errors


# Combine direct grants with group-derived grants before dependency validation.
def flatten_permission_keys(
    permission_keys: List[str] | None = None,
    group_ids: List = None,
) -> List[str]:
    """Flatten direct permission keys + group ids into a unique permission list.

    Used before dependency validation when a role is configured with a mix of
    individual permission grants and attached permission groups.
    """
    result: Set[str] = set(permission_keys or [])

    if group_ids:
        # Group grants count toward dependency satisfaction just like direct grants.
        # Restricted permissions are never valid group members. The filter is
        # a runtime backstop for rows written before that rule existed.
        result.update(group_permission_keys(group_ids, include_restricted=False))

    return sorted(result)


# Fail a role update when its effective permission set is incomplete.
def validate_role_permissions(
    permission_keys: List[str] | None = None,
    group_ids: List = None,
) -> None:
    """
    Validate the effective permission set before assigning to a role.

    Accepts direct permission keys and/or group ids. The two inputs are
    flattened into a single permission set, which is then checked against the
    permission dependency graph.

    Raises ValidationError if dependencies are not satisfied.
    """
    effective_keys = flatten_permission_keys(permission_keys, group_ids)

    if not effective_keys:
        return  # Empty roles are valid; there is no permission dependency to satisfy.

    validator = PermissionDependencyValidator()
    result = validator.validate_permission_set(effective_keys)

    if not result['valid']:
        error_messages = []

        for perm, missing in result['missing_dependencies'].items():
            error_messages.append(
                f"Permission '{perm}' requires: {', '.join(missing)}"
            )

        error_messages.extend(result['errors'])

        raise ValidationError({
            'permission_keys': error_messages
        })
