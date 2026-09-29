"""The pupil on the roll, offered to the branch backfill as a customer's branch.

A school's AR customer is opened for a child and names the child only through
its loose ``source_type`` and ``source_id``. When the customer record itself
carries no branch, the child's own branch on the roll is where the account's
fees belong, the same default :meth:`DjangoStudentCustomerAdapter.ensure_customer`
applies when it opens an account.

The engine cannot read the roll (engines never import a school app), so the FAL
registers this lookup with it. Discovered by :mod:`vs_finance.branch_derivation`
on first use.

Only a child of the customer's own tenant answers: a source id that happens to
name another school's pupil is no answer at all.
"""
from __future__ import annotations

from vs_finance.branch_derivation import register_customer_source

from .contracts import SOURCE_TYPE_STUDENT

PUPIL_ON_THE_ROLL = "the pupil on the roll"


def pupil_branches(ctx, customer_ids):
    """``{customer_id: branch_id}`` for customers opened for a child of ``ctx``'s tenant."""
    from schools.vs_students.models import Student
    from vs_finance.models import Customer

    pupils: dict[int, int] = {}
    rows = Customer._base_manager.filter(
        pk__in=customer_ids, entity=ctx.entity, source_type=SOURCE_TYPE_STUDENT,
    ).values_list("pk", "source_id")
    for customer_id, source_id in rows:
        try:
            pupils[customer_id] = int(source_id)
        except (TypeError, ValueError):
            continue
    if not pupils:
        return {}
    branches = dict(
        Student.all_objects.filter(pk__in=set(pupils.values()), tenant_id=ctx.tenant.pk)
        .values_list("pk", "branch_id")
    )
    return {cid: branches[sid] for cid, sid in pupils.items() if branches.get(sid) is not None}


register_customer_source(PUPIL_ON_THE_ROLL, pupil_branches)
