"""Whether a register is due on a given day.

A class owes a register on a date only when the school is open to it, and the
school is open when all three hold:

* the weekday is one of the school's teaching days
  (``calendar.teaching_days``);
* the date falls inside a term of the class's school year;
* no calendar entry that closes the school covers it: one for the whole school
  (no branch), or one for the register's branch. An entry narrowed to certain
  levels or classes closes only those, so Lekki's primary Speech Day closes
  Primary 4 A and not JSS1 A.

When the day is closed the answer carries the reason a person would read on a
screen, which is the name of the entry that closed it. No register is due on a
closed day and none is ever reported missing for it.

**One rule, two ways to ask it.** :func:`school_day` answers for one class on one
date. :class:`DayCalendar` loads the school's teaching days, terms and closing
entries once for a range of dates and every school year in play, then answers
for any class and date without another query, which is what the hub (every class
on one date) and a student's term (one pupil's classes over many dates) need.
:func:`school_day` is the second with a range of one day, so the two cannot
disagree.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from schools.vs_academics.models import AcademicTerm
from schools.vs_academics.services.words import term_word
from schools.vs_calendar.models import CalendarEvent
from schools.vs_calendar.services.calendar import term_of
from schools.vs_calendar.services.calendar_rules import read_teaching_days

CODE_NOT_TEACHING_DAY = "NOT_TEACHING_DAY"
CODE_OUTSIDE_TERM = "OUTSIDE_TERM"
CODE_CLOSED_BY_EVENT = "CLOSED_BY_EVENT"


@dataclass(frozen=True)
class DayStatus:
    """Whether the school is open to one class on one date, and why not."""

    is_open: bool
    reason: str = ""
    code: str = ""
    term: object = None

    def payload(self):
        """The ``closed`` block of a response: null on an open day."""
        if self.is_open:
            return None
        return {"reason": self.reason, "code": self.code}


class DayCalendar:
    """The school's open and closed days for some school years and a range of dates."""

    def __init__(self, tenant, sessions, start: dt.date, end: dt.date):
        self.tenant = tenant
        session_ids = {session.pk for session in sessions}
        self.teaching_days = read_teaching_days(tenant)
        self._terms: dict[int, list] = {pk: [] for pk in session_ids}
        for term in AcademicTerm.objects.filter(tenant=tenant, session_id__in=session_ids):
            self._terms[term.session_id].append(term)
        self._events: dict[int, list] = {pk: [] for pk in session_ids}
        events = (
            CalendarEvent.objects.filter(
                tenant=tenant, session_id__in=session_ids, closes_school=True,
                start_date__lte=end, end_date__gte=start,
            )
            .prefetch_related("audience")
            .order_by("start_date", "name")
        )
        for event in events:
            self._events[event.session_id].append((event, _audience_of(event)))
        self._outside_term_reason = None

    def _outside_term(self) -> str:
        if self._outside_term_reason is None:
            self._outside_term_reason = f"This date is outside {term_word(self.tenant)} time."
        return self._outside_term_reason

    def status(self, school_class, branch, on_date: dt.date) -> DayStatus:
        """Whether *school_class* owes a register on *on_date*, with the reason if not."""
        if on_date.isoweekday() not in self.teaching_days:
            return DayStatus(
                False, f"{on_date.strftime('%A')} is not a teaching day.",
                CODE_NOT_TEACHING_DAY,
            )

        term = term_of(
            school_class.session, on_date, terms=self._terms.get(school_class.session_id, []),
        )
        if term is None:
            return DayStatus(False, self._outside_term(), CODE_OUTSIDE_TERM)

        for event, audience in self._events.get(school_class.session_id, []):
            if not (event.start_date <= on_date <= event.end_date):
                continue
            if event.branch_id is not None and event.branch_id != branch.pk:
                continue
            if audience is None or _audience_covers(audience, school_class):
                return DayStatus(False, event.name, CODE_CLOSED_BY_EVENT)
        return DayStatus(True, term=term)


def _audience_of(event):
    """The levels and classes an entry is narrowed to, or None for everybody."""
    rows = list(event.audience.all())
    if not rows:
        return None
    return (
        {row.level_id for row in rows if row.level_id},
        {row.school_class_id for row in rows if row.school_class_id},
    )


def _audience_covers(audience, school_class) -> bool:
    """Whether an entry's audience reaches *school_class*, a level reaching its classes."""
    level_ids, class_ids = audience
    return school_class.pk in class_ids or school_class.level_id in level_ids


def school_day(tenant, school_class, branch, on_date: dt.date) -> DayStatus:
    """Whether *school_class* owes a register on *on_date*, with the reason if not."""
    return DayCalendar(tenant, [school_class.session], on_date, on_date).status(
        school_class, branch, on_date,
    )
