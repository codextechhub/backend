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

Vision (CX) staff requests never set a school context, so their queries are
never filtered. Celery tasks have no thread-local context either — they see
everything and must scope explicitly, which is the correct default for
platform jobs.
"""
from __future__ import annotations

from django.db import models

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
    def get_queryset(self):
        qs = super().get_queryset()
        school = get_current_school()
        if school is not None:
            qs = qs.for_school(school)
        return qs

    def for_school(self, school):
        """Explicitly scope to *school*, IGNORING the ambient request context.

        Use this when platform code needs to look at a specific school's rows
        regardless of who is asking.
        """
        return TenantAwareQuerySet(self.model, using=self._db).for_school(school)
