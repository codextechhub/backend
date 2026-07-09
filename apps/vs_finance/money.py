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
from __future__ import annotations  # Defer annotation evaluation during app import.

from decimal import Decimal, ROUND_HALF_UP  # Exact decimal arithmetic and currency rounding mode.
from typing import Union  # Boundary input type alias.

from django.db import models  # Base field class for the custom money column.

#: Minor units in one major unit. NGN, like most currencies, has 100.
KOBO_PER_NAIRA = 100  # Conversion factor between naira and kobo.

#: Quantiser for 2-decimal-place naira rounding.
_NAIRA_QUANT = Decimal("0.01")  # Two-decimal quantizer for naira values.

Number = Union[int, str, Decimal, float]  # Accepted boundary types for major-unit amounts.


def to_kobo(amount: Number) -> int:  # Convert major-unit naira input into integer kobo.
    """Convert a major-unit amount (naira) to integer minor units (kobo).

    Accepts ``int``, ``str``, ``Decimal`` or ``float``. Floats are tolerated only
    here, at the boundary, and are immediately quantised to 2 dp with banker-safe
    ``ROUND_HALF_UP`` before being turned into an exact integer.

    >>> to_kobo("1250.50")
    125050
    >>> to_kobo(Decimal("0.1") + Decimal("0.2"))
    30
    """
    if isinstance(amount, float):  # Floats are only accepted at the input boundary.
        # Route through str() so we quantise the *displayed* value, not the
        # binary-float artefact (e.g. 0.1 -> "0.1", not 0.1000000000000000055).  # Avoid binary float noise.
        amount = Decimal(str(amount))  # Convert through string to preserve human-entered value.
    elif not isinstance(amount, Decimal):  # Strings and ints need Decimal normalization.
        amount = Decimal(amount)  # Convert non-Decimal boundary value to Decimal.
    naira = amount.quantize(_NAIRA_QUANT, rounding=ROUND_HALF_UP)  # Round to cents/kobo precision.
    return int(naira * KOBO_PER_NAIRA)  # Scale to integer kobo.


def to_naira(kobo: int) -> Decimal:  # Convert integer kobo back to exact Decimal naira.
    """Convert integer minor units (kobo) back to a major-unit ``Decimal`` (naira).

    Returns a ``Decimal`` (exact), never a float, so callers can keep computing
    safely or hand it to a serializer for display.

    >>> to_naira(125050)
    Decimal('1250.50')
    """
    if not isinstance(kobo, int):  # Internal money values must remain integer kobo.
        raise TypeError(f"kobo must be an int, got {type(kobo).__name__}")
    return (Decimal(kobo) / KOBO_PER_NAIRA).quantize(_NAIRA_QUANT)  # Scale down and normalize to 2 dp.


def format_naira(kobo: int, *, symbol: str = "₦") -> str:  # Format integer kobo for human display.
    """Human-readable, thousands-separated string for a kobo amount.

    >>> format_naira(125050)
    '₦1,250.50'
    """
    return f"{symbol}{to_naira(kobo):,.2f}"  # Add symbol, thousands separators, and 2 decimals.


_ONES = (  # English words for values from 0 through 19.
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",  # Single digits.
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",  # Ten through sixteen.
    "seventeen", "eighteen", "nineteen",  # Seventeen through nineteen.
)  # Close the grouped expression.
_TENS = (  # English words for tens multiples.
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",  # Index matches tens digit.
)  # Close the grouped expression.
#: Scale words indexed by thousands-group (index 1 = thousand, 2 = million, …).
_SCALES = ("", "thousand", "million", "billion", "trillion", "quadrillion")  # Thousand-group scale names.


def _three_digits_in_words(n: int) -> str:  # Spell a single 0-999 group.
    """Spell a number 0–999 (no scale word). Returns '' for 0 (caller handles it)."""
    if n == 0:  # Empty group contributes no words.
        return ""
    parts = []  # Word fragments for hundreds and remainder.
    if n >= 100:  # Add hundreds phrase when present.
        parts.append(f"{_ONES[n // 100]} hundred")  # Convert hundreds digit.
        n %= 100  # Keep only the remainder below one hundred.
    if n >= 20:  # Remainders from 20 to 99 use tens plus optional hyphenated unit.
        parts.append(_TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else ""))  # Add tens phrase.
    elif n > 0:  # Remainders from 1 to 19 map directly.
        parts.append(_ONES[n])  # Add direct small-number word.
    return " ".join(parts)  # Join fragments for this group.


