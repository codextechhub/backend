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

from core.thread_locals import get_current_school


class TenantAwareQuerySet(models.QuerySet):
    def for_school(self, school):
        """Scope this queryset to *school*.

        Detects the tenant link automatically: a direct ``school`` FK, or an
        indirect one via ``branch__school``. Models with neither are returned
        unfiltered (platform-level data).
        """
        if school is None:
            return self
        field_names = {f.name for f in self.model._meta.get_fields()}
        if "school" in field_names:
            return self.filter(school=school)
        if "branch" in field_names:
            return self.filter(branch__school=school)
        return self


class TenantAwareManager(models.Manager.from_queryset(TenantAwareQuerySet)):
    def __init__(self, *, tenant_field: str | None = None, include_global: bool = False):
        super().__init__()
        self.tenant_field = tenant_field
        self.include_global = include_global

    def _tenant_lookup(self) -> str | None:
        if self.tenant_field:
            return self.tenant_field
        field_names = {f.name for f in self.model._meta.get_fields()}
        if "school" in field_names:
            return "school"
        if "branch" in field_names:
            return "branch__school"
        return None

    def get_queryset(self):
        qs = super().get_queryset()
        school = get_current_school()
        if school is None:
            return qs
        lookup = self._tenant_lookup()
        if lookup is None:
            return qs
        condition = Q(**{lookup: school})
        if self.include_global:
            condition |= Q(**{f"{lookup}__isnull": True})
        return qs.filter(condition)

    def for_school(self, school):
        """Explicitly scope to *school*, IGNORING the ambient request context.

        Use this when platform code needs to look at a specific school's rows
        regardless of who is asking.
        """
        return TenantAwareQuerySet(self.model, using=self._db).for_school(school)
