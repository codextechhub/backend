from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.db import models

from .context import get_current_tenant


class TenantQuerySet(models.QuerySet):
    def for_tenant(self, tenant):
        if tenant is None:
            raise ValueError("An explicit tenant is required.")
        return self.filter(tenant=tenant)


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    """Fail-closed ambient scoping for TenantOwnedModel descendants."""

    def get_queryset(self):
        tenant = get_current_tenant()
        if tenant is None:
            raise ImproperlyConfigured(
                f"{self.model._meta.label} requires an explicit tenant context. "
                "Use all_objects.for_tenant(...) in jobs and commands."
            )
        return super().get_queryset().filter(tenant=tenant)
