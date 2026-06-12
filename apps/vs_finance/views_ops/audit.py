"""Finance audit log read endpoint.
"""
from __future__ import annotations



from core.response import success_response

from ..views import resolve_entity
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
    """GET — the append-only finance audit trail for an entity (filter action/status).

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
        return success_response(
            "Audit log retrieved.",
            data=FinanceAuditLogSerializer(qs[:500], many=True).data,
        )

