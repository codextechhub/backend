"""
Custom QuerySet and Manager for automatic tenant-aware filtering.

The tenant context is established per-request by TenantJWTAuthentication and
stored in a contextvar (vs_tenants.context). ``TenantAwareManager`` applies it
EAGERLY in ``get_queryset()``, so every entry point - ``all()``, ``filter()``,
``get()``, ``exists()``, related lookups through the default manager -
is scoped without any per-call machinery.

Usage in models:

    class Ticket(models.Model):
        tenant = models.ForeignKey(Tenant, ...)

        objects = TenantAwareManager()      # scoped by ambient tenant context
        all_objects = models.Manager()      # unscoped escape hatch

        class Meta:
            default_manager_name = "objects"
            base_manager_name = "all_objects"   # keep FK traversal unscoped

Options:

    TenantAwareManager(include_global=True)
        For models where a NULL tenant means "platform-wide / applies to
        every tenant" (e.g. global workflow templates, global compliance
        rules): a tenant-scoped request sees its own rows PLUS the global
        ones. Without the flag, NULL-tenant rows are platform-only and
        hidden from tenant users.

    TenantAwareManager(tenant_field="institution")
        For models whose tenant FK isn't named ``tenant``.

Vision (CX) staff requests never set a tenant context, so their queries are
never filtered. Celery tasks have no thread-local context either - they see
everything and must scope explicitly, which is the correct default for
platform jobs.

There is deliberately no ``school`` ownership path here. ``Branch`` was the
last model that reached its tenant through one, and it now carries its own
column; a school-shaped fallback in a domain-neutral engine app is the exact
leak the FAL exists to prevent, and
``vs_rbac.tests.test_branch_tenant_boundary.TenantLookupInvariantTests``
fails if a model ever regrows one.
"""
from __future__ import annotations

from django.db import models
from django.db.models import Q

from vs_tenants.context import get_current_tenant


# Support explicit tenant scoping when code cannot rely on request context.
class TenantAwareQuerySet(models.QuerySet):
    def for_tenant(self, tenant):
        if tenant is None:
            raise ValueError("An explicit tenant is required.")
        field_names = {f.name for f in self.model._meta.get_fields()}
        if "tenant" in field_names:
            return self.filter(tenant=tenant)
        if "branch" in field_names:
            # Branch carries its own tenant, so this is one join, not two.
            return self.filter(branch__tenant=tenant)
        raise ValueError(f"{self.model._meta.label} has no tenant ownership path.")

    # Apply the requested school's tenant scope. Kept for callers that hold a
    # School; it resolves to the tenant immediately and never looks at a
    # ``school`` column.
    def for_school(self, school):
        """Scope this queryset to *school*.

        Detects the tenant link automatically: a direct ``tenant`` FK or a
        ``branch`` FK (branches carry their own tenant). Models with neither
        raise, rather than silently returning every row.
        """
        if school is None:
            raise ValueError("An explicit school is required.")
        return self.for_tenant(school.tenant)


# Enforce ambient school scoping for ordinary ORM access.
class TenantAwareManager(models.Manager.from_queryset(TenantAwareQuerySet)):
    # Configure per-model tenant lookup rules.
    def __init__(self, *, tenant_field: str | None = None, include_global: bool = False):
        super().__init__()
        self.tenant_field = tenant_field
        self.include_global = include_global

    # Resolve the model field path that represents tenant ownership.
    def _tenant_lookup(self) -> str | None:
        if self.tenant_field:
            return self.tenant_field
        field_names = {f.name for f in self.model._meta.get_fields()}
        # Direct tenant ownership wins - every converted model carries it.
        if "tenant" in field_names:
            return "tenant"
        if "branch" in field_names:
            return "branch"
        return None

    # Attach the current tenant filter before callers add their own conditions.
    def get_queryset(self):
        qs = super().get_queryset()
        tenant = get_current_tenant()
        if tenant is None:
            return qs
        lookup = self._tenant_lookup()
        if lookup is None:
            return qs
        if lookup == "branch":
            # ``branch__tenant``, not ``branch__school__tenant``: Branch owns a
            # tenant of its own, and no longer has a school to travel through.
            lookup = "branch__tenant"
        condition = Q(**{lookup: tenant})
        if self.include_global:
            # School users also see platform-wide template rows when the model opts in.
            condition |= Q(**{f"{lookup}__isnull": True})
        return qs.filter(condition)

    # Bypass ambient context when platform code intentionally targets one school.
    def for_school(self, school):
        """Explicitly scope to *school*, IGNORING the ambient request context.

        Use this when platform code needs to look at a specific school's rows
        regardless of who is asking.
        """
        return TenantAwareQuerySet(self.model, using=self._db).for_school(school)

    def for_tenant(self, tenant):
        return TenantAwareQuerySet(self.model, using=self._db).for_tenant(tenant)
