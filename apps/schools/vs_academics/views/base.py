"""Wiring every view in this module shares.

Three things, all of which the ship-check asks about by name.

**Tenant scoping is the view's job.** Every queryset is filtered on
``request.tenant`` and a row belonging to another tenant answers 404, never
403, so tenant identifiers cannot be enumerated. ``TenantAwareManager`` already
does this eagerly, but the view asserts it anyway, because the manager is
bypassed by ``all_objects`` and by related traversal.

**Every view declares ``pending_tenant_surface``.** A school builds its academic
structure while it is still PENDING - it is a required onboarding task - so
without the declaration every call here answers 403 TENANT_NOT_LIVE and the
school can never go live. Absence means closed, deliberately, so a view added
later is not admitted by default; that is why it is declared per view and
asserted by a test that enumerates the URL conf.

**The branch dimension is resolved once per request**, not once per row.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.permissions import SAFE_METHODS

from core.pagination import XVSPagination
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive

from ..services.scoping import branch_dimension_applies


class AcademicsViewMixin:
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    pending_tenant_surface = True
    pagination_class = XVSPagination

    @property
    def tenant(self):
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            # Fail closed. A missing tenant context yields nothing rather than
            # an unscoped queryset, which is the failure that looks like a
            # working endpoint until somebody reads another school's rows.
            raise NotFound("No school in context.")
        return tenant

    @property
    def multi_branch(self) -> bool:
        cached = getattr(self, "_multi_branch", None)
        if cached is None:
            cached = branch_dimension_applies(self.tenant)
            self._multi_branch = cached
        return cached

    @property
    def session(self):
        """The academic year this request is about.

        `?session=<id>` when the screen names one, else the school's ACTIVE
        year. Never "all years": levels, classes and subjects belong to exactly
        one, so a list without a year is a list of several years' rows piled on
        top of each other - which is what this module used to show.

        On a WRITE it must also be a year that may still be written to. An
        archived year is last year's record, and the whole point of giving the
        structure a year was that reading 2025/2026 back shows what actually
        happened - which is not true if today's admin can still add a class to
        it. Refused here rather than in each view, because there are eleven
        write paths and the one that forgets is the one that rewrites history.
        """
        cached = getattr(self, "_session", None)
        if cached is not None:
            return cached

        from ..exceptions import NoSessionYet
        from ..models import AcademicSession, SessionStatus
        from ..services.years import assert_year_is_writable

        raw = str(self.request.query_params.get("session") or "").strip()
        if raw:
            found = AcademicSession.objects.filter(tenant=self.tenant, pk=raw).first()
            if found is None:
                raise NotFound("No such session at this school.")
        else:
            found = (
                AcademicSession.objects.filter(
                    tenant=self.tenant, status=SessionStatus.ACTIVE,
                ).first()
                or AcademicSession.objects.filter(tenant=self.tenant)
                .order_by("-start_date").first()
            )
            if found is None:
                raise NoSessionYet()
        if self.request.method not in SAFE_METHODS:
            assert_year_is_writable(found)
        self._session = found
        return found

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["multi_branch"] = self.multi_branch
        return context
