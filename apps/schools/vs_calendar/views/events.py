"""Calendar events: the dated things that hang off a school year.

Holidays, mid-term breaks, exam periods, PTA meetings, sports days. Each sits
inside one session, carries a branch scope, and may narrow further to particular
levels or classes.

**Three validations, and only one of them refuses.** A date outside the session
is refused, because it belongs to a year that is not this one. A date inside the
session but outside every term is *warned* and saved - the December break is a
real entry on a real calendar and refusing it would make the calendar wrong to
protect a rule nobody asked for. An overlap with another event of the same type
and scope is warned and saved too: two mid-term breaks in one term is usually a
mistake and occasionally two branches' arrangements being recorded, and the
server cannot tell which.

**The audience is the narrowing the design did not draw.** Lekki Branch holds a
Speech Day for the primary school; Primary 4 A is off timetable and JSS1 A and
JSS1 B are not. Without it the event is either the whole of Lekki - so JSS1's
teachers see a closure that is not theirs and their teaching-day count loses a
day they actually taught - or it is not recorded and the primary teachers turn
up. No rows means everybody in the branch scope, which is the default and the
common case.
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Prefetch, Q
from rest_framework import generics
from rest_framework.exceptions import ValidationError

from core.response import success_response
from vs_audit.models import AuditActionType, AuditModuleKey
from vs_audit.services import emit_audit_event

from ..constants import (
    PERM_CALENDAR_CREATE,
    PERM_CALENDAR_MANAGE,
    PERM_CALENDAR_UPDATE,
    PERM_CALENDAR_VIEW,
    WARN_EVENT_OUTSIDE_ANY_TERM,
    WARN_EVENT_OVERLAP,
)
from ..exceptions import EventAudienceOutOfScope, EventOutsideSession
from ..models import CalendarEvent, CalendarEventAudience
from ..serializers import CalendarEventSerializer, CalendarEventWriteSerializer
from ..services.calendar import term_of
from ..services.scoping import UNSET, raised_branch, scope_to_visible_branches
from .base import CalendarViewMixin


def _fmt(day) -> str:
    """A date the way the product writes it: 12 Sep 2025."""
    return f"{day.day} {day:%b %Y}"


def _range(start, end) -> str:
    return _fmt(start) if start == end else f"{_fmt(start)} - {_fmt(end)}"


class _EventBase(CalendarViewMixin):
    serializer_class = CalendarEventSerializer

    def _base(self):
        return (
            CalendarEvent.objects.filter(tenant=self.tenant, session=self.session)
            .select_related("branch", "session")
            .prefetch_related(
                Prefetch(
                    "audience",
                    queryset=CalendarEventAudience.objects.select_related(
                        "level", "school_class",
                    ),
                ),
            )
        )

    def _filtered(self, qs):
        params = self.request.query_params
        qs = scope_to_visible_branches(qs, self.request.user, self.tenant)

        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(name__icontains=search) | Q(description__icontains=search))

        event_type = (params.get("type") or "").strip()
        if event_type and event_type.lower() != "all":
            qs = qs.filter(event_type=event_type.upper())

        term = (params.get("term") or "").strip()
        if term and term.lower() != "all":
            # A term is a date range, not a column: an event "in First Term" is
            # one whose dates fall inside it. Storing the term on the row would
            # give the school two truths the day a term's dates were corrected.
            row = self.session.terms.filter(pk=term).first() if self.session else None
            if row is None:
                raise ValidationError({"term": "No such term in this session."})
            qs = qs.filter(start_date__lte=row.end_date, end_date__gte=row.start_date)

        scope = (params.get("scope") or "").strip()
        if scope and scope.lower() != "all" and self.multi_branch:
            if scope.lower() in ("school", "shared", "none"):
                qs = qs.filter(branch__isnull=True)
            else:
                from vs_tenants.references import resolve_branch_reference

                qs = qs.filter(
                    branch=resolve_branch_reference(self.tenant, scope, "scope"),
                )

        start = (params.get("from") or "").strip()
        end = (params.get("to") or "").strip()
        if start:
            qs = qs.filter(end_date__gte=start)
        if end:
            qs = qs.filter(start_date__lte=end)
        return qs

    # ── writes ────────────────────────────────────────────────────────────

    def _assert_inside_session(self, session, start, end):
        if start < session.start_date or end > session.end_date:
            raise EventOutsideSession(
                f"This date is outside {session.name} "
                f"({_range(session.start_date, session.end_date)}).",
                session=session.name,
            )

    def _warnings(self, session, *, start, end, event_type, branch, exclude_pk=None):
        out = []
        if term_of(session, start) is None:
            out.append({
                "code": WARN_EVENT_OUTSIDE_ANY_TERM,
                "detail": (
                    f"This date falls outside every term in {session.name}. It "
                    f"will show on the calendar and be flagged in the events "
                    f"list."
                ),
            })
        overlap = (
            CalendarEvent.objects.filter(
                tenant=self.tenant, session=session, event_type=event_type,
                branch=branch, start_date__lte=end, end_date__gte=start,
            )
            .exclude(pk=exclude_pk)
            .first()
        )
        if overlap is not None:
            out.append({
                "code": WARN_EVENT_OVERLAP,
                "detail": (
                    f"This overlaps {overlap.name} "
                    f"({_range(overlap.start_date, overlap.end_date)}), which "
                    f"is the same type and scope."
                ),
            })
        return out

    def _write_audience(self, event, rows):
        """Replace the event's audience wholesale.

        Wholesale rather than merged: an edit that removed JSS1 from the list
        has to remove it, and a merge would only ever add.
        """
        from schools.vs_academics.models import Level, SchoolClass

        event.audience.all().delete()
        if not rows:
            return

        created = []
        for entry in rows:
            kind = str(entry.get("type") or "").lower()
            pk = entry.get("id")
            if kind == "level":
                target = Level.objects.filter(
                    tenant=self.tenant, session=event.session, pk=pk,
                ).first()
                if target is None:
                    raise ValidationError({"audience": "No such level in this year."})
                self._assert_audience_scope(event, target, target.name)
                created.append(CalendarEventAudience(
                    tenant=self.tenant, event=event, level=target,
                ))
            elif kind == "class":
                target = SchoolClass.objects.filter(
                    tenant=self.tenant, session=event.session, pk=pk,
                ).first()
                if target is None:
                    raise ValidationError({"audience": "No such class in this year."})
                self._assert_audience_scope(event, target, target.name)
                created.append(CalendarEventAudience(
                    tenant=self.tenant, event=event, school_class=target,
                ))
            else:
                raise ValidationError({
                    "audience": 'Each entry needs a type of "level" or "class".',
                })
        CalendarEventAudience.objects.bulk_create(created)

    def _assert_audience_scope(self, event, target, label):
        """A branch event may only narrow to things at that branch.

        Narrowing a Lekki event to an Ikeja class produces an event nobody can
        see the reason for: it shows on Ikeja's calendar because of the class
        and not on it because of the branch.
        """
        if event.branch_id is None:
            return
        if target.branch_id is not None and target.branch_id != event.branch_id:
            raise EventAudienceOutOfScope(
                f"{label} is not at {event.branch.name}, so this event cannot "
                f"be narrowed to it.",
                target=label, branch=event.branch.name,
            )


class EventListCreateView(_EventBase, generics.ListCreateAPIView):
    """GET, POST /v1/academics/calendar/events/

    docstring-name: Calendar events
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_CALENDAR_CREATE if self.request.method == "POST"
            else PERM_CALENDAR_VIEW
        )
        return super().get_permissions()

    def get_queryset(self):
        if self.session is None:
            return CalendarEvent.objects.none()
        return self._filtered(self._base())

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        writer = CalendarEventWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        session = self.session_required

        requested = data["branch"] if "branch" in data else UNSET
        branch = raised_branch(request.user, self.tenant, requested)
        self._assert_inside_session(session, data["start_date"], data["end_date"])

        row = CalendarEvent.objects.create(
            tenant=self.tenant, session=session, branch=branch,
            name=data["name"].strip(),
            event_type=data["event_type"],
            start_date=data["start_date"], end_date=data["end_date"],
            closes_school=data.get("closes_school", False),
            description=(data.get("description") or "").strip(),
            created_by=request.user,
        )
        self._write_audience(row, data.get("audience") or [])

        warnings = self._warnings(
            session, start=row.start_date, end=row.end_date,
            event_type=row.event_type, branch=branch, exclude_pk=row.pk,
        )
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.CREATE,
            entity_type="CalendarEvent", entity_id=str(row.pk),
            entity_label=row.name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{row.name} added to the calendar.",
        )
        payload = CalendarEventSerializer(
            self._base().get(pk=row.pk), context=self.get_serializer_context(),
        ).data
        payload["warnings"] = warnings
        return success_response(f"{row.name} added to the calendar.", payload, status=201)


