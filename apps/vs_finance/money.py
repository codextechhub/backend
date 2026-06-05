"""Money handling for the finance engine.

NON-NEGOTIABLE RULE
-------------------
Every monetary value in finance/procurement is stored as an **integer number of
minor units (kobo)** — never a float, never a scaled Decimal column. ₦1,250.50 is
stored as the integer ``125050``.

Floats cannot represent decimal currency exactly (0.1 + 0.2 != 0.3), so they must
never touch a balance, a journal line, a price or a tax computation. The design
HTML uses JS floats; values crossing the API boundary are converted to kobo here,
once, at the edge — and stay integers everywhere inside the backend.

Use :func:`to_kobo` / :func:`to_naira` to convert at the boundary, and
:class:`MoneyField` for every monetary model column.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Union

from django.db import models

#: Minor units in one major unit. NGN, like most currencies, has 100.
KOBO_PER_NAIRA = 100

#: Quantiser for 2-decimal-place naira rounding.
_NAIRA_QUANT = Decimal("0.01")

Number = Union[int, str, Decimal, float]


def to_kobo(amount: Number) -> int:
    """Convert a major-unit amount (naira) to integer minor units (kobo).

    Accepts ``int``, ``str``, ``Decimal`` or ``float``. Floats are tolerated only
    here, at the boundary, and are immediately quantised to 2 dp with banker-safe
    ``ROUND_HALF_UP`` before being turned into an exact integer.

    >>> to_kobo("1250.50")
    125050
    >>> to_kobo(Decimal("0.1") + Decimal("0.2"))
    30
    """
    if isinstance(amount, float):
        # Route through str() so we quantise the *displayed* value, not the
        # binary-float artefact (e.g. 0.1 -> "0.1", not 0.1000000000000000055).
        amount = Decimal(str(amount))
    elif not isinstance(amount, Decimal):
        amount = Decimal(amount)
    naira = amount.quantize(_NAIRA_QUANT, rounding=ROUND_HALF_UP)
    return int(naira * KOBO_PER_NAIRA)


def to_naira(kobo: int) -> Decimal:
    """Convert integer minor units (kobo) back to a major-unit ``Decimal`` (naira).

    Returns a ``Decimal`` (exact), never a float, so callers can keep computing
    safely or hand it to a serializer for display.

    >>> to_naira(125050)
    Decimal('1250.50')
    """
    if not isinstance(kobo, int):
        raise TypeError(f"kobo must be an int, got {type(kobo).__name__}")
    return (Decimal(kobo) / KOBO_PER_NAIRA).quantize(_NAIRA_QUANT)


def format_naira(kobo: int, *, symbol: str = "₦") -> str:
    """Human-readable, thousands-separated string for a kobo amount.

    >>> format_naira(125050)
    '₦1,250.50'
    """
    return f"{symbol}{to_naira(kobo):,.2f}"


class MoneyField(models.BigIntegerField):
    """A monetary column storing an integer number of minor units (kobo).

    ``BigIntegerField`` (64-bit) is used deliberately: even at kobo resolution it
    comfortably holds national-scale balances. Defaults to ``0`` and is non-null so
    a money column is never ambiguously empty — "no money" is ``0``, explicitly.

    This is a thin, intention-revealing subclass: a reviewer scanning a model sees
    ``MoneyField()`` and immediately knows the column is kobo, not naira and not a
    float. Conversion to/from major units is the boundary's job (:func:`to_kobo` /
    :func:`to_naira`), never the database's.
    """

    description = "Money amount stored as integer minor units (kobo)"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("default", 0)
        kwargs.setdefault("null", False)
        kwargs.setdefault(
            "help_text",
            kwargs.pop("help_text", "") or "Amount in minor units (kobo); integer, never float.",
        )
        super().__init__(*args, **kwargs)

    def deconstruct(self):
        # Keep migrations clean: only emit kwargs that differ from our defaults.
        name, path, args, kwargs = super().deconstruct()
        if kwargs.get("default") == 0:
            kwargs.pop("default", None)
        if kwargs.get("null") is False:
            kwargs.pop("null", None)
        return name, path, args, kwargs
