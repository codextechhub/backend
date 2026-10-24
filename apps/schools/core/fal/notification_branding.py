"""Map school-product recipients to the public XVS email identity.

Notification delivery knows only that a recipient owns a tenant. This adapter
recognises the school product and returns the identity hosted by its public app,
keeping that product fact out of the notification engine.
"""
from __future__ import annotations

from urllib.parse import urljoin

from vs_tenants.app_urls import school_app_url
from vs_tenants.models import Tenant


# The XVS logo as the school app serves it for email: a square PNG, because the
# header draws the logo in a 42px square and mail clients such as Gmail do not
# display SVG. It is the XVS crest, never the CodeX mark, which is the console's.
XVS_EMAIL_LOGO_PATH = "image/xvs-logo-square.png"


def _school_logo_url(tenant) -> str:
    """The school's own uploaded logo at its public route, or "" when it has none."""
    from schools.vs_schools.serializers import public_logo_url

    profile = getattr(tenant, "school_profile", None)
    if profile is None:
        return ""
    branding = getattr(profile, "branding", None)
    return public_logo_url(profile.slug, has_logo=bool(branding is not None and branding.logo))


def _xvs_logo_url(tenant) -> str:
    """The XVS logo as the school's own app serves it, or "" when unaddressable.

    Built on the school's subdomain, because the bare school-app host serves
    the product site, not the app, and has no such file.
    """
    base = school_app_url(getattr(tenant, "slug", ""))
    return urljoin(base.rstrip("/") + "/", XVS_EMAIL_LOGO_PATH) if base else ""


def email_brand_for_tenant(tenant) -> dict[str, str]:
    """Return a school tenant's email identity, and no override otherwise.

    The header carries the school's own logo and nothing beside it
    (``email_mark_only``): a parent opening a fee reminder from
    Lagoon View Academy sees Lagoon View Academy's crest. A school that has
    uploaded no logo shows the XVS logo instead, never the CodeX mark. The
    footer still names XVS through ``email_brand``.

    ``sender_name`` is the school's own name, which every email to the
    school's people carries as its From name; the address stays the
    platform's sending address.
    """
    if getattr(tenant, "kind", None) != Tenant.Kind.SCHOOL:
        return {}

    return {
        "email_brand": "XVS",
        "brand_logo_url": _school_logo_url(tenant) or _xvs_logo_url(tenant),
        "email_mark_only": True,
        "sender_name": getattr(tenant, "name", "") or "",
    }
