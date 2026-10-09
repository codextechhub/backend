"""Map school-product recipients to the public XVS email identity.

Notification delivery knows only that a recipient owns a tenant. This adapter
recognises the school product and returns the identity hosted by its public app,
keeping that product fact out of the notification engine.
"""
from __future__ import annotations

from urllib.parse import urljoin

from django.conf import settings

from vs_tenants.models import Tenant


def email_brand_for_tenant(tenant) -> dict[str, str]:
    """Return XVS branding for a school tenant and no override otherwise."""
    if getattr(tenant, "kind", None) != Tenant.Kind.SCHOOL:
        return {}

    base = (getattr(settings, "SCHOOL_APP_BASE_URL", "") or "").rstrip("/") + "/"
    return {
        "email_brand": "XVS",
        "brand_logo_url": urljoin(base, "svg/logo-blue.svg") if base != "/" else "",
    }
