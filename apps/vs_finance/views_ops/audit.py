"""Finance audit log read endpoints (the trail + its filter facets).
"""
from __future__ import annotations  # Defer annotation evaluation during view import.

from core.response import success_response  # Shared API success response envelope.

from ..views import resolve_entity  # Resolve active finance entity from the request.
from ..constants import FinanceAuditAction  # Audit action choices for display labels.
from ..models import (  # Import project symbols used by this module.
    FinanceAuditLog,  # Append-only finance audit log model.
)  # Close the grouped expression.
from ..serializers import (  # Import project symbols used by this module.
    FinanceAuditLogSerializer,  # Serializer for audit log rows.
)  # Close the grouped expression.


from .base import (  # Import project symbols used by this module.
    _FinanceBase,  # Shared finance ops base view with RBAC/pagination helpers.
)  # Close the grouped expression.

# --------------------------------------------------------------------------- #
# Audit trail                                                                 #
# --------------------------------------------------------------------------- #

class FinanceAuditLogListView(_FinanceBase):  # List/filter finance audit log entries.
    """GET — the append-only finance audit trail for an entity.

    Filterable by ``action``, ``status``, ``target_type``, ``actor`` (user id)
    and a ``date_from``/``date_to`` (YYYY-MM-DD, inclusive on ``created_at``).

    docstring-name: Finance audit log
    """

    rbac_permission = "finance.audit.view"  # Audit trail requires audit view permission.

    def get(self, request):  # Handle GET /finance/audit.
        entity = resolve_entity(request)  # Scope audit rows to the active entity.
        qs = FinanceAuditLog.objects.filter(entity=entity).select_related("actor")  # Base queryset with actor loaded.
        params = request.query_params  # Query parameters drive optional filters.
        if (action := params.get("action")):  # Optional action-code filter.
            qs = qs.filter(action=action)  # Narrow by audit action.
        if (status_val := params.get("status")):  # Optional audit status filter.
            qs = qs.filter(status=status_val)  # Narrow by audit status.
        if (target_type := params.get("target_type")):  # Optional target type filter.
            qs = qs.filter(target_type=target_type)  # Narrow by target type string.
        if (actor_id := params.get("actor")):  # Optional actor id filter.
            qs = qs.filter(actor_id=actor_id)  # Narrow by acting user.
        if (date_from := params.get("date_from")):  # Optional inclusive start date.
            qs = qs.filter(created_at__date__gte=date_from)  # Include rows created on/after date.
        if (date_to := params.get("date_to")):  # Optional inclusive end date.
            qs = qs.filter(created_at__date__lte=date_to)  # Include rows created on/before date.
        return self.paginate(request, qs.order_by("-id"), FinanceAuditLogSerializer)  # Return newest rows first.


class FinanceAuditFacetsView(_FinanceBase):  # Return filter facet values for audit UI.
    """GET — distinct filter options for this entity's audit trail.

    Powers the Audit Trail filter dropdowns with only the values that actually
    occur for the entity (actors, target types, actions) — cheaper and more
    useful than listing the whole ~70-value action enum.

    docstring-name: Finance audit filters
    """

    rbac_permission = "finance.audit.view"  # Facets use the same permission as audit rows.

    def get(self, request):  # Handle GET /finance/audit/facets.
        entity = resolve_entity(request)  # Scope facet values to the active entity.
        qs = FinanceAuditLog.objects.filter(entity=entity)  # Base audit queryset for facet extraction.

        actors = (  # Distinct actors that appear in the audit trail.
            qs.filter(actor__isnull=False)  # Ignore system/no-actor rows.
            .values("actor_id", "actor__email")  # Return id and email only.
            .distinct()  # Collapse duplicate actors.
            .order_by("actor__email")  # Sort for dropdown readability.
        )  # Close the grouped expression.
        target_types = (  # Distinct target type strings.
            qs.exclude(target_type="")  # Ignore blank target types.
            .values_list("target_type", flat=True)  # Return raw type strings.
            .distinct()  # Collapse duplicates.
            .order_by("target_type")  # Sort alphabetically.
        )  # Close the grouped expression.
        labels = dict(FinanceAuditAction.choices)  # Map action code to human label.
        # .order_by("action") clears the model's default -created_at ordering, which
        # would otherwise be pulled into the SELECT and break .distinct() (dup codes).  # Avoid duplicate facet values.
        action_codes = qs.order_by("action").values_list("action", flat=True).distinct()  # Distinct action codes in use.
        actions = sorted(  # Convert codes to value/label dictionaries and sort by label.
            ({"value": a, "label": labels.get(a, a)} for a in action_codes),  # Use enum label when known.
            key=lambda x: x["label"],  # Sort by human label.
        )  # Close the grouped expression.

        return success_response(  # Return facet payload.
            "Audit filters retrieved.",  # Response message.
            data={  # Filter options for UI controls.
                "actors": [{"id": a["actor_id"], "email": a["actor__email"]} for a in actors],  # Actor options.
                "target_types": list(target_types),  # Target type options.
                "actions": actions,  # Action options.
            },  # Close the grouped value.
        )  # Close the grouped expression.
