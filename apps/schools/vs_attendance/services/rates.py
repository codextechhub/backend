"""The attendance rate.

Present (late included) over present plus absent plus excused, as a percentage
to one decimal place. Days with no register taken are left out of the
denominator, never counted as absences, and a suspended child is counted
nowhere. With nothing to divide by the rate is ``None``: a school that has taken
no register has no rate, which is different from a rate of zero.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def rate_percent(present: int, absent: int, excused: int):
    """The rate for these counts, or None when no register has been taken."""
    expected = present + absent + excused
    if expected == 0:
        return None
    percent = (Decimal(present) * 100 / Decimal(expected)).quantize(
        Decimal("0.1"), rounding=ROUND_HALF_UP,
    )
    return float(percent)
