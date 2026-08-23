"""The branches the caller may name.

Every app that files a row against a branch - payroll today, procurement next -
needs the same list, and until now there was none. The only branch list the API
offered was ``GET /v1/i/<slug>/branches/``: keyed by school slug, and gated on
``platform.branches.view``, a platform key. A school's own bursar holds neither,
so the one person who assigns staff to sites could not read the sites.

The rule this endpoint answers is not "what exists" but **"what would the write
path accept from me?"** - so it is derived from :func:`caller_branch_ids`, the
same function :func:`vs_rbac.scoping.raised_branch` narrows a create by. Any
other source could drift, and a picker that drifts offers a branch the save then
refuses.
"""
from __future__ import annotations

from rest_framework import generics
from rest_framework.response import Response

from core.response import success_response
from vs_rbac.permissions import IsAuthenticatedAndActive
from vs_rbac.scoping import caller_branch_ids

from .models import Branch
from .serializers import BranchOptionSerializer


class BranchOptionListView(generics.GenericAPIView):
    """``GET /v1/tenants/branches/`` - the branches the caller may work in.

    No RBAC key. The list is not a capability of its own: it is a projection of
    grants the caller already holds, and it says nothing they could not learn by
    naming a branch on a create and reading the refusal. Gating it on a key
    would mean inventing one that every branch-scoped screen then has to be
    granted, which is how ``platform.branches.view`` came to be the thing
    standing between a bursar and her own sites.

    Out-of-service branches are excluded. ``_grant_scope`` already drops them
    for a branch-pinned caller, so including them for a whole-tenant one would
    make the same closed site pickable by the bursar and invisible to the
    manager standing in it.

    docstring-name: Branches I can work in
    """
    permission_classes = [IsAuthenticatedAndActive]
    serializer_class = BranchOptionSerializer

    def get(self, request, *args, **kwargs) -> Response:
        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return success_response("Branches retrieved.", data=[])

        ids = caller_branch_ids(request)
        if ids is None:
            # Whole-tenant caller. `all_objects` deliberately: the explicit
            # tenant filter below is the boundary, and it must not depend on
            # ambient request-local state that a cross-tenant platform caller
            # has already been allowed to change.
            qs = Branch.all_objects.filter(tenant=tenant)
        elif getattr(request.user, "tenant_id", None) != tenant.pk:
            # A branch-pinned caller asserting somebody else's tenant. Their
            # grants live in their own tenant and say nothing about this one, so
            # `raised_branch` would refuse every branch here; offering any would
            # be a lie. Mirrors that refusal rather than restating it.
            qs = Branch.all_objects.none()
        else:
            qs = Branch.all_objects.filter(tenant=tenant, pk__in=ids)

        qs = qs.filter(status__in=Branch.IN_SERVICE_STATES).order_by(
            "-is_main", "name",
        )
        return success_response(
            "Branches retrieved.",
            data=BranchOptionSerializer(qs, many=True).data,
        )
