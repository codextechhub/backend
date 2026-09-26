"""A school's payers grouped by class, for finance's "collection by class".

Registered as ``FINANCE_PAYER_GROUP_PROVIDER`` (see :mod:`vs_finance.payer_groups`).
A payer is a child's account (``Customer.source_type`` is the student model and
``source_id`` the child), and the group is the class the child is in now. A
payer that is not a child, or a child with no active class, is left out of the
mapping rather than put in an "Unknown" group. Books that are not a school's
get ``None``.
"""
from __future__ import annotations

from vs_finance.payer_groups import PayerGrouping


def payer_classes(entity, customer_ids):
    from vs_finance.models import Customer
    from vs_tenants.models import Tenant

    from .adapters.django_finance import _class_labels

    tenant = getattr(entity, "tenant", None)
    if tenant is None or tenant.kind != Tenant.Kind.SCHOOL:
        return None
    refs = dict(
        Customer.objects.filter(
            entity=entity, id__in=customer_ids, source_type="vs_students.Student",
        ).values_list("id", "source_id")
    )
    labels = _class_labels(refs.values(), tenant.id)
    return PayerGrouping(
        label="Class",
        groups={cid: labels[ref] for cid, ref in refs.items() if labels.get(ref)},
    )
