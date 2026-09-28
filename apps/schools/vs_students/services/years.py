"""Which school years may still be written to.

``vs_academics`` refuses every write against an archived year, and cannot
refuse this one: an enrolment is this module's row and ``vs_academics`` cannot
see it. Its FR-009 rule 3 says so explicitly, and says this module builds the
guard itself while ``vs_academics`` must not build a second, because a guard
written twice is a guard applied once.

What it protects is the truthfulness of a closed year. Once 2024/2025 is
archived, who was in JSS1 A that year is a fact, and attendance, results and
fees all hang off it. A promotion run with the wrong year selected would add a
child to that register eighteen months later and nothing would look wrong.

Reading a closed year is untouched, and so is promoting OUT of one: that is the
ordinary end-of-year move, where last year is exactly what you are leaving.

FRD M11 v2.5 FR-011.
"""
from __future__ import annotations

from django.db.models import Q

from ..exceptions import YearIsClosed


def assert_year_is_open(session, *, what="change"):
    """Raise when *session* is archived. Safe to call with None."""
    from schools.vs_academics.models import SessionStatus

    if session is None or session.status != SessionStatus.ARCHIVED:
        return
    raise YearIsClosed(
        f"{session.name} is closed, so you cannot {what} a student in it. It "
        f"is the record of a year that has finished. Switch to the year the "
        f"school is running.",
        session=session.pk,
        session_name=session.name,
    )


def in_year(session):
    """Who belongs to *session*'s list: the roll as it was, and those not yet placed.

    A child with a placement that year belongs to it, and nobody who only
    exists in another one: the roll AS IT WAS. A child with no placement at all
    (an applicant, an application closed before a class, a child enrolled and
    waiting for one) has no roll row to read, so they belong to the year of
    the level they applied for, and to the running year when they named none.
    Filtering on placements alone left every applicant and every unplaced child
    out of any year's list, which emptied the applicants board at every school
    with a year set up.
    """
    from schools.vs_academics.models import SessionStatus

    unplaced_here = Q(applied_for__session=session)
    if session.status == SessionStatus.ACTIVE:
        unplaced_here |= Q(applied_for__isnull=True)
    return Q(enrolments__session=session) | (Q(enrolments__isnull=True) & unplaced_here)
