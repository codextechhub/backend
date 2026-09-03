"""The platform boundary for CX-internal console surfaces.

Two inputs are never authorisation here. ``is_staff`` means "may log into
/admin/" and ``is_superuser`` is the Django escalation flag, so gating on
either makes any future grant of Django-admin access a silent grant of console
access. A console surface is gated on the actor's home tenant being the
platform one, plus an RBAC key.

Nothing in this module may define a class named ``IsVisionStaff``. The real one
lives in ``vs_rbac.permissions`` and roughly twenty views across the codebase
compose it; a local class of the same name reads as the platform boundary at
every call site while asking a different question, and nothing at the call site
says which of the two was imported.
"""
from __future__ import annotations

from rest_framework.permissions import BasePermission


class IsPlatformActor(BasePermission):
    """Gate CX-internal surfaces on the actor's own tenant kind.

    Why this exists as well as the RBAC key: ``HasRBACPermission`` matches a
    permission *string* against the roles the user holds on their own tenant. It
    does not care which tenant that is, and a school-tenant role can carry a
    ``platform.*`` key - ``vs_rbac/views.py`` already reckons with exactly that
    ("a school role that somehow carried a platform key"). So a key living in
    the ``platform`` module is a naming convention, not a boundary.

    For most platform endpoints that gap is covered downstream, because the rows
    they return are tenant-scoped and a school actor cannot assert another
    tenant. It is NOT covered for surfaces that serve global, non-tenant-scoped
    content - the requirements library being the first of them, where every
    document describes the whole platform for every customer. Those need the
    boundary stated outright, which is what this class does.

    Reads the home tenant, never the asserted ``?tenant=``, matching
    ``is_platform_actor`` in ``views.py``.
    """

    message = "This area is restricted to CX platform staff."

    def has_permission(self, request, view):
        from vs_tenants.models import Tenant

        user = request.user
        if not (user and user.is_authenticated):
            return False
        return getattr(getattr(user, "tenant", None), "kind", None) == Tenant.Kind.PLATFORM
