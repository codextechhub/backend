"""Which school years may still be written to.

M13 refuses every write against an archived year, and cannot refuse this one:
an enrolment is this module's row and M13 cannot see it. Its FR-009 rule 3 says
so explicitly, and says M11 builds this itself and M13 must not build a second,
because a guard written twice is a guard applied once.

What it protects is the truthfulness of a closed year. Once 2024/2025 is
archived, who was in JSS1 A that year is a fact, and attendance, results and
fees all hang off it. A promotion run with the wrong year selected would add a
child to that register eighteen months later and nothing would look wrong.

Reading a closed year is untouched, and so is promoting OUT of one: that is the
ordinary end-of-year move, where last year is exactly what you are leaving.

FRD M11 v2.5 FR-011.
"""
from __future__ import annotations

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
