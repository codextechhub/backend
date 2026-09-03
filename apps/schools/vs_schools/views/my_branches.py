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

**The list IS on the pending-tenant surface; the detail read is not.** That is a
correction rather than an original position. This module first reasoned that
branches are a live-school screen and that during onboarding the control room is
the whole app, which is right about a *branches screen* and wrong about this
list, because academic structure is built before go-live and cannot be built
without it.

``TaskKey.ACADEMIC_STRUCTURE`` is a required onboarding task
(``schools/vs_onboarding/constants.py``), and the academic-structure screens
scope a department, a programme, a level, a class or a subject to "the whole
school" or to one
branch. That control reads this list. With the surface shut, a two-branch school
still PENDING gets an empty branch picker, cannot scope anything to a branch,
and so cannot finish the required task that would make it live - the school is
locked out of go-live by the gate that exists to protect go-live.

Only the list is opened. Nothing before go-live reads one branch on its own, and
``pending_tenant_surface`` is per view precisely so that absence keeps meaning
closed for the next view added here.
"""
from __future__ import annotations

from django.db.models import Count, Q
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
            .annotate(
                # The count SchoolBranchSerializer promised would arrive the
                # day a Class model existed. Live classes only: an archived one
                # is retired, and counting it would make this card disagree
                # with the class list the same admin reads next door.
                #
                # Spelled through the reverse accessor rather than by
                # importing schools.vs_academics, so this app keeps no
                # dependency on a module that landed after it.
                classes_count_annotated=Count(
                    "classes",
                    filter=Q(classes__is_active=True),
                    distinct=True,
                ),
            )
            # The main branch first, then by the code a school already knows its
            # sites by, so the order matches how they talk about them.
            .order_by("-is_main", "code")
        )


class MyBranchListView(_MyBranchBase, generics.ListAPIView):
    """GET /v1/i/me/branches/ - the branches this school runs.

    docstring-name: My school's branches
    """

    # A school builds its academic structure while it is still PENDING, and
    # every "applies to" control on those screens picks from this list. See the
    # module docstring for why the detail route below is deliberately left shut.
    pending_tenant_surface = True

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
