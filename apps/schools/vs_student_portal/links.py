"""Addresses inside the student app, for links the backend hands out.

The student app is served at ``/student/`` on the school's own host
(``corona.xvs.codexng.com/student/``). ``STUDENT_APP_BASE_URL`` names a
different host only where the app is served separately, as a second dev server
is; empty, it is the school app's host.
"""
from __future__ import annotations

from django.conf import settings

from schools.vs_students.constants import STUDENT_APP_PATH
from vs_tenants.app_urls import school_app_url


def student_app_url(tenant, path: str = "") -> str:
    """``https://<slug>.<host>/student/<path>``, or "" when no host is configured."""
    base = getattr(settings, "STUDENT_APP_BASE_URL", "") or None
    host = school_app_url(tenant.slug, base=base)
    if not host:
        return ""
    return f"{host}{STUDENT_APP_PATH}{path.lstrip('/')}"
