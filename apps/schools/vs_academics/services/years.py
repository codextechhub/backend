"""Which years may still be written to.

An archived year is a record of what happened, and the reason the structure
gained a year column in the first place: reading 2025/2026 back should show the
classes that actually ran, not the classes today's admin has since edited into
it. So a write that lands in an archived year is refused.

One function, called from the one place every write resolves its year
(`AcademicsViewMixin.session`) and from the two paths that take their year from
a level instead of from the lens. Spread across the eleven write views it would
be eleven chances to forget.
"""
from __future__ import annotations

from ..exceptions import SessionArchivedReadOnly


def assert_year_is_writable(session) -> None:
    """Raise when `session` is archived. Safe to call with None."""
    from ..models import SessionStatus

    if session is None:
        return
    if session.status == SessionStatus.ARCHIVED:
        raise SessionArchivedReadOnly(
            f"{session.name} is archived, so it can no longer be changed. It is "
            f"the record of a year that has finished. Switch to the year you "
            f"are running to make changes.",
        )
