"""Wiring every view in this module shares.

**Tenant scoping is the view's job.** Every lookup is made on ``request.tenant``
and a structure, scale, class or student of another school answers 404, never
403, so identifiers cannot be enumerated.

**No view declares ``pending_tenant_surface``.** Results are the work of a live
school: a school that has not gone live has no sheets, so every route here
answers ``TENANT_NOT_LIVE`` for a pending school, which is the closed default
and the right one.

**A term is ``?term=<id>``.** Absent, it is the school's current term by date
(:func:`..services.resolve.current_term`). A term of another school, or one that
does not exist, answers 404.

**Setup is shared configuration.** A structure or a scale with no branch applies
to every branch, so a caller bound to some branches reads those and the
school-wide ones and cannot change any of them: a write needs a whole-school
reach, as the school's attendance settings do.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

from schools.vs_academics.models import AcademicTerm
from vs_config.clock import tenant_today
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_rbac.scoping import assert_caller_may_configure, branch_q, caller_may_use_branch
from vs_tenants.references import find_branch_in_tenant

from ..services import resolve

ONLY_WHOLE_SCHOOL = (
    "Only a school-wide administrator can change assessment setup, because it "
    "applies across branches."
)


class GradebookViewMixin:
    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]

    @property
    def tenant(self):
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            # Fail closed: an unscoped lookup looks like a working endpoint
            # until somebody reads another school's rows.
            raise NotFound("No school in context.")
        return tenant

    def branch_scope(self):
        """The ``Q`` narrowing a branch-owned row to those the caller can see; shared rows (no branch) stay."""
        return branch_q(self.request)

    def assert_may_configure(self):
        assert_caller_may_configure(self.request.user, self.tenant, message=ONLY_WHOLE_SCHOOL)

    def term_from_query(self):
        """The term named in ``?term=``, or the school's current term when none is named."""
        raw = self.request.query_params.get("term")
        if raw in (None, ""):
            term = resolve.current_term(self.tenant, tenant_today(self.tenant))
            if term is None:
                raise NotFound("This school has no term yet.")
            return term
        term = (
            AcademicTerm.objects.filter(tenant=self.tenant, pk=int(raw)).first()
            if str(raw).isdigit() and int(raw) < 2**62 else None
        )
        if term is None:
            raise NotFound("No such term at this school.")
        return term

    def branch_from_query(self):
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
