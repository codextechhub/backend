"""The three reads behind the hub and the term calendar.

``/current/`` answers what year and term it is. ``/year/`` is the session with
its terms as a timeline. ``/overview/`` is the hub: where the year stands, what
is coming next, and what is wrong if anything is.

**Two alerts here can never be produced through the API**, and they stay.
``TERM_OUTSIDE_SESSION`` and ``TERM_DATES_OVERLAP`` are both refused at write
time by ``vs_academics`` and backed by a check constraint, so a calendar built
through the
API cannot contain either. Rows arrive by import, by fixture and by migration
too, and a school year that is quietly malformed produces a calendar that is
wrong everywhere and blamed nowhere. They are read-only observations: this
module reports them and must not try to correct them, because the write path
belongs to ``vs_academics``.

**Two alerts and two counts here are not in FRD v3.0.1, and are deliberate.**
Its FR-007 forbids a timetable figure or a clash on this response. That text is
carried unchanged from version 2.3, when the timetable half was deferred, and
its own justification is that such a figure would have "nothing behind it".
Version 3.0 restored the timetable half and never revisited it. The hub screen
shows a classes-timetabled count, a rooms count, an unresolved-clash alert and a
class-with-no-timetable alert, and now there is something behind all four.

**Two of its prohibitions survive and are honoured.** There is no
*complete*-timetable count here - the count is of classes that hold at least one
lesson, never of classes that are finished, because nothing knows how many
periods a subject should get. And there is no scheduled-exams count.
"""
from __future__ import annotations

from datetime import date

from django.db.models import Count, Q
from rest_framework.views import APIView

from core.response import success_response

from ..constants import (
    ALERT_CLASS_HAS_NO_TIMETABLE,
    ALERT_EVENT_OUTSIDE_ANY_TERM,
    ALERT_SESSION_HAS_NO_TERMS,
    ALERT_TERM_DATES_OVERLAP,
    ALERT_TERM_OUTSIDE_SESSION,
    ALERT_TIMETABLE_HAS_CLASHES,
    PERM_CALENDAR_VIEW,
)
from ..models import CalendarEvent, Room, TimetableSlot
from ..serializers import CalendarEventSerializer
from ..services.calendar import (
    teaching_days,
    teaching_days_elapsed,
    term_of,
)
from ..services.scoping import (
    lens_branch,
    narrow_to_lens,
    scope_to_visible_branches,
)
from .base import CalendarViewMixin


def _on_date(request):
    raw = (request.query_params.get("on") or "").strip()
    if raw:
        try:
            return date.fromisoformat(raw)
        except ValueError:
            pass
    return date.today()


def _term_payload(term):
    return {
        "id": term.pk, "name": term.name,
        "start_date": term.start_date, "end_date": term.end_date,
    }


class CurrentView(CalendarViewMixin, APIView):
    """GET /v1/academics/calendar/current/

    docstring-name: What is current
    """

    rbac_permission = PERM_CALENDAR_VIEW
    pagination_class = None

    def get(self, request):
        session = self.session
        if session is None:
            # 200 with nothing, not 404: a school that has not started its year
            # is not a school with a broken calendar.
            return success_response(data={})
        today = _on_date(request)
        terms = list(session.terms.all())
        term = term_of(session, today, terms=terms)
        return success_response(data={
            "session": {
                "id": session.pk, "name": session.name,
                "start_date": session.start_date, "end_date": session.end_date,
                "status": session.status,
            },
            "term": _term_payload(term) if term else None,
            "on": today,
        })


class YearView(CalendarViewMixin, APIView):
    """GET /v1/academics/calendar/year/

    The session as a timeline: its span, its terms, and where today falls.

    docstring-name: The school year
    """

    rbac_permission = PERM_CALENDAR_VIEW
    pagination_class = None

    def get(self, request):
        session = self.session
        if session is None:
            return success_response(data={})
        today = _on_date(request)
        terms = list(session.terms.all())

        rows = []
        for term in terms:
            if term.end_date < today:
                state = "completed"
            elif term.start_date <= today:
                state = "ongoing"
            else:
                state = "pending"
            entry = _term_payload(term)
            entry["state"] = state
            rows.append(entry)

        return success_response(data={
            "session": {
                "id": session.pk, "name": session.name,
                "start_date": session.start_date, "end_date": session.end_date,
                "status": session.status,
                # An archived year is read-only across every screen in this
                # module, and the client needs to know before it renders a
                # button it will only be refused for pressing.
                "read_only": session.status == "ARCHIVED",
            },
            "terms": rows,
            "on": today,
        })


