"""What a stock transfer between branches shows to somebody who cannot read its inter-branch twin.

A transfer between two branches' stores books an inter-branch goods transfer in
finance: the value moved, the narration, the reference, the date, the two leg
journals and the document itself are the same facts there and on the stock
movement. Finance lets a school close each of them per role, so a role that
cannot read the amount on the inter-branch screen must not read the same naira
figure off the movement it came from, nor off the running balance that the
movement left behind (the step in the balance is the amount).

Each movement key below is held to the finance switch that governs the same
value, on top of whatever governs the stock ledger itself: a value shows only
when every switch that governs it is readable. A transfer between two stores of
one branch posts nothing and has no inter-branch twin, so it follows the stock
ledger alone.

The other half of a transfer is the receiving store. Its movement carries that
store's code and the balance it holds after the goods arrive, which belong to
another branch's stock; the sender is shown that leg only when the store is one
their branch reach lets them read, and otherwise only what was handed over.
"""
from __future__ import annotations

from vs_rbac.field_enforcement import can_read

from .constants import StockMovementType

_INTERBRANCH = "finance.interbranch"

#: The movement keys that restate one inter-branch field, by the field's name.
TRANSFER_LEG_KEYS: dict[str, tuple[str, ...]] = {
    "amount": ("value_amount", "value_amount_naira", "balance_value", "balance_value_naira"),
    "purpose": ("narration",),
    "reference": ("reference",),
    "transaction_date": ("movement_date",),
    "journals": ("journal_id",),
}

#: What the receiving store's movement exposes beyond what the sender handed over.
RECEIVING_STORE_KEYS = (
    "location_id", "location_code", "balance_qty", "balance_value",
    "balance_value_naira", "journal_id", "grn_id",
)


def is_booked_transfer_leg(movement) -> bool:
    """Whether *movement* is a leg of a transfer that booked an inter-branch document."""
    return (
        movement.movement_type == StockMovementType.TRANSFER
        and getattr(movement, "inter_branch_transfer_id", None) is not None
    )


def interbranch_readable(request, name: str) -> bool:
    """Whether the caller may read the inter-branch field called *name*."""
    return can_read(request, f"{_INTERBRANCH}.{name}")


def trim_transfer_leg(request, data: dict, movement) -> dict:
    """*data*, a serialised movement, without the values its inter-branch twin hides."""
    if request is None or not is_booked_transfer_leg(movement):
        return data
    for name, keys in TRANSFER_LEG_KEYS.items():
        if not interbranch_readable(request, name):
            for key in keys:
                data.pop(key, None)
    return data


def trim_receiving_leg(data: dict) -> dict:
    """The receiving store's movement as a sender outside that store's reach may see it."""
    return {key: value for key, value in data.items() if key not in RECEIVING_STORE_KEYS}
