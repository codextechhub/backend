"""Whose bank details a payout line shows, and who may read them.

A payout to a vendor is a copy of the vendor's own banking: the beneficiary's
name, account number and bank code are taken from the vendor record when the
line is made. The vendor record lets a school close each of those per role
(``procurement.vendor.bank_account_name``, ``.bank_account_number``,
``.bank_code``), and the payout lets it close the beneficiary fields
separately (``payments.payout``). One value in two places answers to both: a
role that cannot read the vendor's account number must not read it off the
payout either, so a vendor payout line shows a beneficiary field only when
both switches are readable.

A payout that is not to a vendor (a held settlement paid to the school's own
bank account) copies nothing from a vendor and follows ``payments.payout``
alone.

Every surface that restates a beneficiary applies the same rule through the
helpers here: the payout line itself (:func:`trim_vendor_beneficiary`), the
movements feed, the approval document's beneficiary table
(:func:`beneficiary_access`) and the payout export
(:func:`export_beneficiary_annotations`).
"""
from __future__ import annotations

from django.db.models import BooleanField, Case, Value, When

from vs_rbac.field_enforcement import can_read

#: The vendor type a vendor-backed payout line records as its source.
VENDOR_SOURCE_TYPE = "vs_procurement.Vendor"

#: Each beneficiary field of a payout, and the vendor field it is copied from.
VENDOR_BANK_FIELDS = {
    "beneficiary_name": "procurement.vendor.bank_account_name",
    "beneficiary_account_number": "procurement.vendor.bank_account_number",
    "beneficiary_bank_code": "procurement.vendor.bank_code",
}


def is_vendor_payout(payout) -> bool:
    """Whether *payout* pays a vendor, and so carries the vendor's banking."""
    return payout.vendor_source_type == VENDOR_SOURCE_TYPE


def trim_vendor_beneficiary(request, data: dict, payout) -> dict:
    """*data*, a serialised payout line, without the vendor banking its caller may not read."""
    if request is None or not is_vendor_payout(payout):
        return data
    for name, vendor_key in VENDOR_BANK_FIELDS.items():
        if name in data and not can_read(request, vendor_key):
            data.pop(name)
    return data


def payout_key(name: str) -> str:
    """The ``payments.payout`` registry key of the beneficiary field *name*."""
    return f"payments.payout.{name}"


def beneficiary_access(name: str, *, vendor_backed: bool) -> tuple:
    """Every registry key a reader needs to read the beneficiary field *name*.

    The payout's own switch always, and the matching vendor bank switch as well
    when the line pays a vendor.
    """
    keys = (payout_key(name),)
    return keys + (VENDOR_BANK_FIELDS[name],) if vendor_backed else keys


def may_read_beneficiary(readable, name: str, *, vendor_backed: bool) -> bool:
    """Whether *readable*, a predicate over registry keys, opens the field *name*."""
    return all(readable(key) for key in beneficiary_access(name, vendor_backed=vendor_backed))


def vendor_backed_expression():
    """A database expression that is true on a payout line paying a vendor."""
    return Case(
        When(vendor_source_type=VENDOR_SOURCE_TYPE, then=Value(True)),
        default=Value(False), output_field=BooleanField(),
    )


def export_beneficiary_annotations(readable) -> dict:
    """Annotations that blank each vendor line's beneficiary values *readable* cannot open.

    The export engine drops a column its reader cannot read at all
    (the column's own ``payments.payout`` key). The vendor switch differs per
    row, so the value is cut in the query: on a line that pays a vendor whose
    matching bank switch is closed to the reader the cell is empty, and on any
    other line it is the stored value. A sort on the column ranks the cut value,
    never the hidden one. ``readable=None`` judges nobody and keeps every value.
    """
    from django.db.models import CharField, F

    out = {}
    for name, vendor_key in VENDOR_BANK_FIELDS.items():
        column = f"export_{name}"
        if readable is None or readable(vendor_key):
            out[column] = F(name)
        else:
            out[column] = Case(
                When(vendor_source_type=VENDOR_SOURCE_TYPE, then=Value("")),
                default=F(name), output_field=CharField(),
            )
    return out
