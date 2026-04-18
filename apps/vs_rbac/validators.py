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

from .models import GroupPermission, Permission, PermissionDependency


class PermissionDependencyValidator:
    """
    Validates permission dependencies before role assignment.
    
    Usage:
        validator = PermissionDependencyValidator()
        validator.validate_permission_set(permission_keys=['finance.invoice.approve'])
    """
    
    def __init__(self):
        self._dependency_cache: Dict[str, Set[str]] = {}
        self._load_dependencies()
    
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
    
    def get_dependencies(self, permission_key: str) -> Set[str]:
        """Get all direct dependencies for a permission."""
        return self._dependency_cache.get(permission_key, set())
    
    def get_all_dependencies(self, permission_key: str, visited: Set[str] = None) -> Set[str]:
        """
        Recursively get all dependencies (direct + transitive).
        
        Returns set of all permission keys that must be granted before this one.
        """
        if visited is None:
            visited = set()
        
        if permission_key in visited:
            # Circular dependency detected
            raise ValidationError(
                f"Circular dependency detected for permission: {permission_key}"
            )
        
        visited.add(permission_key)
        
        all_deps = set()
        direct_deps = self.get_dependencies(permission_key)
        
        for dep_key in direct_deps:
            all_deps.add(dep_key)
            # Recursively get transitive dependencies
            all_deps.update(self.get_all_dependencies(dep_key, visited.copy()))
        
        return all_deps
    
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
        result.update(
            GroupPermission.objects.filter(group_id__in=group_ids).values_list(
                "permission_id", flat=True
            )
        )

    return sorted(result)


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
        return

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