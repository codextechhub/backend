"""Wiring every view in this module shares.

**Tenant scoping is the view's job.** Every lookup is made on ``request.tenant``
and a class, student or register of another school answers 404, never 403, so
identifiers cannot be enumerated. A class in a branch the caller cannot see
answers 404 as well (:mod:`..services.access`).

**No view declares ``pending_tenant_surface``.** Attendance is the daily work of
a live school: a school that has not gone live has no students placed in
classes and nothing to register, so every route here answers ``TENANT_NOT_LIVE``
for a pending school, which is the closed default and the right one.

**Dates travel in the path** as ``YYYY-MM-DD`` and are parsed once, here, so a
malformed one is a 400 with a sentence rather than a 404 from the router.
"""
from __future__ import annotations

import datetime as dt

from rest_framework.exceptions import NotFound

from core.pagination import XVSPagination
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import caller_may_use_branch
from vs_tenants.references import find_branch_in_tenant

from ..exceptions import InvalidDate
from ..serializers import RegisterSerializer
from ..services.registers import RegisterContext, load_context, register_payload


class AttendanceViewMixin:
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def tenant(self):
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            # Fail closed: an unscoped lookup looks like a working endpoint
            # until somebody reads another school's rows.
            raise NotFound("No school in context.")
        return tenant

    @staticmethod
    def parse_date(raw: str) -> dt.date:
        try:
            return dt.date.fromisoformat(str(raw))
        except ValueError:
            raise InvalidDate() from None

    def optional_date(self, name="date"):
        """The date in the query string, or None when the caller named none."""
        raw = self.request.query_params.get(name)
        return self.parse_date(raw) if raw else None

    def branch_filter(self):
        """The branch named in ``?branch=``, or None for every branch the caller sees.

        A branch of another school and one the caller cannot see answer the same
        404 as a branch that does not exist, so the parameter cannot be used to
        learn which ids are in use.
        """
        raw = self.request.query_params.get("branch")
        if not raw:
            return None
        branch = find_branch_in_tenant(self.tenant, raw)
        if branch is None or not caller_may_use_branch(self.request, branch):
            raise NotFound("No such branch at this school.")
        return branch

    def paginated(self, queryset, render):
        """One page of *queryset*, each row passed through *render*, in the standard envelope."""
        paginator = XVSPagination()
        page = paginator.paginate_queryset(queryset, self.request, view=self)
        return paginator.get_paginated_response(render(page))

    def register_context(self, class_id, raw_date) -> RegisterContext:
        return self.load_context_for(class_id, self.parse_date(raw_date))

    def load_context_for(self, class_id, on_date: dt.date) -> RegisterContext:
        return load_context(self.tenant, self.request.user, class_id, on_date)

    def serialize(self, ctx: RegisterContext) -> dict:
        """The register as the caller may read it."""
        return dict(RegisterSerializer(
            register_payload(ctx), context={"request": self.request},
        ).data)

    def reread(self, ctx: RegisterContext) -> dict:
        """The register re-read after a write, so it shows what was just written."""
        return self.serialize(load_context(
            ctx.tenant, self.request.user, ctx.school_class.pk, ctx.date,
        ))
