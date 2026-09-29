"""Continuing a series of numbers a tenant issues in a format of its own.

A tenant that numbers things (enrolments, employees, anything) chooses the
format, and the platform cannot invert a regular expression into "the next
one". So the next number is read from the numbers the tenant has actually
issued: the most recent one's trailing run of digits is incremented, keeping
its zero padding, which works for any format that ends in digits without
knowing what the format is:

    BFS/2025/0142  ->  BFS/2025/0143
    CSS-24-0117    ->  CSS-24-0118
    0099           ->  0100

Two rules hold for every caller. A format is checked **anchored**, so a rule
cannot match a substring of a longer number. And a successor that the tenant's
own rule refuses is no successor at all: the series has moved on (a year inside
the number has changed, say) and an empty answer is better than a confident
wrong one.
"""
from __future__ import annotations

import re

#: Bounded so a pathological series cannot spin: twenty consecutive successors
#: all taken means the tenant is not numbering the way this reads it.
SUCCESSOR_TRIES = 20


def anchored_pattern(pattern: str):
    """*pattern* compiled to match a whole number, or None for no pattern.

    A leading ``^`` and trailing ``$`` are accepted and ignored, so a rule
    written either way means the same. Raises ``re.error`` for an expression
    that does not compile; the caller words the refusal.
    """
    if not pattern:
        return None
    body = pattern
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$") and not body.endswith("\\$"):
        body = body[:-1]
    return re.compile(f"^(?:{body})$")


def split_series(number: str):
    """``(head, digits)`` for a number ending in digits, else None.

    Anchored to the end: "BFS/2025/A" has no successor, and reading the year
    inside it as the running digits would offer "BFS/2026/A".
    """
    match = re.search(r"(\d+)$", number or "")
    if match is None:
        return None
    return number[: match.start(1)], match.group(1)


def successor(head: str, digits: str, *, taken=frozenset(), compiled=None) -> str:
    """The first number after ``head + digits`` that is free and in format.

    *taken* holds lower-cased numbers that may not be offered, compared without
    case as the tenants' unique constraints compare them. Returns "" when the
    next free number fails *compiled* or none is free within
    :data:`SUCCESSOR_TRIES`.
    """
    value = int(digits)
    for _ in range(SUCCESSOR_TRIES):
        value += 1
        candidate = f"{head}{str(value).zfill(len(digits))}"
        if candidate.lower() in taken:
            continue
        if compiled is not None and not compiled.match(candidate):
            return ""
        return candidate
    return ""
