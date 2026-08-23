from __future__ import annotations

from rest_framework.exceptions import NotFound, ValidationError

from .models import Tenant


def resolve_tenant(request):
    """Resolve and authorize the mandatory ``?tenant=<slug>`` assertion."""
    slug = (request.query_params.get("tenant") or "").strip().lower()
    if not slug:
        raise ValidationError({"tenant": "A 'tenant' query parameter is required."})

    tenant = Tenant.objects.filter(slug=slug, status=Tenant.Status.ACTIVE).first()
    effective_user = getattr(request, "effective_user", None) or request.user
    if tenant is None or getattr(effective_user, "tenant_id", None) != tenant.pk:
        raise NotFound("No tenant matches the requested context.")
    request.tenant = tenant
    return tenant
