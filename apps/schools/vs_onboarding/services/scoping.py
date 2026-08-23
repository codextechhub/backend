"""One place that decides which rows belong to a tenant.

Every queryset in this module goes through :func:`rows_for`, and it uses the
*unscoped* manager plus an explicit tenant filter rather than the tenant-aware
one. That is not belt and braces, it is correctness: ``TenantAwareManager``
filters on the ambient tenant of the request, and provisioning runs from school
creation, where the ambient tenant is the platform actor's, not the new
school's. A tenant-aware manager there would scope to the wrong tenant and
quietly find nothing.

It fails closed. No tenant means no rows, never every row.
"""
from __future__ import annotations


def rows_for(model, tenant):
    """Return ``model``'s rows for ``tenant``, and nothing else."""
    manager = getattr(model, "all_objects", None) or model._default_manager
    if tenant is None:
        return manager.none()
    return manager.filter(tenant=tenant)
