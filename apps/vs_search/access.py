"""The door a people provider asks before searching on somebody's behalf.

A provider answers inside the search endpoint, which holds no permission key of
its own, so it has to ask the question its module's own search endpoint asks
at the door: does this caller hold the read key, and does the tenant's plan
reach it. :func:`caller_may` asks both in the same order and with the same
answers as :class:`vs_rbac.permissions.HasRBACPermission`, so a provider can
never return people the module's own endpoint would refuse to show.
"""
from __future__ import annotations

from vs_rbac.evaluator import has_permission
from vs_rbac.permissions import is_vision_super_admin
from vs_rbac.plan_gate import plan_refusal


def caller_may(user, tenant, *permission_keys: str) -> bool:
    """Whether *user* would pass a view gated on any of *permission_keys* in *tenant*."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if is_vision_super_admin(user):
        return True
    if not any(has_permission(user, key, tenant=tenant) for key in permission_keys):
        return False
    return not plan_refusal(list(permission_keys), tenant)