class OverviewView(CalendarViewMixin, APIView):
    """GET /v1/academics/calendar/overview/

    docstring-name: Calendar overview
    """

    rbac_permission = PERM_CALENDAR_VIEW
    pagination_class = None

    def get(self, request):
        session = self.session
        if session is None:
            return success_response(data={})
        today = _on_date(request)
        terms = list(session.terms.all())
        term = term_of(session, today, terms=terms)

        # The hub counts what the screens below it list, so it reads through
        # the same lens they do. A hub saying "12 events, 4 classes" over
        # screens showing 5 and 2 is worse than a hub with no counts on it.
        lens = lens_branch(self)

        events = list(
            narrow_to_lens(
                scope_to_visible_branches(
                    CalendarEvent.objects.filter(
                        tenant=self.tenant, session=session,
                    ).select_related("branch"),
                    request.user, self.tenant,
                ),
                lens,
            ),
        )

        term_events = [
            e for e in events
            if term and e.start_date <= term.end_date and e.end_date >= term.start_date
        ]
        upcoming = sorted(
            (e for e in events if e.end_date >= today),
            key=lambda e: (e.start_date, e.name),
        )[:4]

        classes = list(
            narrow_to_lens(
                scope_to_visible_branches(
                    __import__(
                        "schools.vs_academics.models", fromlist=["SchoolClass"],
                    ).SchoolClass.objects.filter(
                        tenant=self.tenant, session=session, is_active=True,
                    ),
                    request.user, self.tenant,
                ),
                lens,
            ),
        )
        timetabled_ids = set(
            TimetableSlot.objects.filter(tenant=self.tenant, session=session)
            .values_list("school_class_id", flat=True)
            .distinct(),
        )
        # Rooms are the exception the lens documents: a room's branch is never
        # null, so there is no shared row to include and the read is exclusive.
        rooms = scope_to_visible_branches(
            Room.objects.filter(tenant=self.tenant), request.user, self.tenant,
        )
        if lens is not None:
            rooms = rooms.filter(branch=lens)
        rooms = rooms.count()

        data = {
            "session": {
                "id": session.pk, "name": session.name,
                "start_date": session.start_date, "end_date": session.end_date,
                "status": session.status,
            },
            "term": None,
            "counts": {
                "terms": len(terms),
                "events_in_term": len(term_events),
                # A count of classes that hold at least one lesson. Never a
                # count of classes that are finished: nothing knows how many
                # periods a subject should get, so a full grid is not
                # necessarily right and one with gaps is not necessarily wrong.
                "classes_timetabled": len(
                    [c for c in classes if c.pk in timetabled_ids],
                ),
                "rooms": rooms,
                # Deliberately no scheduled-exams count. FR-007 forbids it and
                # the reason still holds: a zero would read as a real and
                # alarming figure rather than an absent feature.
            },
            "next_up": [
                {
                    "id": e.pk, "name": e.name, "event_type": e.event_type,
                    "type_label": e.get_event_type_display(),
                    "start_date": e.start_date, "end_date": e.end_date,
                    "days_away": (e.start_date - today).days,
                }
                for e in upcoming
            ],
            "alerts": self._alerts(
                session, terms, events, classes, timetabled_ids,
            ),
        }
        if term is not None:
            total_days = (term.end_date - term.start_date).days + 1
            elapsed = max(0, min(total_days, (today - term.start_date).days + 1))
            data["term"] = {
                **_term_payload(term),
                "days_elapsed": elapsed,
                "days_total": total_days,
                # The half the closed-days flag exists for. Narrowed by
                # audience per class elsewhere; school-wide here.
                "teaching_days_elapsed": teaching_days_elapsed(term, events, today),
                "teaching_days_total": teaching_days(term, events),
            }
        return success_response(data=data)

    def _alerts(self, session, terms, events, classes, timetabled_ids):
        out = []
        if not terms:
            out.append({
                "code": ALERT_SESSION_HAS_NO_TERMS,
                "detail": f"{session.name} has no terms defined.",
                "ids": [],
            })

        # Defensive: ``vs_academics`` refuses both at write time and a check
        # constraint backs it, so neither arrives through the API. Rows arrive
        # other ways.
        outside = [
            t for t in terms
            if t.start_date < session.start_date or t.end_date > session.end_date
        ]
        if outside:
            out.append({
                "code": ALERT_TERM_OUTSIDE_SESSION,
                "detail": (
                    f"{', '.join(t.name for t in outside)} "
                    f"{'falls' if len(outside) == 1 else 'fall'} outside "
                    f"{session.name}."
                ),
                "ids": [t.pk for t in outside],
            })
        ordered = sorted(terms, key=lambda t: t.start_date)
        overlapping = [
            (a, b) for a, b in zip(ordered, ordered[1:])
            if b.start_date <= a.end_date
        ]
        if overlapping:
            out.append({
                "code": ALERT_TERM_DATES_OVERLAP,
                "detail": "; ".join(
                    f"{a.name} and {b.name} overlap" for a, b in overlapping
                ) + ".",
                "ids": sorted({t.pk for pair in overlapping for t in pair}),
            })

        # An event in the gap between two terms, or before the first, or after
        # the last. NOT an event whose term has been archived: an archived term
        # is still a term, such an event falls in it and reports it, and
        # alerting would tell a school its calendar is broken when it archived a
        # year exactly as it was supposed to.
        stray = [e for e in events if term_of(session, e.start_date, terms=terms) is None]
        if stray:
            out.append({
                "code": ALERT_EVENT_OUTSIDE_ANY_TERM,
                "detail": (
                    f"{len(stray)} "
                    f"{'event is' if len(stray) == 1 else 'events are'} dated "
                    f"outside every term. "
                    f"{'It is' if len(stray) == 1 else 'They are'} still on the "
                    f"calendar."
                ),
                "ids": [e.pk for e in stray],
            })

        from .timetable import _classes_with_clashes

        clashed = _classes_with_clashes(self.tenant, session)
        if clashed:
            out.append({
                "code": ALERT_TIMETABLE_HAS_CLASHES,
                "detail": (
                    f"{len(clashed)} "
                    f"{'class has' if len(clashed) == 1 else 'classes have'} an "
                    f"unresolved clash. A teacher or room is double-booked, and "
                    f"publishing is blocked until it is fixed."
                ),
                "ids": sorted(clashed),
            })

        missing = [c for c in classes if c.pk not in timetabled_ids]
        if missing:
            out.append({
                "code": ALERT_CLASS_HAS_NO_TIMETABLE,
                "detail": (
                    f"{', '.join(c.name for c in missing[:3])}"
                    f"{' and others' if len(missing) > 3 else ''} "
                    f"{'has' if len(missing) == 1 else 'have'} no timetable yet."
                ),
                "ids": [c.pk for c in missing],
            })
        return out
