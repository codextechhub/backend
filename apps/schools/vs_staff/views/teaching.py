"""Teaching duties, the class teacher, and the coverage grid.

Closed before go-live: teaching belongs to a school year, and a school still
onboarding has not started one.

FRD M12 v2.1, FR-011 and FR-017.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.views import APIView

from core.pagination import XVSPagination
from core.response import success_response

from ..constants import PERM_ASSIGN, PERM_VIEW, TeachingPart
from ..models import TeachingAssignment
from ..serializers import (
    ClassTeacherSerializer,
    TeachingAssignmentSerializer,
    TeachingWriteSerializer,
)
from ..services import teaching
from .base import StaffViewMixin


class _SessionMixin:
    def resolve_session(self, value=None):
        """The year being worked in, defaulting to the active one.

        Resolved inside this tenant, so a session id from another school is a
        404 rather than a silent read of somebody else's year.
        """
        from schools.vs_academics.models import AcademicSession

        if value in (None, ""):
            session = self.active_session
            if session is None:
                raise ValidationError({
                    "session": "This school has no active year, so there is "
                               "nothing to teach yet.",
                })
            return session
        session = AcademicSession.objects.filter(
            tenant=self.tenant, pk=value,
        ).first()
        if session is None:
            raise NotFound("No such school year at this school.")
        return session


class StaffTeachingView(StaffViewMixin, _SessionMixin, APIView):
    """GET/POST /v1/i/me/staff/<id>/teaching/ - what one person teaches.

    An archived year still returns its assignments and refuses new ones: who
    taught what last year is the record a school will be asked for.

    docstring-name: A staff member's teaching duties
    """

    @property
    def rbac_permission(self):
        method = (getattr(self.request, "method", "") or "").upper()
        return PERM_VIEW if method in ("GET", "HEAD", "OPTIONS") else PERM_ASSIGN

    def get(self, request, pk):
        staff = self.get_staff(pk)
        session = self.resolve_session(request.query_params.get("session"))
        rows = (
            staff.teaching_assignments.filter(session=session)
            .select_related("subject", "school_class", "staff__user")
        )
        return success_response(data={
            "session": {"id": session.pk, "name": session.name},
            "assignments": TeachingAssignmentSerializer(rows, many=True).data,
            "load": rows.count(),
            # Uncoloured and with nothing to compare it against. A subject's
            # weekly frequency is recorded nowhere and no contract records a
            # maximum, so a threshold here would be invented.
            "load_note": "A count of assignments. There is no target to compare it against.",
        })

    def post(self, request, pk):
        staff = self.get_staff(pk)
        payload = TeachingWriteSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data
        session = self.resolve_session(data.get("session"))

        school_class, subject = self._resolve_pair(
            data["school_class"], data["subject"], session,
        )
        assignment = teaching.assign(
            tenant=self.tenant, staff=staff, school_class=school_class,
            subject=subject, session=session, part=data["part"],
            actor=request.user,
        )
        return success_response(
            message="Teaching duty recorded.",
            data=TeachingAssignmentSerializer(assignment).data,
            status=status.HTTP_201_CREATED,
        )

    def _resolve_pair(self, class_id, subject_id, session):
        """A class and a subject, both inside this school and this year.

        The class is narrowed by the caller's branches, inclusive of the shared
        ones, so a branch administrator cannot staff another branch's class by
        posting its id.
        """
        from schools.vs_academics.models import Subject

        school_class = (
            teaching._visible_classes(self.tenant, self.request.user, session)
            .filter(pk=class_id)
            .first()
        )
        if school_class is None:
            raise NotFound("No such class at this school.")
        subject = Subject.objects.filter(tenant=self.tenant, pk=subject_id).first()
        if subject is None:
            raise NotFound("No such subject at this school.")
        return school_class, subject


class TeachingAssignmentDetailView(StaffViewMixin, APIView):
    """PATCH/DELETE /v1/i/me/staff/teaching/<id>/ - the part, or the removal.

    PATCH is Make lead and Step back. Promoting somebody where the pairing
    already has a lead is refused with the current holder named, so a school
    displaces a colleague deliberately rather than discovering afterwards that
    the person who had the class no longer does.

    docstring-name: One teaching duty
    """

    rbac_permission = PERM_ASSIGN

    def _row(self, pk):
        row = (
            TeachingAssignment.objects.filter(tenant=self.tenant, pk=pk)
            .select_related(
                "staff__user", "staff__branch", "subject", "school_class", "session",
            )
            .first()
        )
        if row is None:
            raise NotFound("No such teaching duty at this school.")
        self.get_staff(row.staff_id)
        return row

    def patch(self, request, pk):
        row = self._row(pk)
        part = request.data.get("part")
        if part not in dict(TeachingPart.choices):
            raise ValidationError({"part": "Choose lead or assistant."})
        teaching.set_part(row, part, actor=request.user)
        return success_response(
            message="Updated.", data=TeachingAssignmentSerializer(row).data,
        )

    def delete(self, request, pk):
        row = self._row(pk)
        teaching.unassign(row, actor=request.user)
        return success_response(message="Teaching duty removed.")


class ClassTeacherView(StaffViewMixin, APIView):
    """PUT /v1/i/me/staff/teaching/class-teacher/ - designate or clear.

    Written to the class rather than to an assignment, because the designation
    is unique per class by construction and a copy on a row that is not would
    let two people claim it.

    docstring-name: Set a class teacher
    """

    rbac_permission = PERM_ASSIGN

    def put(self, request):
        payload = ClassTeacherSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        from schools.vs_academics.models import SchoolClass

        school_class = (
            SchoolClass.objects.filter(tenant=self.tenant, pk=data["school_class"])
            .select_related("session")
            .first()
        )
        if school_class is None:
            raise NotFound("No such class at this school.")
        teaching.assert_session_open(school_class.session)

        staff = self.get_staff(data["staff"]) if data.get("staff") else None
        teaching.set_class_teacher(
            school_class, staff, actor=request.user, tenant=self.tenant,
        )
        return success_response(
            message=(
                f"{school_class.name} has a class teacher."
                if staff is not None
                else f"{school_class.name} has no class teacher."
            ),
            data={
                "school_class": {"id": school_class.pk, "name": school_class.name},
                "class_teacher": (
                    {
                        "id": staff.pk,
                        "name": f"{staff.user.first_name} {staff.user.last_name}".strip(),
                    }
                    if staff is not None else None
                ),
            },
        )


class TeachingCoverageView(StaffViewMixin, _SessionMixin, APIView):
    """GET /v1/i/me/staff/teaching/coverage/?session= - what has a teacher.

    Two kinds of gap, counted separately, because they are different problems. A
    **coverage gap** is a pairing nobody teaches. A **lead gap** is a pairing
    with assistants and no lead: it is being taught and nobody owns the marks.

    This is the one genuinely computable warning in the module, because it
    counts rows. It says nothing about whether the assigned teacher is a good
    choice, how many periods the subject needs, or whether anybody is
    overloaded, and no screen may imply otherwise.

    Paginated and capped: a secondary school with forty classes and fifteen
    subjects is six hundred pairings and must not be one response.

    docstring-name: Teaching coverage
    """

    rbac_permission = PERM_VIEW
    pagination_class = XVSPagination

    def get(self, request):
        session = self.resolve_session(request.query_params.get("session"))
        from schools.vs_academics.models import SessionStatus

        if session.status == SessionStatus.ARCHIVED:
            from ..exceptions import SessionArchived

            raise SessionArchived(
                f"{session.name} has been archived, so it has no coverage "
                f"question to answer.",
                session=session.name,
            )

        rows, coverage_gaps, lead_gaps = teaching.coverage(
            self.tenant, request.user, session,
        )
        if request.query_params.get("only_gaps") == "true":
            rows = [
                row for row in rows
                if row["lead"] is None or not (row["lead"] or row["assistants"])
            ]

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(rows, request, view=self)
        response = paginator.get_paginated_response([
            self._cell(row) for row in page
        ])
        response.data["session"] = {"id": session.pk, "name": session.name}
        response.data["coverage_gaps"] = coverage_gaps
        response.data["lead_gaps"] = lead_gaps
        response.data["headline"] = self._headline(coverage_gaps, lead_gaps)
        return response

    def _cell(self, row):
        def person(assignment):
            user = assignment.staff.user
            return {
                "assignment_id": assignment.pk,
                "staff_id": assignment.staff_id,
                "name": f"{user.first_name} {user.last_name}".strip(),
            }

        return {
            "class_id": row["school_class"].pk,
            "class_name": row["school_class"].name,
            "subject_id": row["subject"].pk,
            "subject_name": row["subject"].name,
            "lead": person(row["lead"]) if row["lead"] else None,
            "assistants": [person(item) for item in row["assistants"]],
            "gap": not row["lead"] and not row["assistants"],
            "lead_gap": row["lead"] is None and bool(row["assistants"]),
        }

    def _headline(self, coverage_gaps, lead_gaps) -> str:
        if not coverage_gaps and not lead_gaps:
            return "Every subject is covered, and each has a lead."
        parts = []
        if coverage_gaps:
            parts.append(
                f"{coverage_gaps} "
                + ("pair has nobody" if coverage_gaps == 1 else "pairs have nobody"),
            )
        if lead_gaps:
            parts.append(
                f"{lead_gaps} "
                + ("has no lead" if lead_gaps == 1 else "have no lead"),
            )
        return " and ".join(parts)
