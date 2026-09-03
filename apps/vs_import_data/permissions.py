from vs_rbac.permissions import HasRBACPermission, has_permission

#: ``dataset_type`` -> the owning module's own import key.
#:
#: A domain app registers its pair from ``AppConfig.ready``; this engine
#: imports nothing. The direction matters: a hard-coded entry here refuses
#: the wizard to every module added afterwards however its key is granted,
#: and that failure reads as a seeding problem rather than as a branch
#: nobody extended.
_DATASET_IMPORT_KEYS: dict[str, str] = {
    # Finance's own pair, kept here rather than registered from vs_finance so
    # it holds even if that app's ready() is never reached.
    "bank_statements": "finance.bankaccount.import",
}


def register_dataset_import_key(dataset_type: str, permission_key: str) -> None:
    """Let *permission_key* stand in for the generic import keys on this dataset.

    Idempotent by dataset type, so a second ``ready()`` - Django calls it once
    per process, but test runners and management commands can re-enter -
    replaces rather than accumulates.
    """
    _DATASET_IMPORT_KEYS[dataset_type] = permission_key


class HasImportBatchRBACPermission(HasRBACPermission):
    """Allow the generic import key or the owning module's scoped import key.

    A finance user should not need broad ``import.*`` access merely to finish a
    bank-statement wizard, and a school administrator should not need it to
    load their students. The fallback stays deliberately object-aware: it
    applies only to a batch of the dataset the key belongs to, resolved against
    the request's asserted tenant, so holding one module's import key never
    opens another module's file.
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

        from .models import ImportBatch

        batch = ImportBatch.all_objects.filter(pk=batch_id, tenant=tenant).first()
        if batch is None:
            return False

        key = _DATASET_IMPORT_KEYS.get(batch.dataset_type)
        if key is None or not has_permission(request.user, key, tenant=tenant):
            return False

        # Bank statements keep their extra containment check: the batch's typed
        # finance context must resolve to the asserted tenant too, because the
        # batch row alone does not prove which set of books it touches.
        if batch.dataset_type == "bank_statements":
            return ImportBatch.all_objects.filter(
                pk=batch_id, tenant=tenant,
                bank_statement_context__bank_account__entity__tenant=tenant,
            ).exists()
        return True
