"""Addresses inside the parent app, for links the notices module hands out.

The parent app is served at ``/parent/`` on the school's own host
(``corona.xvs.codexng.com/parent/``). ``PARENT_APP_BASE_URL`` names a different
host only where the app is served separately, as a second dev server is; empty,
it is the school app's host.
"""
from __future__ import annotations

from django.conf import settings

from vs_tenants.app_urls import school_app_url

from .constants import PARENT_APP_PATH


def parent_app_url(tenant, path: str = "") -> str:
    """``https://<slug>.<host>/parent/<path>``, or "" when no host is configured."""
    base = getattr(settings, "PARENT_APP_BASE_URL", "") or None
    host = school_app_url(tenant.slug, base=base)
    if not host:
        return ""
    return f"{host}{PARENT_APP_PATH}{path.lstrip('/')}"
