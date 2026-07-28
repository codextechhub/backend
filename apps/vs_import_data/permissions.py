"""Object-aware permission bridges for dataset-specific import-wizard flows."""
from __future__ import annotations

from vs_rbac.evaluator import has_permission
from vs_rbac.permissions import HasRBACPermission


class HasImportBatchRBACPermission(HasRBACPermission):
    """Allow the generic import key or the owning module's scoped import key.

    A finance user should not need broad ``import.*`` access merely to finish a
    bank-statement wizard. The fallback is deliberately object-aware: it applies
    only to a bank-statement batch whose typed finance context resolves to the
    request's asserted tenant.
    """

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        tenant = getattr(request, "tenant", None)
        batch_id = getattr(view, "kwargs", {}).get(
            getattr(view, "batch_lookup_url_kwarg", "batch_id"),
        )
        if tenant is None or batch_id is None:
            return False
        if not has_permission(
            request.user,
            "finance.bankaccount.import",
            tenant=tenant,
            branch=getattr(request, "branch", None),
        ):
            return False

        from .models import DatasetTypeChoices, ImportBatch

        return ImportBatch.all_objects.filter(
            pk=batch_id,
            tenant=tenant,
            dataset_type=DatasetTypeChoices.BANK_STATEMENTS,
            bank_statement_context__bank_account__entity__tenant=tenant,
        ).exists()
