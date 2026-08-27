"""One person's week, derived from the class grids and stored nowhere.

**Read-only, and it has to stay that way.** A slot is changed on the class's
grid, and the response carries the class id for every slot so a client can link
straight there. A write path here would be a second way to edit the same row.

**Derived, never stored.** A stored teacher grid is a second copy of the class
grids that goes stale the moment one slot moves, and the school finds out when a
teacher walks to the wrong room.

**The workload figure is a count of rows and carries no judgement.** Nothing
records a contract, a part-time pattern or a maximum load, so there is no
threshold to have crossed. No status, no colour, no over-loaded flag and no
percentage of anything - every one of them would be the API inventing a check
the server cannot make.
"""
from __future__ import annotations

from collections import defaultdict

from django.db.models import Count
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_TIMETABLE_VIEW
from ..models import DayOfWeek, PeriodType, Period, TimetableSlot
from ..serializers import TimetableSlotSerializer
from ..services.bells import periods_in_force
from ..services.clashes import slot_warnings
from ..services.teachers import display_name, teaching_user_ids, teaching_users
from .base import CalendarViewMixin
from .timetable import GRID_DAYS


class TeacherListView(CalendarViewMixin, APIView):
    """GET /v1/academics/timetable/teachers/

    The teacher picker: every teacher of the school, alphabetical, each with the
    two facts the design shows beside the name - how many lessons they hold this
    year, and whether any of them clashes.

    **Not narrowed by branch, deliberately.** Mr Eze teaches Physics at Lekki on
    Monday to Wednesday and at Ikeja on Thursday and Friday; a picker filtered
    by the branch being edited would make him unschedulable at the second. What
    makes the wide picker safe is that the clash query is wide too. Narrowing it
    looks like tightening security and protects nothing: whether this caller may
    write this timetable at all was answered before the picker was rendered.

    **Nothing beyond those two facts.** No specialism, no availability, no
    qualification, no maximum load and no suggestion. Nothing in the platform
    records any of them.

    Not in FRD v3.0.1, which serves one teacher's grid and leaves the picker
    with nothing to render.

    docstring-name: Teachers
    """

    rbac_permission = PERM_TIMETABLE_VIEW
    pagination_class = None

    def get(self, request):
        session = self.session
        people = list(teaching_users(self.tenant))
        if session is None:
            return success_response(data=[
                {
                    "id": p.pk, "name": display_name(p),
                    "lesson_count": 0, "has_clash": False,
                }
                for p in people
            ])

        counts = dict(
            TimetableSlot.objects.filter(tenant=self.tenant, session=session)
            .exclude(teacher__isnull=True)
            .values_list("teacher_id")
            .annotate(n=Count("pk"))
            .values_list("teacher_id", "n"),
        )
        clashed = _teachers_with_clashes(self.tenant, session)

        search = (request.query_params.get("search") or "").strip().lower()
        out = []
        for person in people:
            name = display_name(person)
            if search and search not in name.lower():
                continue
            out.append({
                "id": person.pk,
                "name": name,
                "lesson_count": counts.get(person.pk, 0),
                "has_clash": person.pk in clashed,
            })
        return success_response(data=out)


def _teachers_with_clashes(tenant, session):
    """Which teachers are double-booked anywhere this year. One query.

    Tenant-wide: a person cannot be at two branches at once however the
    school's permissions are arranged.
    """
    rows = (
        TimetableSlot.objects.filter(tenant=tenant, session=session)
        .exclude(teacher__isnull=True)
        .values_list("teacher_id", "day_of_week", "period_id")
    )
    seen, clashed = set(), set()
    for teacher_id, day, period_id in rows:
        key = (teacher_id, day, period_id)
        if key in seen:
            clashed.add(teacher_id)
        seen.add(key)
    return clashed


class TeacherTimetableView(CalendarViewMixin, APIView):
    """GET /v1/academics/timetable/teachers/<user_id>/

    docstring-name: Teacher timetable
    """

    rbac_permission = PERM_TIMETABLE_VIEW
    pagination_class = None

    def get(self, request, user_id):
        from vs_user.models import User

        session = self.session_required
        person = User.objects.filter(tenant=self.tenant, pk=user_id).first()
        if person is None:
            # 404, never 403: another tenant's user id must not be probeable.
            raise NotFound("No such person at this school.")

        mine = list(
            TimetableSlot.objects.filter(
                tenant=self.tenant, session=session, teacher=person,
            ).select_related("period", "subject", "room", "school_class",
                            "school_class__branch", "teacher"),
        )
        # A person who is no longer in the teacher directory still gets their
        # grid rather than a 404: they may hold slots written before the role
        # was withdrawn, and hiding those would hide a real booking.

        by_key = {(s.day_of_week, s.period_id): s for s in mine}
        branches = {
            s.school_class.branch_id for s in mine if s.school_class.branch_id
        }

        # A person may teach across branches, so their free periods are the
        # union of the days they actually work rather than one branch's bells.
        period_rows = list(
            Period.objects.filter(
                tenant=self.tenant, session=session, is_active=True,
            ).select_related("branch"),
        )

        days, teaching, free = [], 0, 0
        per_day = defaultdict(int)
        for day in GRID_DAYS:
            in_force = periods_in_force(
                self.tenant, session, day_of_week=day, queryset=period_rows,
            )
            cells = []
            for period in in_force:
                if period.period_type != PeriodType.LESSON:
                    cells.append({
                        "period": period.pk,
                        "period_label": period.label,
                        "kind": period.period_type,
                        "label": period.get_period_type_display(),
                    })
                    continue
                slot = by_key.get((day, period.pk))
                if slot is None:
                    free += 1
                    cells.append({
                        "period": period.pk,
                        "period_label": period.label,
                        "kind": PeriodType.LESSON,
                        "slot": None,
                    })
                    continue
                teaching += 1
                per_day[day] += 1
                entry = TimetableSlotSerializer(
                    slot, context=self.get_serializer_context(),
                ).data
                if self.multi_branch and slot.school_class.branch_id:
                    entry["branch_name"] = slot.school_class.branch.name
                cells.append({
                    "period": period.pk,
                    "period_label": period.label,
                    "kind": PeriodType.LESSON,
                    "slot": entry,
                    "warnings": [
                        w.as_dict() for w in slot_warnings(
                            slot, visible=self.visible,
                        )
                    ],
                })
            days.append({
                "day_of_week": int(day),
                "day_label": DayOfWeek(day).label,
                "cells": cells,
            })

        busiest = max(per_day, key=per_day.get) if per_day else None
        data = {
            "teacher": {"id": person.pk, "name": display_name(person)},
            "session": {"id": session.pk, "name": session.name},
            "days": days,
            "summary": {
                # Plain counts. No threshold, no colour, no comparison.
                "teaching_periods": teaching,
                "free_periods": free,
                "busiest_day": DayOfWeek(busiest).label if busiest else None,
            },
            "read_only": True,
        }
        if self.multi_branch:
            from vs_tenants.models import Branch

            data["summary"]["branches"] = [
                b.name for b in Branch.all_objects.filter(
                    tenant=self.tenant, pk__in=branches,
                ).order_by("name")
            ]
        return success_response(data=data)