def _int_in_words(n: int) -> str:  # Spell a non-negative integer.
    """Spell a non-negative integer in words (British short scale). 0 → 'zero'."""
    if n == 0:  # Zero is a special case because group spelling returns blank.
        return "zero"
    groups = []  # Word groups from least significant to most significant.
    scale = 0  # Index into _SCALES for each thousands group.
    while n > 0:  # Split number into 3-digit groups.
        n, rem = divmod(n, 1000)  # Peel off the next thousands group.
        if rem:  # Skip empty groups.
            words = _three_digits_in_words(rem)  # Spell this 3-digit group.
            if _SCALES[scale]:  # Add scale word for thousands and above.
                words = f"{words} {_SCALES[scale]}"  # Append scale label.
            groups.append(words)  # Store group words in reverse order.
        scale += 1  # Move to the next scale.
    return " ".join(reversed(groups))  # Reverse to most-significant-first order.


def naira_in_words(kobo: int) -> str:  # Spell a kobo amount for receipts/documents.
    """Spell a kobo amount as words, e.g. for a receipt's amount-in-words line.

    The naira part is spelled in British short scale and suffixed ``naira``; when
    there is a kobo remainder it is appended (``… naira, fifty kobo``), otherwise
    ``only`` is appended (``… naira only``). The first letter is capitalised.

    >>> naira_in_words(10000000)
    'One hundred thousand naira only'
    >>> naira_in_words(125050)
    'One thousand two hundred fifty naira, fifty kobo'
    >>> naira_in_words(0)
    'Zero naira only'
    """
    if not isinstance(kobo, int):  # Internal money values must remain integer kobo.
        raise TypeError(f"kobo must be an int, got {type(kobo).__name__}")
    negative = kobo < 0  # Preserve sign for final wording.
    kobo = abs(kobo)  # Spell the absolute amount.
    naira, k = divmod(kobo, KOBO_PER_NAIRA)  # Split major and minor units.

    words = f"{_int_in_words(naira)} naira"  # Start with the naira portion.
    if k:  # Include kobo words when there is a remainder.
        words += f", {_int_in_words(k)} kobo"  # Append minor-unit wording.
    else:  # Whole-naira amounts get the conventional suffix.
        words += " only"  # Append "only" when no kobo remainder exists.
    if negative:  # Negative amounts keep a clear prefix.
        words = f"minus {words}"  # Prefix negative wording.
    return words[0].upper() + words[1:]  # Capitalize the first character for display.


class MoneyField(models.BigIntegerField):  # Django model field storing money as integer kobo.
    """A monetary column storing an integer number of minor units (kobo).

    ``BigIntegerField`` (64-bit) is used deliberately: even at kobo resolution it
    comfortably holds national-scale balances. Defaults to ``0`` and is non-null so
    a money column is never ambiguously empty — "no money" is ``0``, explicitly.

    This is a thin, intention-revealing subclass: a reviewer scanning a model sees
    ``MoneyField()`` and immediately knows the column is kobo, not naira and not a
    float. Conversion to/from major units is the boundary's job (:func:`to_kobo` /
    :func:`to_naira`), never the database's.
    """

    description = "Money amount stored as integer minor units (kobo)"  # Admin/introspection field description.

    def __init__(self, *args, **kwargs):  # Initialize money field defaults.
        kwargs.setdefault("default", 0)  # Money defaults to zero, not NULL.
        kwargs.setdefault("null", False)  # Money columns are always non-null.
        kwargs.setdefault(  # Provide a clear admin/schema help text.
            "help_text",  # Help text keyword argument.
            kwargs.pop("help_text", "") or "Amount in minor units (kobo); integer, never float.",  # Use caller text or default.
        )  # Close the grouped expression.
        super().__init__(*args, **kwargs)  # Delegate final field setup to BigIntegerField.

    def deconstruct(self):  # Serialize field configuration for Django migrations.
        # Keep migrations clean: only emit kwargs that differ from our defaults.  # Avoid noisy generated migrations.
        name, path, args, kwargs = super().deconstruct()  # Get the base field deconstruction.
        if kwargs.get("default") == 0:  # Default zero is implicit for MoneyField.
            kwargs.pop("default", None)  # Remove implicit default from migration kwargs.
        if kwargs.get("null") is False:  # Non-null is implicit for MoneyField.
            kwargs.pop("null", None)  # Remove implicit null setting from migration kwargs.
        return name, path, args, kwargs  # Return cleaned migration representation.
