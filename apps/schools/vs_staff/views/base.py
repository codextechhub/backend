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

**Reading is inclusive; changing is not.** A branch administrator reads the
school-wide people and may not change them, and may post people only to their
own branches. Every write resolves its person through ``get_staff_for_write``
and every posting through ``guard_postings``, and each row carries
``can_manage`` so a screen hides the controls the server would refuse.

**A person always reaches their own record**, whatever they hold. A teacher with
no staff key at all opens her own profile, and her own documents and leave
while the school's profile policy shows them to her, which by default it does.

**A profile tab is read by key or by relationship.** A view naming a
``profile_group`` admits its GET through :meth:`StaffViewMixin.admit_profile_read`:
the key path above, or the reader's standing to the person (themselves, their
line manager, a colleague) where the school's policy grants that tab
(``services/visibility.py``). Writes are keys only.

**``pending_tenant_surface`` is declared deliberately, one view at a time.**
Absence means closed. The directory, the invite, the record and the posting are
open because "Add Staff & Invitations" is a step on the school's own onboarding
checklist, and a closed surface would leave a school unable to finish onboarding
and unable to go live. Teaching, coverage, leave, the lifecycle and the organogram are
closed: nobody resigns during onboarding, there is nothing to teach before a
session exists, and drawing the org chart is not a step of the checklist.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

from core.pagination import XVSPagination
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from ..services.scoping import viewer_sees_branches


class StaffViewMixin:
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    pagination_class = XVSPagination

    #: The profile group a view's GET serves, for a view that is one tab of a
    #: staff profile. Such a GET is admitted by :meth:`admit_profile_read`
    #: rather than by the permission classes, because it may be let in by the
    #: reader's relationship to the person as well as by a key.
    profile_group: str | None = None

    def get_permissions(self):
        """Only sign-in for a profile tab's GET; the read is admitted in the handler."""
        if self.profile_group is not None and self._is_read():
            return [IsAuthenticatedAndActive()]
        return super().get_permissions()

    def _is_read(self) -> bool:
        return (getattr(self.request, "method", "") or "").upper() in (
            "GET", "HEAD", "OPTIONS",
        )

    def admit_profile_read(self, pk):
        """One person's profile tab, if this reader may read it, and how they got in.

        Returns ``(staff, access, admission)``. Three ways in, tried in order
        (:meth:`~..services.visibility.ProfileAccess.admission`): the person
        themselves, where the school's policy gives them the tab; the key the
        view names for a read, with the person inside the reader's branch
        scope, which is the path every tab had before relationships and still
        asks the plan gate through :class:`HasRBACPermission`; and the policy,
        for a line manager or a colleague, which asks the plan gate for the same
        key so a relationship never reaches a module the school has not bought.

        Another school's id is a 404 before anything else is asked. A reader
        refused everything is told 404 when they hold the tab's key and the
        person is simply outside their branches, which is what the key path has
        always said, and 403 otherwise.
        """
        from rest_framework.exceptions import PermissionDenied

        from vs_rbac.exceptions import PlanUpgradeRequired
        from vs_rbac.plan_gate import plan_refusal

        from ..services import visibility

        group = self.profile_group
        staff = self.staff_queryset().filter(pk=pk).first()
        if staff is None:
            raise NotFound("No such person at this school.")
        access = visibility.profile_access(
            self.request, staff, self.tenant, visible=self.viewer_branches,
        )
        admission = access.admission(group)
        if admission == visibility.ADMITTED_KEY:
            if not HasRBACPermission().has_permission(self.request, self):
                raise PermissionDenied()
        elif admission == visibility.ADMITTED_RELATIONSHIP:
            refusal = plan_refusal([visibility.GROUP_PERMISSIONS[group]], self.tenant)
            if refusal:
                raise PlanUpgradeRequired(refusal)
        elif admission is None:
            if group in access.held and not access.relationships:
                raise NotFound("No such person at this school.")
            raise PermissionDenied()
        return staff, access, admission

    @staticmethod
    def refuse_as_at_unless_full(as_at, admission) -> None:
        """Refuse ``?as_at=`` to a reader let in by relationship alone.

        Reading a record as it stood on an earlier day is reading what it no
        longer says, which is for the person themselves and for the people
        whose keys reach them.
        """
        from rest_framework.exceptions import PermissionDenied

        from ..services.visibility import ADMITTED_KEY, ADMITTED_SELF

        if as_at is not None and admission not in (ADMITTED_SELF, ADMITTED_KEY):
            raise PermissionDenied(
                "Only the person themselves and people whose role reaches their "
                "record can read it as it stood on an earlier day.",
            )

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
        """Whether the branch dimension is rendered to this viewer at all.

        Per viewer, not per school: a branch administrator at a two-branch
        school works in one branch, so the posting field, column, filter and
        roster are absent for them exactly as they are at a one-branch school.
        Imports keep asking ``branch_dimension_applies`` directly, because a
        file's branch column is a fact about the school.

        Cached per request, because several serializers ask and the answer
        cannot change mid-request.
        """
        cached = getattr(self, "_multi_branch", None)
        if cached is None:
            cached = viewer_sees_branches(self.request.user, self.tenant)
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
        from ..serializers import STAFF_LIST_PREFETCH

        queryset = (
            StaffProfile.objects.filter(tenant=self.tenant)
            .select_related("user", "branch")
            .prefetch_related(*STAFF_LIST_PREFETCH)
        )
        from ..services.leave import (
            on_leave_expression,
            on_leave_until_expression,
        )

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
            # And when it ends, which is the next thing anybody asks. Carried on
            # the row so the chip can say it without a second call per person.
            on_leave_until=on_leave_until_expression(),
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

    def get_staff_for_write(self, pk):
        """One person the caller may change, not merely read.

        The read scoping first, so another branch's person is still a 404, then
        :func:`~..services.scoping.assert_manages`, so a school-wide person a
        branch administrator can see is a 403 naming why.
        """
        from ..services.scoping import assert_manages

        staff = self.get_staff(pk)
        assert_manages(self.request.user, self.tenant, staff)
        return staff

    @property
    def viewer_branches(self):
        """The caller's branch scope, resolved once for every row's ``can_manage``."""
        if not hasattr(self, "_viewer_branches"):
            from vs_rbac.scoping import visible_branch_ids

            self._viewer_branches = visible_branch_ids(self.request.user, self.tenant)
        return self._viewer_branches

    def serializer_context(self):
        from ..services.leave import on_leave_today

        return {
            "request": self.request,
            "tenant": self.tenant,
            "multi_branch": self.multi_branch,
            "viewer_branches": self.viewer_branches,
            "on_leave_ids": on_leave_today(self.tenant),
        }