class EventDetailView(_EventBase, generics.RetrieveUpdateDestroyAPIView):
    """GET, PATCH, DELETE /v1/academics/calendar/events/<id>/

    docstring-name: Calendar event
    """

    def get_permissions(self):
        self.rbac_permission = {
            "PATCH": PERM_CALENDAR_UPDATE,
            "PUT": PERM_CALENDAR_UPDATE,
            # SENSITIVE and school_admin only, deliberately: a branch adds and
            # edits its own entries, and removing one from the school's
            # calendar is the school's call.
            "DELETE": PERM_CALENDAR_MANAGE,
        }.get(self.request.method, PERM_CALENDAR_VIEW)
        return super().get_permissions()

    def get_queryset(self):
        return (
            CalendarEvent.objects.filter(tenant=self.tenant)
            .select_related("branch", "session")
            .prefetch_related(
                Prefetch(
                    "audience",
                    queryset=CalendarEventAudience.objects.select_related(
                        "level", "school_class",
                    ),
                ),
            )
        )

    def retrieve(self, request, *args, **kwargs):
        row = self.get_object()
        data = CalendarEventSerializer(
            row, context=self.get_serializer_context(),
        ).data
        # The exam-period drawer links forward to its schedule, and needs to
        # know whether one has been built yet.
        if row.event_type == "EXAM_PERIOD":
            exam = row.exams.first()
            data["exam"] = (
                {"id": exam.pk, "name": exam.name, "status": exam.status}
                if exam else None
            )
        return success_response(data=data)

    @transaction.atomic
    def update(self, request, *args, **kwargs):
        row = self.get_object()
        writer = CalendarEventWriteSerializer(row, data=request.data, partial=True)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data

        if "branch" in data:
            row.branch = raised_branch(request.user, self.tenant, data["branch"])
        for field in ("name", "event_type", "start_date", "end_date",
                      "closes_school", "description"):
            if field in data:
                setattr(row, field, data[field])
        row.name = row.name.strip()
        row.description = (row.description or "").strip()
        self._assert_inside_session(row.session, row.start_date, row.end_date)
        row.save()

        if "audience" in data:
            self._write_audience(row, data.get("audience") or [])

        warnings = self._warnings(
            row.session, start=row.start_date, end=row.end_date,
            event_type=row.event_type, branch=row.branch, exclude_pk=row.pk,
        )
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.UPDATE,
            entity_type="CalendarEvent", entity_id=str(row.pk),
            entity_label=row.name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{row.name} updated.",
        )
        payload = CalendarEventSerializer(
            self.get_queryset().get(pk=row.pk),
            context=self.get_serializer_context(),
        ).data
        payload["warnings"] = warnings
        return success_response(f"{row.name} updated.", payload)

    @transaction.atomic
    def destroy(self, request, *args, **kwargs):
        row = self.get_object()
        if row.exams.exists():
            from ..exceptions import RoomInUse

            raise RoomInUse(
                f"{row.name} holds an exam timetable, so it cannot be removed "
                f"from the calendar. Delete the exam timetable first.",
            )
        name = row.name
        row.delete()
        emit_audit_event(
            module_key=AuditModuleKey.ACADEMICS,
            action_type=AuditActionType.DELETE,
            entity_type="CalendarEvent", entity_id=str(kwargs.get("pk")),
            entity_label=name,
            tenant=self.tenant, actor_user=request.user,
            summary=f"{name} deleted.",
        )
        return success_response(f"{name} deleted.")
