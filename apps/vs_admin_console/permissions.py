from __future__ import annotations

from rest_framework.permissions import BasePermission


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


# NOTE: this module used to define its own ``IsVisionStaff``, and that was the
# defect rather than a stylistic wrinkle.
#
# ``vs_rbac.permissions.IsVisionStaff`` is the platform's real gate and asks
# the right question - is this account on a PLATFORM-kind tenant - and roughly
# twenty views across vs_payments, vs_user, vs_todo and apps/schools compose
# it. The copy that lived here shared its name and asked a different question
# entirely: ``user.is_staff``, the Django-admin login flag.
#
# A name collision is what made it dangerous. ``permission_classes =
# [IsVisionStaff]`` in a sibling module reads as the platform-wide boundary to
# anyone who has met the real class, and nothing at the call site said which
# of the two had been imported. The task monitor was gated on the weaker one
# for that reason, and the effect was that every Codex account - a sales hire,
# a designer, anyone the platform grants Django admin to - could list every
# tenant's task runs together with their raw errors and tracebacks.
#
# ``is_staff`` is also the wrong input on its own terms: it already means "may
# log into /admin/", so reusing it as an authorisation decision means any
# future grant of Django-admin access silently grants console access too.
#
# The class is gone rather than fixed. Import ``IsVisionStaff`` from
# ``vs_rbac.permissions`` and pair it with an RBAC key, which is what
# ``IsPlatformActor`` above explains and what ``views_tasks`` now does.

# ``StaffReadOnlyOrSuperuserWrite`` stood here and is deleted with the class
# above. It was never imported anywhere, and it was built on the same wrong
# input - ``is_staff`` for the read half, ``is_superuser`` for the write half -
# so leaving it in place would have left the next person a ready-made way to
# reintroduce exactly this bug. Gate on the platform tenant plus an RBAC key.
