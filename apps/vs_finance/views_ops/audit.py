"""Finance audit log read endpoints: the trail, one entry, and the trail's filter facets.

Every read here is narrowed to the caller's branches the way every transaction
read is (:func:`vs_rbac.scoping.transaction_branch_q`), because an entry is
about a document and carries that document's branch
(:class:`~vs_finance.models.FinanceAuditLog`). Lagoon View's Lekki bursar,
Ngozi, reads Lekki's entries only: their share of a central payroll run and not
Ikeja's, and no entry without a branch, since nothing on such an entry says
whose it is. The narrowing is applied before any filter, count or facet, so a
filter cannot find, and a count or a facet cannot hint at, an entry the list
would not show. A whole-school reader is not narrowed.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound

from core.response import success_response
from vs_rbac.scoping import transaction_branch_q

from ..views import resolve_entity
from ..constants import FinanceAuditAction
from ..models import (
    FinanceAuditLog,
)
from ..serializers import (
    FinanceAuditLogSerializer,
)


from .base import (
    _FinanceBase,
)

# --------------------------------------------------------------------------- #
# Audit trail                                                                 #
# --------------------------------------------------------------------------- #

# List/filter finance audit log entries.
class FinanceAuditLogListView(_FinanceBase):
    """GET - the append-only finance audit trail for an entity, in the caller's branches.

    Filterable by ``action``, ``status``, ``target_type``, ``actor`` (user id)
    and a ``date_from``/``date_to`` (YYYY-MM-DD, inclusive on ``created_at``).

    docstring-name: Finance audit log
    """

    rbac_permission = "finance.audit.view"  # Audit trail requires audit view permission.

    # Handle GET /finance/audit.
    def get(self, request):
        entity = resolve_entity(request)  # Scope audit rows to the active entity.
        qs = FinanceAuditLog.objects.filter(
            transaction_branch_q(request), entity=entity,
        ).select_related("actor", "effective_user", "branch")
        params = request.query_params  # Query parameters drive optional filters.
        if (action := params.get("action")):
            qs = qs.filter(action=action)
        if (status_val := params.get("status")):
            qs = qs.filter(status=status_val)
        if (target_type := params.get("target_type")):
            qs = qs.filter(target_type=target_type)
        if (actor_id := params.get("actor")):
            qs = qs.filter(actor_id=actor_id)
        if (date_from := params.get("date_from")):
            qs = qs.filter(created_at__date__gte=date_from)
        if (date_to := params.get("date_to")):
            qs = qs.filter(created_at__date__lte=date_to)
        return self.paginate(request, qs.order_by("-id"), FinanceAuditLogSerializer)


class FinanceAuditLogDetailView(_FinanceBase):
    """GET - one entry of the finance audit trail.

    An entry outside the caller's branches, or with no branch at all for a
    branch-bound caller, is the same 404 as an id that does not exist, so the
    address says nothing about whether another branch's entry is there.

    docstring-name: Finance audit entry
    """

    rbac_permission = "finance.audit.view"

    def get(self, request, pk):
        entity = resolve_entity(request)
        entry = (
            FinanceAuditLog.objects.filter(transaction_branch_q(request), entity=entity, pk=pk)
            .select_related("actor", "effective_user", "branch")
            .first()
        )
        if entry is None:
            raise NotFound("No such audit entry in this entity.")
        return success_response(
            "Audit entry retrieved.",
            data=FinanceAuditLogSerializer(entry, context={"request": request}).data,
        )


# Return filter facet values for audit UI.
class FinanceAuditFacetsView(_FinanceBase):
    """GET - distinct filter options for this entity's audit trail.

    Powers the Audit Trail filter dropdowns with only the values that actually
    occur in the entries the caller can read (actors, target types, actions) -
    cheaper and more useful than listing the whole ~70-value action enum, and
    never an option only another branch's entries carry.

    docstring-name: Finance audit filters
    """

    rbac_permission = "finance.audit.view"  # Facets use the same permission as audit rows.

    # Handle GET /finance/audit/facets.
    def get(self, request):
        entity = resolve_entity(request)  # Scope facet values to the active entity.
        qs = FinanceAuditLog.objects.filter(transaction_branch_q(request), entity=entity)

        actors = (  # Distinct actors that appear in the audit trail.
            qs.filter(actor__isnull=False)
            .values("actor_id", "actor__email")
            .distinct()  # Collapse duplicate actors.
            .order_by("actor__email")
        )
        target_types = (  # Distinct target type strings.
            qs.exclude(target_type="")
            .values_list("target_type", flat=True)
            .distinct()  # Collapse duplicates.
            .order_by("target_type")
        )
        labels = dict(FinanceAuditAction.choices)  # Map action code to human label.
        # .order_by("action") clears the model's default -created_at ordering, which
        # would otherwise be pulled into the SELECT and break .distinct() (dup codes).  # Avoid duplicate facet values.
        action_codes = qs.order_by("action").values_list("action", flat=True).distinct()
        actions = sorted(  # Convert codes to value/label dictionaries and sort by label.
            ({"value": a, "label": labels.get(a, a)} for a in action_codes),
            key=lambda x: x["label"],  # Sort by human label.
        )

        return success_response(
            "Audit filters retrieved.",  # Response message.
            data={  # Filter options for UI controls.
                "actors": [{"id": a["actor_id"], "email": a["actor__email"]} for a in actors],  # Actor options.
                "target_types": list(target_types),  # Target type options.
                "actions": actions,  # Action options.
            },
        )
