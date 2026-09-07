"""Wiring every view in this module shares.

Four things, all of which the repo's ship-check asks about by name.

**Tenant scoping is the view's job.** Every queryset is filtered on
``request.tenant`` and a row belonging to another school answers **404, never
403**, so a staff id cannot be used to learn that somebody works elsewhere. The
wording matters as much as the code: "no such person at this school" rather than
"this user does not exist", because the same address may legitimately be an
account at another school and a refusal must not say so.

**The branch narrowing here is inclusive**, unlike ``vs_students``'. A posting
is nullable and a null means across the whole school, so a branch administrator
sees their branch's people **plus** the school-wide ones. A registrar with no
posting is not missing data; she belongs to the school and appears in every
roster.

**A person always reaches their own record**, whatever they hold. A teacher with
no staff key at all opens her own profile, her own documents and her own leave,
and nobody else's.

**``pending_tenant_surface`` is declared deliberately, one view at a time.**
Absence means closed. The directory, the invite, the record and the posting are
open because "Add Staff & Invitations" is a step on the school's own onboarding
checklist, and a closed surface would leave a school unable to finish onboarding
and unable to go live. Teaching, coverage, leave and the lifecycle are closed:
nobody resigns during onboarding, and there is nothing to teach before a session
exists.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

from core.pagination import XVSPagination
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from ..services.scoping import branch_dimension_applies


class StaffViewMixin:
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    pagination_class = XVSPagination

    @property
    def tenant(self):
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            # Fail closed: an unscoped queryset looks like a working endpoint
            # until somebody reads another school's people.
            raise NotFound("No school in context.")
        return tenant

    @property
    def multi_branch(self) -> bool:
        """Whether the branch dimension is rendered at all.

        Cached per request, because several serializers ask and the answer
        cannot change mid-request.
        """
        cached = getattr(self, "_multi_branch", None)
        if cached is None:
            cached = branch_dimension_applies(self.tenant)
            self._multi_branch = cached
        return cached

    @property
    def onboarding(self) -> bool:
        """True while this school has not gone live."""
        from vs_tenants.models import Tenant

        return getattr(self.tenant, "status", None) == Tenant.Status.PENDING

    @property
    def active_session(self):
        """The school year this module works in. There is only ever one.

        Cached per request: the directory, the profile and the coverage screen
        all ask, and asking three times is three queries for a value that cannot
        change while the request is in flight.
        """
        if not hasattr(self, "_active_session"):
            from schools.vs_academics.models import AcademicSession, SessionStatus

            self._active_session = AcademicSession.objects.filter(
                tenant=self.tenant, status=SessionStatus.ACTIVE,
            ).first()
        return self._active_session

    def staff_queryset(self):
        """Everybody at this school who has a staff record, prefetched.

        **The queryset is people who have a StaffProfile**, which is the
        narrowing the live endpoint's own docstring asked for: it listed every
        user the tenant owned, and now that guardians carry accounts, that
        filter would turn a school's staff list into part of its roll.

        Without all five of these joins a page of twenty-five people is a page
        of queries, which the live endpoint's docstring already measured at
        fifty extra.
        """
        from django.db.models import Count, Q

        from ..models import StaffProfile

        queryset = (
            StaffProfile.objects.filter(tenant=self.tenant)
            .select_related("user", "branch")
            .prefetch_related(
                "user__tenant_role_assignments__role",
                "user__invitation",
            )
        )
        from ..services.leave import on_leave_expression

        session = self.active_session
        return queryset.annotate(
            teaching_load=Count(
                "teaching_assignments",
                filter=Q(teaching_assignments__session=session) if session else Q(),
                distinct=True,
            ),
            # Whether their leave is running, carried on every row. Annotated
            # here rather than computed per serializer, because the row, the
            # ?employment_status= filter and the header's count all have to read
            # the same answer - and the two that are querysets cannot read a
            # Python set.
            is_on_leave=on_leave_expression(),
        # Ordered explicitly rather than relying on Meta: a paginated queryset
        # with no ORDER BY returns whatever the database felt like, so page two
        # can repeat a row from page one and drop another entirely.
        ).order_by("-created_at", "-id")

    def scoped_staff(self):
        from ..services.scoping import scope_staff

        return scope_staff(self.staff_queryset(), self.request.user, self.tenant)

    def get_staff(self, pk):
        from ..services.scoping import get_staff_or_404

        return get_staff_or_404(
            self.tenant, self.request.user, pk, queryset=self.staff_queryset(),
        )

    def serializer_context(self):
        from ..services.leave import on_leave_today

        return {
            "request": self.request,
            "tenant": self.tenant,
            "multi_branch": self.multi_branch,
            "on_leave_ids": on_leave_today(self.tenant),
        }
