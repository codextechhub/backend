"""
Custom QuerySet and Manager for automatic tenant-aware filtering.

The school context is established per-request (TenantJWTAuthentication for
JWT calls, TenantContextMiddleware for session calls) and stored in
thread-local storage. ``TenantAwareManager`` applies it EAGERLY in
``get_queryset()``, so every entry point — ``all()``, ``filter()``,
``get()``, ``exists()``, related lookups through the default manager —
is scoped without any per-call machinery.

Usage in models:

    class Student(models.Model):
        school = models.ForeignKey(School, ...)

        objects = TenantAwareManager()      # scoped by ambient school context
        all_objects = models.Manager()      # unscoped escape hatch

        class Meta:
            default_manager_name = "objects"
            base_manager_name = "all_objects"   # keep FK traversal unscoped

Options:

    TenantAwareManager(include_global=True)
        For models where a NULL school means "platform-wide / applies to
        every school" (e.g. global workflow templates, global compliance
        rules): a school-scoped request sees its own rows PLUS the global
        ones. Without the flag, NULL-school rows are platform-only and
        hidden from school users.

    TenantAwareManager(tenant_field="institution")
        For models whose tenant FK isn't named ``school``.

Vision (CX) staff requests never set a school context, so their queries are
never filtered. Celery tasks have no thread-local context either — they see
everything and must scope explicitly, which is the correct default for
platform jobs.
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
        if "school" in field_names:
            return self.filter(school__tenant=tenant)
        if "branch" in field_names:
            return self.filter(branch__school__tenant=tenant)
        raise ValueError(f"{self.model._meta.label} has no tenant ownership path.")

    # Apply the requested school scope across direct-school and branch-owned models.
    def for_school(self, school):
        """Scope this queryset to *school*.

        Detects the tenant link automatically: a direct ``school`` FK, or an
        indirect one via ``branch__school``. Models with neither are returned
        unfiltered (platform-level data).
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

    # Resolve the model field path that represents school ownership.
    def _tenant_lookup(self) -> str | None:
        if self.tenant_field:
            return self.tenant_field
        field_names = {f.name for f in self.model._meta.get_fields()}
        if "school" in field_names:
            return "school"
        if "branch" in field_names:
            return "branch__school"
        return None

    # Attach the current school filter before callers add their own conditions.
    def get_queryset(self):
        qs = super().get_queryset()
        tenant = get_current_tenant()
        if tenant is None:
            return qs
        lookup = self._tenant_lookup()
        if lookup is None:
            return qs
        if lookup == "school":
            lookup = "school__tenant"
        elif lookup == "branch__school":
            lookup = "branch__school__tenant"
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
