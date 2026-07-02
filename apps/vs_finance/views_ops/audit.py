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

class FinanceAuditLogListView(_FinanceBase):
    """GET — the append-only finance audit trail for an entity.

    Filterable by ``action``, ``status``, ``target_type``, ``actor`` (user id)
    and a ``date_from``/``date_to`` (YYYY-MM-DD, inclusive on ``created_at``).

    docstring-name: Finance audit log
    """

    rbac_permission = "finance.audit.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = FinanceAuditLog.objects.filter(entity=entity).select_related("actor")
        params = request.query_params
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


class FinanceAuditFacetsView(_FinanceBase):
    """GET — distinct filter options for this entity's audit trail.

    Powers the Audit Trail filter dropdowns with only the values that actually
    occur for the entity (actors, target types, actions) — cheaper and more
    useful than listing the whole ~70-value action enum.

    docstring-name: Finance audit filters
    """

    rbac_permission = "finance.audit.view"

    def get(self, request):
        entity = resolve_entity(request)
        qs = FinanceAuditLog.objects.filter(entity=entity)

        actors = (
            qs.filter(actor__isnull=False)
            .values("actor_id", "actor__email")
            .distinct()
            .order_by("actor__email")
        )
        target_types = (
            qs.exclude(target_type="")
            .values_list("target_type", flat=True)
            .distinct()
            .order_by("target_type")
        )
        labels = dict(FinanceAuditAction.choices)
        # .order_by("action") clears the model's default -created_at ordering, which
        # would otherwise be pulled into the SELECT and break .distinct() (dup codes).
        action_codes = qs.order_by("action").values_list("action", flat=True).distinct()
        actions = sorted(
            ({"value": a, "label": labels.get(a, a)} for a in action_codes),
            key=lambda x: x["label"],
        )

        return success_response(
            "Audit filters retrieved.",
            data={
                "actors": [{"id": a["actor_id"], "email": a["actor__email"]} for a in actors],
                "target_types": list(target_types),
                "actions": actions,
            },
        )
