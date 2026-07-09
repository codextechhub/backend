"""Finance audit log read endpoints (the trail + its filter facets).
"""
from __future__ import annotations

from core.response import success_response

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
    """GET — the append-only finance audit trail for an entity.

    Filterable by ``action``, ``status``, ``target_type``, ``actor`` (user id)
    and a ``date_from``/``date_to`` (YYYY-MM-DD, inclusive on ``created_at``).

    docstring-name: Finance audit log
    """

    rbac_permission = "finance.audit.view"  # Audit trail requires audit view permission.

    # Handle GET /finance/audit.
    def get(self, request):
        entity = resolve_entity(request)  # Scope audit rows to the active entity.
        qs = FinanceAuditLog.objects.filter(entity=entity).select_related("actor")
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


# Return filter facet values for audit UI.
class FinanceAuditFacetsView(_FinanceBase):
    """GET — distinct filter options for this entity's audit trail.

    Powers the Audit Trail filter dropdowns with only the values that actually
    occur for the entity (actors, target types, actions) — cheaper and more
    useful than listing the whole ~70-value action enum.

    docstring-name: Finance audit filters
    """

    rbac_permission = "finance.audit.view"  # Facets use the same permission as audit rows.

    # Handle GET /finance/audit/facets.
    def get(self, request):
        entity = resolve_entity(request)  # Scope facet values to the active entity.
        qs = FinanceAuditLog.objects.filter(entity=entity)

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
