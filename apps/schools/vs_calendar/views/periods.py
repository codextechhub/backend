"""The bell schedule: the daily period structure every grid is built on.

The screen's centrepiece is a proportional strip of one day, and the rule it
exists to make unmistakable is that **a weekday holding its own periods replaces
the everyday schedule on that day rather than adding to it**. The list read
therefore answers for a day rather than handing over every row and leaving the
client to work out which apply - two clients would work it out differently.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Q
from rest_framework import generics
from rest_framework.exceptions import ValidationError

from core.response import success_response
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import (
    PERM_TIMETABLE_CREATE,
    PERM_TIMETABLE_MANAGE,
    PERM_TIMETABLE_UPDATE,
    PERM_TIMETABLE_VIEW,
)
from ..models import DayOfWeek, Period
from ..serializers import PeriodSerializer, PeriodWriteSerializer
from ..services.bells import (
    assert_no_overlap,
    day_has_own_schedule,
    provisional_order_index,
    periods_in_force,
    renumber_day,
)
from ..services.scoping import (
    UNSET,
    narrow_to_lens,
    raised_branch,
    scope_to_visible_branches,
)
from .base import CalendarViewMixin


class _PeriodBase(CalendarViewMixin):
    serializer_class = PeriodSerializer

    def _base(self):
        return (
            Period.objects.filter(tenant=self.tenant, session=self.session)
            .select_related("branch")
        )

    def _write_branch(self, validated):
        requested = validated["branch"] if "branch" in validated else UNSET
        return raised_branch(self.request.user, self.tenant, requested)

    def _lens_branch(self):
        # The module's one lens reader. A local copy here is how the other
        # surfaces end up with no copy at all.
        from ..services.scoping import lens_branch

        return lens_branch(self)


class PeriodListCreateView(_PeriodBase, generics.ListCreateAPIView):
    """GET, POST /v1/academics/timetable/periods/

    ``?day=`` narrows to the periods actually in force on that weekday, which
    is not the same as filtering on the column: a day with no rows of its own
    runs the everyday schedule, and a day with rows of its own runs only those.

    docstring-name: Bell schedule
    """

    pagination_class = None  # A school day is a dozen rows; paginating it would
                             # make a client reassemble a schedule it asked for whole.

    def get_permissions(self):
        self.rbac_permission = (
            PERM_TIMETABLE_CREATE if self.request.method == "POST"
            else PERM_TIMETABLE_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        qs = scope_to_visible_branches(self._base(), self.request.user, self.tenant)
        # The lens was read here from the day the screen shipped and applied
        # only to the "school day" strip below, never to the list of periods.
        # So the strip narrowed and the table under it did not, on the same
        # screen, from the same request.
        qs = narrow_to_lens(qs, self._lens_branch())
        state = (self.request.query_params.get("is_active") or "").strip().lower()
        if state not in ("all", "false", "0"):
            qs = qs.filter(is_active=True)
        elif state in ("false", "0"):
            qs = qs.filter(is_active=False)
        return qs

    def list(self, request, *args, **kwargs):
        if self.session is None:
            return success_response(data={"periods": [], "day": None})

        rows = list(self.get_queryset())
        raw_day = (request.query_params.get("day") or "").strip()
        context = self.get_serializer_context()

        if not raw_day or raw_day.lower() == "all":
            rows.sort(key=lambda p: (p.day_of_week or 0, p.start_time))
            return success_response(data={
                "day": None,
                "periods": PeriodSerializer(rows, many=True, context=context).data,
            })

        try:
            day = int(raw_day)
            DayOfWeek(day)
        except (TypeError, ValueError):
            raise ValidationError({"day": "Give a weekday as 1 (Monday) to 7."})

        branch = self._lens_branch()
        in_force = periods_in_force(
            self.tenant, self.session, day_of_week=day, branch=branch,
            queryset=self.get_queryset(),
        )
        own = day_has_own_schedule(
            self.tenant, self.session, day_of_week=day, branch=branch,
        )
        return success_response(data={
            "day": day,
            "day_label": DayOfWeek(day).label,
            "has_own_schedule": own,
            # The line the screen renders verbatim, so that a school is told
            # the override is wholesale rather than left to infer it.
            "note": (
                f"{DayOfWeek(day).label} uses its own schedule "
                f"({len(in_force)} period{'' if len(in_force) == 1 else 's'}). "
                f"The everyday schedule does not apply."
                if own else ""
            ),
            "periods": PeriodSerializer(in_force, many=True, context=context).data,
        })

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        writer = PeriodWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        session = self.session_required
        branch = self._write_branch(data)
        day = data.get("day_of_week")

        assert_no_overlap(
            self.tenant, session, branch=branch, day_of_week=day,
            start_time=data["start_time"], end_time=data["end_time"],
        )
        row = Period.objects.create(
            tenant=self.tenant, session=session, branch=branch,
            day_of_week=day,
            # Parked at the end of the day; renumber_day puts it in time
            # order in the same transaction.
            order_index=provisional_order_index(
                self.tenant, session, branch=branch, day_of_week=day,
            ),
            label=data["label"].strip(),
            period_type=data["period_type"],
            start_time=data["start_time"], end_time=data["end_time"],
            is_active=data.get("is_active", True),
        )
        renumber_day(self.tenant, session, branch=branch, day_of_week=day)
        row.refresh_from_db()

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.CREATE,
            entity_type="Period", entity_id=str(row.pk), entity_label=row.label,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{row.label} added to the bell schedule.",
        )
        return success_response(
            f"{row.label} added to the bell schedule.",
            PeriodSerializer(row, context=self.get_serializer_context()).data,
            status=201,
        )


class PeriodDetailView(_PeriodBase, generics.RetrieveUpdateDestroyAPIView):
    """GET, PATCH, DELETE /v1/academics/timetable/periods/<id>/

    docstring-name: Bell schedule period
    """

    def get_permissions(self):
        self.rbac_permission = {
            "PATCH": PERM_TIMETABLE_UPDATE,
            "PUT": PERM_TIMETABLE_UPDATE,
            "DELETE": PERM_TIMETABLE_MANAGE,
        }.get(self.request.method, PERM_TIMETABLE_VIEW)
        return super().get_permissions()

    def get_queryset(self):
        return Period.objects.filter(tenant=self.tenant).select_related("branch")

    def retrieve(self, request, *args, **kwargs):
        return success_response(
            data=self.get_serializer(self.get_object()).data,
        )

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        row = self.get_object()
        writer = PeriodWriteSerializer(row, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data

        branch = self._write_branch(data) if "branch" in data else row.branch
        day = data["day_of_week"] if "day_of_week" in data else row.day_of_week
        start = data.get("start_time", row.start_time)
        end = data.get("end_time", row.end_time)

        assert_no_overlap(
            self.tenant, row.session, branch=branch, day_of_week=day,
            start_time=start, end_time=end, exclude_pk=row.pk,
        )
        was_branch, was_day = row.branch, row.day_of_week
        if (was_branch, was_day) != (branch, day):
            # Moving into another day's numbering: park it past the end there
            # first, or it collides with whatever holds its old index.
            row.order_index = provisional_order_index(
                self.tenant, row.session, branch=branch, day_of_week=day,
                exclude_pk=row.pk,
            )
        row.branch, row.day_of_week = branch, day
        row.start_time, row.end_time = start, end
        if "label" in data:
            row.label = data["label"].strip()
        if "period_type" in data:
            row.period_type = data["period_type"]
        if "is_active" in data:
            row.is_active = data["is_active"]
        row.save()

        renumber_day(self.tenant, row.session, branch=branch, day_of_week=day)
        if (was_branch, was_day) != (branch, day):
            # Moving a period out of a day leaves a hole in the day it left.
            renumber_day(
                self.tenant, row.session, branch=was_branch, day_of_week=was_day,
            )
        row.refresh_from_db()

        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type="Period", entity_id=str(row.pk), entity_label=row.label,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{row.label} updated.",
        )
        return success_response(
            f"{row.label} updated.",
            PeriodSerializer(row, context=self.get_serializer_context()).data,
        )

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        row = self.get_object()
        slots = row.slots.count()
        if slots:
            from ..exceptions import RoomInUse  # same code, different sentence

            raise RoomInUse(
                f"{row.label} holds {slots} lesson"
                f"{'' if slots == 1 else 's'} across the school's timetables. "
                f"Clear them first, or make the period inactive instead.",
                lessons=slots,
            )
        label, session, branch, day = row.label, row.session, row.branch, row.day_of_week
        row.delete()
        renumber_day(self.tenant, session, branch=branch, day_of_week=day)
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="Period", entity_id=str(kwargs.get("pk")), entity_label=label,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{label} deleted.",
        )
        return success_response(f"{label} deleted.")
