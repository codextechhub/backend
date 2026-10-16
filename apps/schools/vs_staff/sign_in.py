"""How a staff number signs somebody in.

A staff number is the school's own label for an employee (``BFS/STF/0012``),
unique inside one school and nowhere else. The account engine accepts it in the
sign-in box through the resolver registry in
:mod:`vs_user.services.sign_in_identifiers`; this module is the resolver this
app registers from ``AppConfig.ready()``.
"""
from __future__ import annotations


def staff_number_resolver(*, tenant_id, identifier):
    """The accounts whose staff record carries *identifier* inside *tenant_id*.

    Case is ignored, as the stored uniqueness (``uq_staff_number_per_tenant_ci``)
    ignores it, so a school holds at most one row per number; at most two rows
    are read so an ambiguous number is still detected. A blank identifier never
    matches, since a blank staff number means the member of staff has none.

    Only an employee's own row is consulted, through ``all_objects`` with the
    tenant named explicitly: there is no ambient tenant before sign-in.
    """
    from .models import StaffProfile

    number = (identifier or "").strip()
    if not number:
        return []
    rows = (
        StaffProfile.all_objects.select_related("user__tenant")
        .filter(tenant_id=tenant_id, staff_number__iexact=number)[:2]
    )
    return [row.user for row in rows]


def register():
    """Tell the account engine this app issues staff numbers."""
    from vs_user.services.sign_in_identifiers import register_identifier_resolver

    register_identifier_resolver("staff_number", staff_number_resolver)
