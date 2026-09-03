"""The branches the caller may name.

Every app that files a row against a branch, payroll and procurement among
them, needs the same list, and it has to be the list the write path will
accept. The endpoint answers "what would a create accept from me?" rather than
"what exists": it derives from :func:`caller_branch_ids`, the same function
:func:`vs_rbac.scoping.raised_branch` narrows a create by. Any other source
would drift, and a picker that drifts offers a branch the save then refuses.
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
    granted, and a platform key such as ``platform.branches.view`` stands
    between a school's own bursar and the sites she assigns staff to.

    Three shapes of caller are answered differently:

    - whole-tenant, filtered on the asserted tenant alone;
    - branch-pinned inside that tenant, narrowed to the branches they hold;
    - branch-pinned in a different tenant, given nothing, because their grants
      live in their own tenant and ``raised_branch`` would refuse every branch
      here. Offering any would be a lie.

    ``all_objects`` throughout: the explicit tenant filter is the boundary, and
    it must not depend on ambient request-local state that a cross-tenant
    platform caller has already been allowed to change.

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
            # Whole-tenant caller.
            qs = Branch.all_objects.filter(tenant=tenant)
        elif getattr(request.user, "tenant_id", None) != tenant.pk:
            # Branch-pinned caller asserting somebody else's tenant.
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
