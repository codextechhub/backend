"""Everything that moves a student: status, class, and both in bulk."""
from __future__ import annotations

from django.db import transaction
from rest_framework import generics
from rest_framework.views import APIView

from core.response import success_response

from ..constants import (
    BULK_MAX,
    PERM_CLASS_ASSIGN,
    PERM_MANAGE,
    PERM_UPDATE,
    PERM_VIEW,
    StudentStatus,
)
from ..exceptions import (
    BulkTooLarge,
    NothingToMove,
    PlacementRequired,
    StudentsError,
)
from ..models import StudentStatusLog
from ..serializers import (
    AssignClassSerializer,
    BulkAssignSerializer,
    BulkStatusSerializer,
    ClassHistorySerializer,
    ConfirmSerializer,
    ReactivateSerializer,
    ReasonOnlySerializer,
    StatusChangeSerializer,
    StatusLogSerializer,
    StudentDetailSerializer,
    TransferOutSerializer,
)
from ..services import enrolment as enrolment_service
from ..services.placement import place, resolve_class
from ..services.status import assert_can_change, transition
from .base import StudentsViewMixin


class _StudentAction(StudentsViewMixin, APIView):
    """One student, one act. Subclasses declare their key and do the work."""

    key = PERM_MANAGE

    def get_permissions(self):
        self.rbac_permission = self.key
        return super().get_permissions()

    def payload(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def done(self, student, message):
        student.refresh_from_db()
        return success_response(
            message,
            data=StudentDetailSerializer(
                student, context=self.get_serializer_context(),
            ).data,
        )


class ConfirmApplicantView(_StudentAction):
    """POST /v1/students/<id>/confirm/

    docstring-name: Confirm an applicant
    """

    key = PERM_UPDATE
    serializer_class = ConfirmSerializer

    @transaction.atomic
    def post(self, request, pk):
        data = self.payload(request)
        student = self.student(pk)
        enrolment_service.confirm_applicant(
            student, actor=request.user,
            reason=data.get("reason", ""),
            effective_date=data.get("effective_date"),
            number=data.get("student_number") or None,
        )
        return self.done(student, f"{student.full_name} is now enrolled.")


class RejectApplicantView(_StudentAction):
    """POST /v1/students/<id>/reject/

    Closing an application is not withdrawing a student: the applicant was
    never on the roll, and a school looking up why a family did not join needs
    the two apart.

    docstring-name: Reject an applicant
    """

    key = PERM_UPDATE
    serializer_class = ReasonOnlySerializer

    @transaction.atomic
    def post(self, request, pk):
        data = self.payload(request)
        student = self.student(pk)
        transition(
            student, StudentStatus.REJECTED, actor=request.user,
            reason=data["reason"], effective_date=data.get("effective_date"),
        )
        return self.done(student, f"{student.full_name}'s application is closed.")


class WithdrawStudentView(_StudentAction):
    """POST /v1/students/<id>/withdraw/

    docstring-name: Withdraw a student
    """

    serializer_class = ReasonOnlySerializer

    @transaction.atomic
    def post(self, request, pk):
        data = self.payload(request)
        student = self.student(pk)
        transition(
            student, StudentStatus.WITHDRAWN, actor=request.user,
            reason=data["reason"], effective_date=data.get("effective_date"),
        )
        return self.done(student, f"{student.full_name} has been withdrawn.")


class SuspendStudentView(_StudentAction):
    """POST /v1/students/<id>/suspend/

    The active enrolment is deliberately left alone. A suspended child keeps
    their seat, and that is the whole difference between suspending and
    withdrawing.

    docstring-name: Suspend a student
    """

    serializer_class = ReasonOnlySerializer

    @transaction.atomic
    def post(self, request, pk):
        data = self.payload(request)
        student = self.student(pk)
        transition(
            student, StudentStatus.SUSPENDED, actor=request.user,
            reason=data["reason"], effective_date=data.get("effective_date"),
        )
        return self.done(student, f"{student.full_name} is suspended.")


class TransferOutView(_StudentAction):
    """POST /v1/students/<id>/transfer-out/

    docstring-name: Transfer a student out
    """

    serializer_class = TransferOutSerializer

    @transaction.atomic
    def post(self, request, pk):
        data = self.payload(request)
        student = self.student(pk)
        transition(
            student, StudentStatus.TRANSFERRED, actor=request.user,
            reason=data["reason"], effective_date=data.get("effective_date"),
            destination_school=data["destination_school"],
        )
        return self.done(
            student,
            f"{student.full_name} has transferred to {data['destination_school']}.",
        )


class ReactivateStudentView(_StudentAction):
    """POST /v1/students/<id>/reactivate/

    A suspended student comes straight back, because they never lost their
    placement. A withdrawn one needs a class: their old one may have been
    archived or belong to a session that has ended.

    docstring-name: Reactivate a student
    """

    serializer_class = ReactivateSerializer

    @transaction.atomic
    def post(self, request, pk):
        data = self.payload(request)
        student = self.student(pk)

        if student.status == StudentStatus.SUSPENDED:
            transition(
                student, StudentStatus.ACTIVE, actor=request.user,
                reason=data["reason"],
                effective_date=data.get("effective_date"),
            )
            return self.done(student, f"{student.full_name} is active again.")

        class_id = data.get("school_class")
        if not class_id:
            raise PlacementRequired()
        self.assert_holds(PERM_MANAGE, PERM_CLASS_ASSIGN)

        school_class = resolve_class(self.tenant, request.user, class_id)
        transition(
            student, StudentStatus.ENROLLED, actor=request.user,
            reason=data["reason"], effective_date=data.get("effective_date"),
        )
        place(
            student, school_class, actor=request.user,
            effective_date=data.get("effective_date"),
            allow_over_capacity=data.get("allow_over_capacity", False),
        )
        return self.done(
            student, f"{student.full_name} is back on the roll in {school_class.name}.",
        )


class ChangeStatusView(_StudentAction):
    """POST /v1/students/<id>/status/

    The status drawer's single route, for the moves that have no dedicated
    endpoint. Every one of them still goes through the same state machine.

    docstring-name: Change a student's status
    """

    serializer_class = StatusChangeSerializer

    @transaction.atomic
    def post(self, request, pk):
        data = self.payload(request)
        student = self.student(pk)
        assert_can_change(student)
        transition(
            student, data["to_status"], actor=request.user,
            reason=data["reason"], effective_date=data.get("effective_date"),
            destination_school=data.get("destination_school", ""),
        )
        return self.done(
            student,
            f"{student.full_name} is now "
            f"{StudentStatus(data['to_status']).label.lower()}.",
        )


class AssignClassView(_StudentAction):
    """POST /v1/students/<id>/assign-class/

    docstring-name: Assign or transfer a class
    """

    key = PERM_CLASS_ASSIGN
    serializer_class = AssignClassSerializer

    @transaction.atomic
    def post(self, request, pk):
        data = self.payload(request)
        student = self.student(pk)
        if student.status not in (
            StudentStatus.ENROLLED, StudentStatus.ACTIVE, StudentStatus.SUSPENDED,
        ):
            raise NothingToMove(
                f"{student.first_name} is "
                f"{StudentStatus(student.status).label.lower()}, so there is "
                f"no class to move.",
            )
        school_class = resolve_class(self.tenant, request.user, data["school_class"])
        _, was_transfer, over = place(
            student, school_class, actor=request.user,
            reason=data.get("reason", ""),
            effective_date=data.get("effective_date"),
            allow_over_capacity=data.get("allow_over_capacity", False),
        )
        verb = "moved to" if was_transfer else "assigned to"
        message = f"{student.full_name} {verb} {school_class.name}."
        if over:
            message += " The class is now over capacity."
        return self.done(student, message)


class StatusHistoryView(StudentsViewMixin, generics.ListAPIView):
    """GET /v1/students/<id>/status-history/

    docstring-name: A student's status history
    """

    serializer_class = StatusLogSerializer

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get_queryset(self):
        student = self.student(self.kwargs["pk"])
        return StudentStatusLog.objects.filter(student=student).select_related(
            "changed_by",
        )


class ClassHistoryView(StudentsViewMixin, generics.ListAPIView):
    """GET /v1/students/<id>/class-history/

    The promotion trail. Every placement the student has held, with what
    became of it.

    docstring-name: A student's class history
    """

    serializer_class = ClassHistorySerializer

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get_queryset(self):
        student = self.student(self.kwargs["pk"])
        return student.enrolments.select_related(
            "school_class", "session",
        ).order_by("-assigned_at")


class _BulkAction(StudentsViewMixin, APIView):
    """Per-row results, never all-or-nothing.

    A caller who selected twenty students and mistyped one should not lose the
    nineteen, and a bulk route that failed the whole request on one bad id
    would be unusable to them. Each student is its own transaction; the
    response says what happened to each.
    """

    def _students(self, ids):
        from ..services.scoping import get_student_or_404

        if len(ids) > BULK_MAX:
            raise BulkTooLarge(
                f"That is {len(ids)} students. Select {BULK_MAX} or fewer.",
                limit=BULK_MAX, given=len(ids),
            )
        out = []
        for sid in ids:
            try:
                out.append((sid, get_student_or_404(self.tenant, self.request.user, sid)))
            except Exception:  # noqa: BLE001 - a bad id is a per-row result
                out.append((sid, None))
        return out

    @staticmethod
    def _row(sid, student, ok, code="", message=""):
        return {
            "student": sid,
            "name": student.full_name if student is not None else "",
            "ok": ok, "code": code, "message": message,
        }


class BulkAssignClassView(_BulkAction):
    """POST /v1/students/bulk/assign-class/

    docstring-name: Assign a class to several students
    """

    def get_permissions(self):
        self.rbac_permission = PERM_CLASS_ASSIGN
        return super().get_permissions()

    def post(self, request):
        writer = BulkAssignSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        pairs = self._students(data["student_ids"])
        school_class = resolve_class(self.tenant, request.user, data["school_class"])

        # Capacity is checked once against the whole selection rather than per
        # student, so assigning twenty-five children into a class with ten
        # seats warns once about the total instead of fifteen times about the
        # overflow.
        from ..services.placement import assert_capacity

        wanted = sum(1 for _, s in pairs if s is not None)
        assert_capacity(
            school_class, self.active_session, adding=wanted,
            acknowledged=data.get("allow_over_capacity", False),
        )

        results = []
        for sid, student in pairs:
            if student is None:
                results.append(self._row(sid, None, False, "NOT_FOUND",
                                         "No such student at this school."))
                continue
            try:
                with transaction.atomic():
                    place(
                        student, school_class, actor=request.user,
                        reason=data.get("reason", ""),
                        effective_date=data.get("effective_date"),
                        allow_over_capacity=True,
                    )
            except StudentsError as exc:
                results.append(self._row(sid, student, False, exc.error_code, exc.message))
            else:
                results.append(self._row(sid, student, True))

        moved = sum(1 for r in results if r["ok"])
        return success_response(
            f"{moved} of {len(results)} students assigned to {school_class.name}.",
            data={"results": results, "assigned": moved},
        )


class BulkStatusView(_BulkAction):
    """POST /v1/students/bulk/status/

    docstring-name: Change several students' status
    """

    def get_permissions(self):
        self.rbac_permission = PERM_MANAGE
        return super().get_permissions()

    def post(self, request):
        writer = BulkStatusSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        results = []
        for sid, student in self._students(data["student_ids"]):
            if student is None:
                results.append(self._row(sid, None, False, "NOT_FOUND",
                                         "No such student at this school."))
                continue
            try:
                with transaction.atomic():
                    transition(
                        student, data["to_status"], actor=request.user,
                        reason=data["reason"],
                        effective_date=data.get("effective_date"),
                        destination_school=data.get("destination_school", ""),
                    )
            except StudentsError as exc:
                results.append(self._row(sid, student, False, exc.error_code, exc.message))
            else:
                results.append(self._row(sid, student, True))

        changed = sum(1 for r in results if r["ok"])
        label = StudentStatus(data["to_status"]).label.lower()
        return success_response(
            f"{changed} of {len(results)} students are now {label}.",
            data={"results": results, "changed": changed},
        )
