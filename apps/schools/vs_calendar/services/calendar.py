"""Reading the school year: which term it is, what is dated inside it, and
which days are actually taught.

**What is derived is never stored.** A term is not a column on an event: it is
the term of the event's session whose dates cover the event's start. Storing it
would give the school two truths the day a term's dates were corrected.

**An archived term is still a term.** An event whose dates fall inside one falls
inside it and reports it. ``EVENT_OUTSIDE_ANY_TERM`` means one thing only - the
event's dates fall inside no term of its session at all, which is the December
break, the gap between two terms, or a date before the first or after the last.
Alerting on an archived term would tell a school its calendar is broken when the
school archived a year exactly as it was supposed to.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Q

from ..models import CalendarEvent, CalendarEventAudience


def term_of(session, on_date, terms=None):
    """The term covering *on_date*, or None. Compares dates, never archived_at."""
    rows = terms if terms is not None else list(session.terms.all())
    for term in rows:
        if term.start_date <= on_date <= term.end_date:
            return term
    return None


def current_term(session, today, terms=None):
    return term_of(session, today, terms=terms)


def visible_events(tenant, session, *, visible_branches):
    """Every event of the year the caller may see.

    Inclusive: school-wide events plus the caller's own branches'.
    """
    from .scoping import scope_to_visible_branches

    rows = CalendarEvent.objects.filter(tenant=tenant, session=session)
    return rows


def audience_labels(event, *, audience_rows=None):
    """Who the event covers, as names, or an empty list meaning everybody.

    An empty list is not "nobody": it is the default and it means the whole of
    the event's branch scope. The serializer renders it as an absent field
    rather than as an empty chip, so a screen never shows "Applies to: none".
    """
    rows = (
        audience_rows
        if audience_rows is not None
        else event.audience.select_related("level", "school_class").all()
    )
    out = []
    for row in rows:
        if row.level_id:
            out.append({"type": "level", "id": row.level_id, "name": row.level.name})
        else:
            out.append({
                "type": "class",
                "id": row.school_class_id,
                "name": row.school_class.name,
            })
    return out


def classes_covered_by(event):
    """The class ids an event actually reaches, for the teaching-day count.

    A level in the audience covers every class under it, which is what a school
    means by "the whole of JSS1" and saves it naming three arms one at a time.

    With no audience rows the event covers everything in its branch scope, and
    this returns None to say so - distinct from an empty set, which would mean
    it reaches nothing.
    """
    from schools.vs_academics.models import SchoolClass

    rows = list(event.audience.all())
    if not rows:
        return None

    level_ids = [r.level_id for r in rows if r.level_id]
    class_ids = {r.school_class_id for r in rows if r.school_class_id}
    if level_ids:
        class_ids |= set(
            SchoolClass.objects.filter(level_id__in=level_ids)
            .values_list("id", flat=True),
        )
    return class_ids


def non_teaching_dates(events, *, school_class=None):
    """The dates a school is closed, for one class or for the school.

    ``school_class`` narrows by audience: Lekki's primary Speech Day closes the
    day for Primary 4 A and not for JSS1 A, which is the whole reason the
    audience table exists. Without it the teaching-day count is wrong for every
    class the event did not actually reach.
    """
    out = set()
    for event in events:
        if not event.closes_school:
            continue
        if school_class is not None:
            covered = classes_covered_by(event)
            if covered is not None and school_class.pk not in covered:
                continue
        day = event.start_date
        while day <= event.end_date:
            out.add(day)
            day += timedelta(days=1)
    return out


def teaching_days(term, events, *, school_class=None, weekend=(6, 7)):
    """How many days of *term* are actually taught, and how many have passed.

    A weekday that is not closed. Saturday and Sunday are excluded by default
    rather than by a stored calendar, because nothing in the platform records
    which days a school opens; a school teaching Saturdays passes its own set.
    """
    closed = non_teaching_dates(events, school_class=school_class)
    total = 0
    day = term.start_date
    while day <= term.end_date:
        if day.isoweekday() not in weekend and day not in closed:
            total += 1
        day += timedelta(days=1)
    return total


def teaching_days_elapsed(term, events, on_date, *, school_class=None,
                          weekend=(6, 7)):
    closed = non_teaching_dates(events, school_class=school_class)
    count = 0
    day = term.start_date
    last = min(on_date, term.end_date)
    while day <= last:
        if day.isoweekday() not in weekend and day not in closed:
            count += 1
        day += timedelta(days=1)
    return count
