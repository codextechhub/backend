"""Which import batches a caller may read: the branch reading, per dataset.

A batch is filed under the branch it was uploaded for, and most batches are read
inclusively: a student roll uploaded for the whole school, with no branch, belongs
to every branch and stays visible from each of them.

A dataset whose rows are money records reads its batches the other way. A bank
statement belongs to its bank account's branch, and a bank account is read
exclusively like every transaction (:func:`vs_rbac.scoping.transaction_branch_scope`):
the Lekki bursar sees Lekki's statement imports and never Ikeja's, nor one for an
account nobody has given a branch. Such a batch is read by its account's branch,
whatever branch the batch row itself carries, so a statement follows its account
when the account is placed.

Each such dataset names the route from :class:`~vs_import_data.models.ImportBatch`
to the row whose branch it follows. The finance pair is written here rather than
registered from ``vs_finance``, as :mod:`vs_import_data.permissions` does with its
import keys, so it holds even if that app's ``ready()`` is never reached.
"""
from __future__ import annotations

from django.db.models import Q

from vs_rbac.scoping import branch_scope

#: ``dataset_type`` -> the route from a batch to the row whose branch it follows.
_TRANSACTION_DATASETS: dict[str, str] = {
    "bank_statements": "bank_statement_context__bank_account__",
}


def register_transaction_dataset(dataset_type: str, branch_route: str) -> None:
    """Read *dataset_type*'s batches exclusively, by the branch at *branch_route*.

    *branch_route* ends in ``__`` and leads from a batch to a row with a
    ``branch``. Idempotent: a second registration replaces the first.
    """
    _TRANSACTION_DATASETS[dataset_type] = branch_route


def batch_branch_q(request):
    """The batches the caller behind *request* may read, as a ``Q``.

    Empty for a caller nothing narrows. Otherwise: a batch of an ordinary dataset
    filed under one of the caller's branches or under none, and a batch of a
    transaction dataset whose row (the bank account, for a statement) is one of
    the caller's branches'.
    """
    scope = branch_scope(request, include_shared=True)
    if not scope.is_narrowed:
        return Q()
    exclusive = scope.__class__(scope.branch_ids, include_shared=False)
    q = ~Q(dataset_type__in=tuple(_TRANSACTION_DATASETS)) & scope.q()
    for dataset_type, route in _TRANSACTION_DATASETS.items():
        q |= Q(dataset_type=dataset_type) & exclusive.q(route)
    return q
