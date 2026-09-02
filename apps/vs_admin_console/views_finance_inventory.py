"""Platform oversight of who has books, deliberately not of what is in them."""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response
from vs_finance.models import LedgerEntity
from vs_rbac.permissions import HasRBACPermission, IsAuthenticatedAndActive
from vs_tenants.models import Tenant


class FinanceEntityInventoryView(APIView):
    """
    GET /admin/finance/entities/

    Every ledger entity on the platform, with the tenant that owns it.

    **This is an inventory, not a ledger.** It answers "does this school have
    books, and are they usable" - the code, currency, kind and active state -
    and nothing about the money in them. A balance, an invoice or a trial
    balance is still read by proxying a user who holds the finance permission
    at that tenant, which swaps the asserted tenant so the read is attributable
    to somebody entitled to it. See ``vs_finance.views.visible_entities``, which
    is deliberately scoped by asserted tenant and is NOT widened by this.

    The distinction is the point. A support call saying "our fees screen is
    empty" needs an answer in seconds, and today that answer is only reachable
    from a Django shell. Reading the school's actual figures to answer it is a
    different act with a different risk, and it keeps its existing route.

    Schools with NO books are listed too, with ``entities: []``. That absence is
    the most useful row on the screen: it is the difference between "their books
    are broken" and "they were never provisioned", which look identical from a
    school's side.

    Permission: platform.schools.view

    docstring-name: Finance entity inventory
    """

    permission_classes = [IsAuthenticatedAndActive & HasRBACPermission]
    rbac_permission = "platform.schools.view"

    def get(self, request):
        entities = (
            LedgerEntity.objects.select_related("tenant")
            .order_by("tenant__name", "code")
        )
        by_tenant: dict[int, list[dict]] = {}
        for entity in entities:
            by_tenant.setdefault(entity.tenant_id, []).append({
                "id": entity.id,
                "code": entity.code,
                "name": entity.name,
                "kind": entity.kind,
                "base_currency": getattr(entity.base_currency, "code", None),
                "is_active": entity.is_active,
            })

        rows = [
            {
                "tenant": {"id": t.id, "slug": t.slug, "name": t.name, "kind": t.kind,
                           "status": t.status},
                "entities": by_tenant.get(t.id, []),
                # Hoisted so a screen can sort or filter on it without walking
                # the list: this is the column somebody actually scans for.
                "has_books": bool(by_tenant.get(t.id)),
            }
            for t in Tenant.objects.order_by("name")
        ]
        return success_response(
            data=rows,
            message="Finance entity inventory retrieved successfully.",
        )
