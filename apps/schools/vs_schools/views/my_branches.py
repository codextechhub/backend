"""A school's own branches, read-only, at ``/v1/i/me/branches/``.

This sits beside ``views/branch.py`` rather than opening those views. Every view
there demands ``platform.branches.view`` / ``.create`` / ``.update``, which is
PLATFORM-scoped and held by no school role, and they carry creation and editing
on the same key. Neither is a school's to do: CodeX provisions and corrects
branches. So this is the read half only, in the shape ``/v1/i/me/profile/`` and
``/v1/i/me/staff/`` established.

**Every member of the school's staff may read it.** The gate is
:class:`vs_rbac.permissions.IsTenantStaff`: an account holding an active role
at the school, whether a library role or one the school made itself (a bursar,
an admissions officer), or a personal grant there. Every form that files a
record under a branch decides whether to ask for one, and offers the choices,
from this list. A key
for it would have to be granted to every role a school ever creates, and the
role somebody forgot would leave its holder at a two-branch school unable to
file an expense claim, because the form never asks and the server refuses a
claim that names no branch.

A parent's or a pupil's account holds no grant and is refused; the parent app
and the pupil portal answer from their own endpoints. ``school.branches.view``
remains the key of the Branches screen itself, not of this list.

What a reader sees of each branch is Field Access's question, not the gate's.
The name, code, main flag and class count are open to every reader; address and
email each have a Read switch of ``school.branches``, so a school can close them
to a role (see ``SchoolBranchSerializer``).

**It takes no school identifier.** The school is ``request.tenant``'s, so there
is no slug to tamper with and no way to read another school's branches.

**It is read-only.** No create, no update, no transition. A school that needs a
new branch or a corrected address asks CodeX.

**The detail read follows the same rule as the list.** It returns one row of
the list in the same shape under the same switches, so a key on it would protect
nothing the list does not already show.

**The list is on the pending-tenant surface; the detail read is not.**
``TaskKey.ACADEMIC_STRUCTURE`` is a required onboarding task
(``schools/vs_onboarding/constants.py``), and the academic-structure screens
scope a department, a programme, a level, a class or a subject to the whole
school or to one branch. That control reads this list, so with the surface shut
a two-branch school still PENDING gets an empty branch picker, cannot finish the
task, and never goes live. Nothing before go-live reads one branch on its own,
and ``pending_tenant_surface`` is per view so that absence keeps meaning closed
for the next view added here.
"""
from __future__ import annotations

from django.db.models import Count, Q
from rest_framework import generics
from rest_framework.exceptions import NotFound

from core.pagination import XVSPagination
from core.response import success_response
from vs_rbac.permissions import IsTenantStaff, IsAuthenticatedAndActive
from vs_tenants.models import Branch

from ..serializers import SchoolBranchSerializer


class _MyBranchBase:
    """Shared wiring: this school's branches, for any member of its staff."""

    permission_classes = [IsAuthenticatedAndActive & IsTenantStaff]
    serializer_class = SchoolBranchSerializer

    def get_queryset(self):
        """The request's school's branches, each with its count of live classes.

        An archived class is retired, so counting it would make a branch card
        disagree with the class list beside it. The count is spelled through
        the reverse accessor rather than an import of ``schools.vs_academics``,
        so this app keeps no dependency on that module.
        """
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            return Branch.objects.none()
        return (
            Branch.objects.filter(tenant=tenant)
            .annotate(
                classes_count_annotated=Count(
                    "classes",
                    filter=Q(classes__is_active=True),
                    distinct=True,
                ),
            )
            # Main branch first, then by the school's own branch code.
            .order_by("-is_main", "code")
        )


class MyBranchListView(_MyBranchBase, generics.ListAPIView):
    """GET /v1/i/me/branches/ - the branches this school runs.

    docstring-name: My school's branches
    """

    # Open before go-live; see the module docstring.
    pending_tenant_surface = True

    pagination_class = XVSPagination


class MyBranchDetailView(_MyBranchBase, generics.RetrieveAPIView):
    """GET /v1/i/me/branches/<code>/ - one branch in full.

    Looked up by ``code``, the per-school number a school uses for its own
    branches, rather than the global row id. Another school's branch cannot be
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
