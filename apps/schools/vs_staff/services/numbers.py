"""Whether a staff ID is already somebody else's at this school.

Case does not make a different ID: "BS/stf/0001" and "BS/STF/0001" are the same
person's number typed twice, exactly as admission numbers are compared. The
database constraint (uq_staff_number_per_tenant_ci) compares the same way, and
the add form, the edit endpoint and the import all ask this one function first,
so each can say which field is wrong instead of failing at the constraint.
"""
from __future__ import annotations

from django.db.models.functions import Lower


def staff_number_taken(tenant, value, *, exclude_pk=None) -> bool:
    from ..models import StaffProfile

    value = (value or "").strip()
    if not value:
        return False
    qs = StaffProfile.all_objects.filter(tenant=tenant).annotate(
        _n=Lower("staff_number"),
    ).filter(_n=value.lower())
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()
