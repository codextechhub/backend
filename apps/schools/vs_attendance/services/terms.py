"""The terms a screen offers, and the one it opens on.

A term pill lists every term that has begun, oldest first, so a school can look
back at last term and never forward at one that has not started. The screen
opens on the term containing today, else on the latest past term of the active
school year, else on the latest term of all. The student's Attendance tab and
the reports read their terms from here, so the two pick the same one.
"""
from __future__ import annotations

import datetime as dt

from rest_framework.exceptions import NotFound

from schools.vs_academics.models import AcademicTerm, SessionStatus


def terms_to_show(tenant, today: dt.date) -> list:
    """Every term that has begun, oldest first, for the term pills."""
    return list(
        AcademicTerm.objects.filter(tenant=tenant, start_date__lte=today)
        .select_related("session").order_by("start_date", "order_index"),
    )


def default_term(terms, today: dt.date):
    """The term containing *today*, else the latest past term of an active year, else the latest."""
    for term in terms:
        if term.start_date <= today <= term.end_date:
            return term
    active = [t for t in terms if t.session.status == SessionStatus.ACTIVE]
    pool = active or terms
    return max(pool, key=lambda t: (t.end_date, t.pk), default=None)


def term_block(term, today: dt.date) -> dict:
    return {
        "id": term.pk,
        "name": term.name,
        "session_name": term.session.name,
        "start_date": term.start_date,
        "end_date": term.end_date,
        "is_current": term.start_date <= today <= term.end_date,
    }


def chosen_term(tenant, terms, term_id, today: dt.date):
    """The term named by *term_id*, or the default when none is named.

    A term of another school answers the same 404 as one that does not exist.
    """
    if term_id is None:
        return default_term(terms, today)
    term = (
        AcademicTerm.objects.filter(tenant=tenant, pk=term_id)
        .select_related("session").first()
    )
    if term is None:
        raise NotFound("No such term at this school.")
    return term
