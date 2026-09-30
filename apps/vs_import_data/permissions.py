from collections.abc import Iterable

from vs_rbac.permissions import HasRBACPermission, has_permission

from .constants import ImportPermission
from .scoping import batch_branch_q

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


#: The engine keys a dataset's own import key may stand in for.
#:
#: These are the steps of taking one file through the wizard: reading the batch,
#: its file, its issues and its jobs, validating it, starting the import, and
#: abandoning it before anything is written. ``batches.create`` is here for that
#: last step only: cancel names it, and the cancel view itself confines a caller
#: without ``batches.update`` or ``batches.delete`` to batches they uploaded.
#:
#: Everything else the engine guards unwinds or administers data already
#: written - rewriting or deleting a batch, resolving an issue in place, rolling
#: back, and the rollback history, audit and notification feeds - and needs the
#: engine's own key for that action, unless the dataset declares that key in
#: ``_DATASET_EXTRA_ENGINE_KEYS``. A school corrects an import by uploading a
#: fixed file; a rollback takes every imported row off again and is a support
#: action, not a step of the wizard.
_WIZARD_KEYS: frozenset[str] = frozenset({
    ImportPermission.BATCH_VIEW,
    ImportPermission.BATCH_CREATE,
    ImportPermission.BATCH_VALIDATE,
    ImportPermission.BATCH_IMPORT,
    ImportPermission.VALIDATION_VIEW,
    ImportPermission.JOB_VIEW,
})


#: ``dataset_type`` -> engine keys past the wizard that its own key also covers.
#:
#: Empty for a dataset that is not listed, which is every school dataset: a
#: registrar corrects a roll by uploading a fixed file, and taking 400 children
#: off it again stays a support action.
#:
#: Bank statements declare the rollback. Finance refuses to edit a
#: bulk-imported statement line by line, because the lines must stay the ones
#: the file carried, and sends the bursar to roll the statement back and import
#: it again. Without the rollback, a bursar who loads March into the April
#: account has no way to correct it. The finance rollback itself refuses once
#: any line of the statement has been matched, ignored or posted, so this
#: removes only statements nothing has touched yet. The rollback history read
#: is not declared: the job detail already carries the rollback's start and
#: finish, and no finance screen reads the history.
_DATASET_EXTRA_ENGINE_KEYS: dict[str, frozenset[str]] = {
    "bank_statements": frozenset({ImportPermission.ROLLBACK_RUN}),
}


def register_dataset_import_key(
    dataset_type: str,
    permission_key: str,
    *,
    extra_engine_keys: Iterable[str] = (),
) -> None:
    """Let *permission_key* stand in for the engine's keys on this dataset.

    By default it covers the wizard alone (see ``_WIZARD_KEYS``).
    *extra_engine_keys* names engine keys past the wizard that the owning module
    has decided its own key should also reach, on batches of this dataset and
    no other; a key that unwinds or erases data belongs here only when the
    module has no other way to correct an import (see
    ``_DATASET_EXTRA_ENGINE_KEYS``).

    Idempotent by dataset type, so a second ``ready()`` - Django calls it once
    per process, but test runners and management commands can re-enter -
    replaces rather than accumulates, the extra keys included.
    """
    _DATASET_IMPORT_KEYS[dataset_type] = permission_key
    extra = frozenset(extra_engine_keys)
    if extra:
        _DATASET_EXTRA_ENGINE_KEYS[dataset_type] = extra
    else:
        _DATASET_EXTRA_ENGINE_KEYS.pop(dataset_type, None)


def _stand_in_keys(dataset_type: str) -> frozenset[str]:
    """The engine keys a dataset's own import key covers on its batches."""
    return _WIZARD_KEYS | _DATASET_EXTRA_ENGINE_KEYS.get(dataset_type, frozenset())


def _any_stand_in_keys() -> frozenset[str]:
    """Every engine key some dataset's import key covers on some batch."""
    return _WIZARD_KEYS.union(*_DATASET_EXTRA_ENGINE_KEYS.values())


class HasImportBatchRBACPermission(HasRBACPermission):
    """Allow the engine's import key, or the owning module's key where it stands in.

    A finance user should not need broad ``import.*`` access merely to finish a
    bank-statement wizard, and a school administrator should not need it to
    load their students. The module key stands in only where the view asks for
    one of ``_WIZARD_KEYS``, or for one of the extra engine keys its dataset
    declares (``_DATASET_EXTRA_ENGINE_KEYS``: the rollback, for bank statements
    alone). A view guarded by a key no dataset declares (delete, update, issue
    resolution, the rollback, audit and notification feeds) is refused here
    before any lookup, so it answers the same for every batch id. A declared
    extra key is checked against the batch's own dataset after the lookup, so
    the bank-statement key rolls back a statement and never a student roll.

    The fallback stays deliberately object-aware: it applies only to a batch of
    the dataset the key belongs to, resolved against the request's asserted
    tenant and against the branches the caller is entitled to work in, so
    holding one module's import key never opens another module's file and never
    reaches across branches.

    The branch narrowing is the same rule the views apply when they resolve a
    batch, spelled again here because this runs first and on its own: an
    unreachable batch must be refused at the gate rather than admitted to a
    lookup that then has to catch it. Both halves therefore answer alike, and
    a caller who reaches this fallback cannot tell an id they may not see from
    one that was never there - each is the same refusal.
    """

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        required = getattr(view, "rbac_permission", None)
        if isinstance(required, str):
            required = [required]
        if not required or _any_stand_in_keys().isdisjoint(required):
            return False

        tenant = getattr(request, "tenant", None)
        batch_id = getattr(view, "kwargs", {}).get(
            getattr(view, "batch_lookup_url_kwarg", "batch_id"),
        )
        if tenant is None or batch_id is None:
            return False

        from .models import ImportBatch

        batch = ImportBatch.all_objects.filter(
            batch_branch_q(request), pk=batch_id, tenant=tenant,
        ).first()
        if batch is None:
            return False

        if _stand_in_keys(batch.dataset_type).isdisjoint(required):
            return False

        key = _DATASET_IMPORT_KEYS.get(batch.dataset_type)
        if key is None or not has_permission(request.user, key, tenant=tenant):
            return False

        # Bank statements keep their extra containment check: the batch's typed
        # finance context must resolve to the asserted tenant too, because the
        # batch row alone does not prove which set of books it touches.
        if batch.dataset_type == "bank_statements":
            return ImportBatch.all_objects.filter(
                batch_branch_q(request),
                pk=batch_id, tenant=tenant,
                bank_statement_context__bank_account__entity__tenant=tenant,
            ).exists()
        return True
