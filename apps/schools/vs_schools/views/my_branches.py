"""A school's own branches, read-only, at ``/v1/i/me/branches/``.

Why this exists beside ``views/branch.py`` rather than opening those. Every view
in that module demands ``platform.branches.view`` / ``.create`` / ``.update``,
which is PLATFORM-scoped and held by no school role - so a live school
administrator asking for her own sites is refused outright. The branch-options
endpoint in ``vs_tenants`` already names the problem in its own docstring:
``platform.branches.view`` is "the thing standing between a bursar and her own
sites".

Opening those views would hand a school branch CREATION and EDITING with the
same key, and neither is a school's to do - the platform provisions sites. So
this is the read half only, on the school's own key, in the shape
``/v1/i/me/profile/`` and ``/v1/i/me/staff/`` already established:

**It takes no school identifier.** The school is ``request.tenant``'s, so there
is no slug to tamper with and no way to read another school's sites.

**It is read-only.** No create, no update, no transition. A school that needs a
new branch or a corrected address asks CodeX, which is the same answer the
branch endpoints give today - only now it is a screen saying so rather than a
403 nobody can act on.

**It is NOT on the pending-tenant surface.** Branches are a live-school screen;
during onboarding the control room is the whole app.
"""
from __future__ import annotations

from rest_framework import generics
from rest_framework.exceptions import NotFound

from core.pagination import XVSPagination
from core.response import success_response
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_tenants.models import Branch

from ..serializers import SchoolBranchSerializer


class _MyBranchBase:
    """Shared wiring: this school's sites, on this school's own key."""

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    # The key a school actually holds. ``school.branches.view`` has been granted
    # to every school administrator all along and read by nothing; this is the
    # screen it was always for.
    rbac_permission = "school.branches.view"
    serializer_class = SchoolBranchSerializer

    def get_queryset(self):
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            return Branch.objects.none()
        return (
            Branch.objects.filter(tenant=tenant)
            # The main branch first, then by the code a school already knows its
            # sites by, so the order matches how they talk about them.
            .order_by("-is_main", "code")
        )


class MyBranchListView(_MyBranchBase, generics.ListAPIView):
    """GET /v1/i/me/branches/ - the branches this school runs.

    docstring-name: My school's branches
    """

    pagination_class = XVSPagination


class MyBranchDetailView(_MyBranchBase, generics.RetrieveAPIView):
    """GET /v1/i/me/branches/<code>/ - one branch in full.

    Looked up by ``code``, the per-school number a school uses for its own
    sites, rather than the global row id. Another school's branch cannot be
    reached: the queryset is scoped to the caller's tenant first, so a code that
    exists elsewhere is a 404 here rather than a 403 that confirms it exists.

    docstring-name: My school's branch
    """

    lookup_field = "code"
    lookup_url_kwarg = "code"

    def get_object(self):
        branch = self.get_queryset().filter(
            code=self.kwargs[self.lookup_url_kwarg],
        ).first()
        if branch is None:
            raise NotFound("No such branch at this school.")
        return branch

    def retrieve(self, request, *args, **kwargs):
        # The app's envelope, which DRF's own RetrieveAPIView does not apply.
        # The list gets one from the paginator; without this the two halves of
        # the same endpoint answer in two different shapes.
        serializer = self.get_serializer(self.get_object())
        return success_response("Branch retrieved.", data=serializer.data)
