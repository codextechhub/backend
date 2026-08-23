from __future__ import annotations

from rest_framework.permissions import BasePermission, SAFE_METHODS


# Restrict an endpoint to actors whose HOME tenant is the platform (Codex) one.
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


# Gate admin-console endpoints to authenticated Django staff accounts.
class IsVisionStaff(BasePermission):
    """
    Simple gate:
    - allow only Django users with is_staff=True
    (Later you can swap this to a richer RBAC system.)
    """
    message = "Vision Admin Console access is restricted to staff users."

    def has_permission(self, request, view):
        user = request.user
        # This console is platform operational tooling, not tenant self-service.
        return bool(user and user.is_authenticated and user.is_staff)


# Allow staff visibility while reserving dangerous writes for superusers.
class StaffReadOnlyOrSuperuserWrite(BasePermission):
    """
    Staff can read.
    Only superusers can write.
    Useful for high-risk endpoints like provisioning/import logs if you want.
    """
    message = "Write access requires superuser."

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_staff):
            return False
        if request.method in SAFE_METHODS:
            return True
        # Mutating operational state requires the narrower superuser boundary.
        return bool(user.is_superuser)
