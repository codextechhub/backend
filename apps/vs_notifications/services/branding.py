"""Resolve the product identity used by system notification email.

The notification engine owns the CodeX default. A product may replace that
identity through ``NOTIFICATION_EMAIL_BRAND_PROVIDER`` without teaching this
domain-neutral app about the product's tenant model or assets. The result is
applied by recipient tenant, so one cross-tenant event can use a different
identity for each audience.
"""
from __future__ import annotations

from urllib.parse import urljoin

from django.conf import settings
from django.utils.module_loading import import_string

from .layout import brand_logo_from_context


def _product_override(tenant) -> dict:
    """Return the configured product brand for a tenant, when one applies."""
    path = getattr(settings, "NOTIFICATION_EMAIL_BRAND_PROVIDER", "")
    if not path:
        return {}
    value = import_string(path)(tenant)
    return dict(value or {})


def email_brand_context(tenant) -> dict[str, str]:
    """Return the trusted product name and public logo URL for one audience."""
    override = _product_override(tenant)
    name = str(override.get("email_brand") or "").strip()
    logo_url = brand_logo_from_context(override)
    if name and logo_url:
        return {"email_brand": name, "brand_logo_url": logo_url}

    from vs_config.platform_settings import get_platform_profile

    profile = get_platform_profile()
    default_logo_url = urljoin(
        f"{settings.FRONTEND_BASE_URL.rstrip('/')}/",
        "image/codex-logo.jpg",
    )
    return {
        "email_brand": name or "CodeX",
        "brand_logo_url": logo_url or brand_logo_from_context(
            {"brand_logo_url": profile.get("logo_url") or default_logo_url}
        ),
    }
