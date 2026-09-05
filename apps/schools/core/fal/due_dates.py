"""Turning a school's fee due rule into a date on an invoice.

``vs_finance`` decides when a bill falls due from the entity's payment terms,
which is the right answer for a supplier invoice and the wrong one for school
fees: a term's fees are due by a date in the school's calendar, not thirty days
after whenever the bursar happened to run the billing. This module holds the
school's answer, and hands ``vs_finance`` a plain date so the engine never
learns what a term is.

The floor is the part worth knowing about. Every basis except ``DAYS_AFTER`` can
resolve to a date already past: a bursar who bills First Term in the last week
of November, catching up after a busy start, would otherwise raise invoices that
were overdue the moment they existed, and dun parents for lateness that was the
school's. So a resolved date earlier than the bill's own date is lifted to the
bill date. The fees are payable immediately, which is true, rather than late,
which is not.
"""
from __future__ import annotations

import calendar
import datetime
from typing import Optional


def month_end(day: datetime.date) -> datetime.date:
    """The last day of the month ``day`` falls in."""
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def resolve_due_date(
    *, basis: str, days_after: int, invoice_date: datetime.date,
    term_end: Optional[datetime.date] = None,
    session_end: Optional[datetime.date] = None,
) -> datetime.date:
    """The date a fee bill raised on ``invoice_date`` falls due.

    ``term_end`` and ``session_end`` come from the fee structure's own term link,
    so a structure linked to a whole session rather than one term has no term end
    and falls back to the session's. A basis whose date is unknown falls back to
    ``days_after`` rather than to nothing: an unresolvable rule must still
    produce a deadline, because a null due date is not "no deadline" to any
    report that reads it, it is "never overdue".
    """
    from .models import FeeDueBasis

    fallback = invoice_date + datetime.timedelta(days=days_after)

    if basis == FeeDueBasis.TERM_END:
        resolved = term_end or session_end
    elif basis == FeeDueBasis.SESSION_END:
        resolved = session_end
    elif basis == FeeDueBasis.MONTH_END:
        resolved = month_end(invoice_date)
    else:
        resolved = fallback

    if resolved is None:
        resolved = fallback

    # Never behind the bill itself: payable now, not already late.
    return max(resolved, invoice_date)


def policy_for(tenant_id) -> tuple[str, int]:
    """The school's ``(basis, days_after)``, or the default it bills by unset.

    Returns the default rather than ``None`` so callers have one shape to
    handle: a school that has never opened the settings screen still bills on
    term end, which is what a school means when it says nothing.
    """
    from .models import FeeDueBasis, SchoolFeeDuePolicy

    row = SchoolFeeDuePolicy.objects.filter(tenant_id=tenant_id).first()
    if row is None:
        return FeeDueBasis.TERM_END, 30
    return row.basis, row.days_after
