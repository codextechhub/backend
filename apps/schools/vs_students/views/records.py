"""The reads that hang off a profile: documents, subjects, history, rosters."""
from __future__ import annotations

from django.db import transaction
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response
from vs_history.as_at import parse_as_at

from vs_rbac.field_enforcement import assert_writable, can_read

from ..constants import (
    PERM_CLASS_VIEW,
    PERM_UPDATE,
    PERM_VIEW,
    DocumentType,
)
from ..models import StudentDocument
from ..serializers import (
    AdmissionPolicySerializer,
    DocumentSerializer,
    DocumentUploadSerializer,
    StudentListSerializer,
)
from ..services import documents as document_service
from ..services.placement import (
    capacity_state,
    class_seats,
    resolve_class,
    roster,
)
from .base import StudentsViewMixin


#: The registered field the passport photograph answers to.
PHOTO_KEY = "school.students.photo"


def assert_photo_writable(request, document_type):
    """Refuse a passport photograph the caller's Write switch does not reach.

    The passport photograph is the student's face on every screen, registered
    as ``school.students.photo``. It is attached and removed here rather than
    through a serializer, so the switch is asked here.
    """
    if document_type == DocumentType.PASSPORT_PHOTO:
        assert_writable(request, "school.students", {"photo_url": None})


def hide_unreadable_photo(request, rows):
    """Drop the passport photograph's link for a caller who may not read it.

    The row stays, attached or not, so the checklist still says the document
    is held; only the file is withheld, as ``photo_url`` is on the profile.
    """
    if can_read(request, PHOTO_KEY):
        return rows
    return [
        {**row, "url": ""} if row["document_type"] == DocumentType.PASSPORT_PHOTO else row
        for row in rows
    ]


class StudentDocumentsView(StudentsViewMixin, APIView):
    """GET, POST /v1/students/<id>/documents/

    ``?as_at=YYYY-MM-DD`` answers as at the end of that day (``as_at.py``).

    docstring-name: A student's documents
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_UPDATE if self.request.method == "POST" else PERM_VIEW
        )
        return super().get_permissions()

    def get(self, request, pk):
        from .. import as_at as past

        student = self.student(pk)
        as_at = parse_as_at(request)
        if as_at is None:
            rows = document_service.checklist(student, request=request)
        else:
            past.student_at(student, as_at)
            rows = past.checklist_at(student.pk, as_at, request=request)
        rows = hide_unreadable_photo(request, rows)
        return success_response(data=DocumentSerializer(rows, many=True).data)

    @transaction.atomic
    def post(self, request, pk):
        student = self.student(pk)
        writer = DocumentUploadSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        assert_photo_writable(request, writer.validated_data["document_type"])
        doc = document_service.attach(
            student,
            document_type=writer.validated_data["document_type"],
            upload=writer.validated_data["file"], actor=request.user,
        )
        return success_response(
            f"{doc.get_document_type_display()} attached.",
            data=DocumentSerializer(
                hide_unreadable_photo(
                    request, document_service.checklist(student, request=request),
                ),
                many=True,
            ).data,
            status=201,
        )


class StudentDocumentDetailView(StudentsViewMixin, APIView):
    """DELETE /v1/students/<id>/documents/<doc_id>/

    docstring-name: One of a student's documents
    """

    def get_permissions(self):
        self.rbac_permission = PERM_UPDATE
        return super().get_permissions()

    @transaction.atomic
    def delete(self, request, pk, doc_id):
        student = self.student(pk)
        doc = StudentDocument.objects.filter(
            tenant=self.tenant, student=student, pk=doc_id,
        ).first()
        if doc is None:
            raise NotFound("No such document on this student's record.")
        assert_photo_writable(request, doc.document_type)
        document_service.remove(student, doc, actor=request.user)
        return success_response("Document removed.")


class StudentSubjectsView(StudentsViewMixin, APIView):
    """GET /v1/students/<id>/subjects/

    ``?as_at=YYYY-MM-DD`` answers as at the end of that day (``as_at.py``).

    Read from Academic Structure for the level of the student's current class.
    A student with no class gets an empty list rather than a 404: having no
    class is an ordinary state, not a missing page.

    docstring-name: A student's subjects
    """

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get(self, request, pk):
        from schools.vs_academics.models import SubjectOffering

        from .. import as_at as past

        student = self.student(pk)
        as_at = parse_as_at(request)
        if as_at is None:
            enrolment = student.enrolments.filter(is_active=True).select_related(
                "school_class", "school_class__level",
            ).first()
        else:
            past.student_at(student, as_at)
            enrolment = past.active_enrolment(past.enrolments_at(student.pk, as_at))
        if enrolment is None or enrolment.school_class.level_id is None:
            return success_response(data=[])

        offerings = SubjectOffering.objects.filter(
            tenant=self.tenant, level_id=enrolment.school_class.level_id,
            subject__is_active=True,
        ).select_related("subject")
        return success_response(data=[
            {
                "id": o.subject_id, "name": o.subject.name,
                "code": o.subject.code,
                "is_core": o.is_core if o.is_core is not None else o.subject.is_core,
            }
            for o in offerings
        ])


class StudentHistoryView(StudentsViewMixin, APIView):
    """GET /v1/students/<id>/history/

    ``?as_at=YYYY-MM-DD`` answers as at the end of that day (``as_at.py``).

    Wider than the status log. The profile's history tab shows status changes,
    class moves, guardian links and field edits in one stream, so this merges
    the module's own log with the platform's audit trail rather than
    duplicating either.

    docstring-name: A student's record history
    """

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get(self, request, pk):
        from vs_audit.models import AuditEvent

        from .. import as_at as past

        student = self.student(pk)
        as_at = parse_as_at(request)
        if as_at is not None:
            past.student_at(student, as_at)
        entries = [
            {
                "kind": "status",
                "text": self._status_text(row),
                "when": row.changed_at,
                "actor": self._actor(row.changed_by),
            }
            for row in student.status_logs.select_related("changed_by")
        ]
        events = AuditEvent.objects.filter(
            tenant=self.tenant, entity_type="Student", entity_id=str(student.pk),
        # AuditEvent stamps ``event_at``, not ``created_at`` - it records when
        # the action happened, which is not always when the row was written.
        # Both names below were guessed from the convention the other models in
        # this repo follow, and the tab answered 500 for every student because
        # of it.
        ).select_related("actor_user").order_by("-event_at")[:200]
        for event in events:
            entries.append({
                "kind": self._kind(event.action_type),
                "text": event.summary,
                "when": event.event_at,
                "actor": self._actor(event.actor_user),
            })
        if as_at is not None:
            entries = [entry for entry in entries if as_at.includes(entry["when"])]
        entries.sort(key=lambda e: e["when"], reverse=True)

        page = self.paginate_queryset(entries)
        if page is not None:
            return self.get_paginated_response(page)
        return success_response(data=entries)

    @staticmethod
    def _kind(action_type):
        if "GUARDIAN" in action_type:
            return "guardian"
        if "CLASS" in action_type or "PROMOTION" in action_type:
            return "class"
        if "DOCUMENT" in action_type:
            return "document"
        if action_type == "UPDATE":
            return "edit"
        return "status"

    @staticmethod
    def _status_text(row):
        from ..constants import StudentStatus

        to_label = StudentStatus(row.to_status).label
        if not row.from_status:
            return f"Record created as {to_label}."
        return (
            f"Status moved from {StudentStatus(row.from_status).label} to "
            f"{to_label}." + (f" Reason: {row.reason}" if row.reason else "")
        )

    @staticmethod
    def _actor(user):
        # A name, never an email address: this tab is read by anyone who can
        # see the student, and a colleague's address is not theirs to give out.
        if user is None:
            return "System"
        return (
            getattr(user, "full_name", None)
            or getattr(user, "first_name", "")
            or "System"
        )


class ClassRosterView(StudentsViewMixin, generics.ListAPIView):
    """GET /v1/students/classes/<class_id>/roster/

    Mounted here rather than under /v1/academics/, because the enrolment row is
    this module's and registering it there would make a vs_academics view import a
    school app it must not know about.

    docstring-name: A class roster
    """

    serializer_class = StudentListSerializer

    @property
    def class_session(self):
        """The year this register belongs to: the CLASS's, not the school's.

        A class belongs to a year, so a school has one JSS1 A per session and an
        enrolment names the same year its class does. Reading the roster against
        the ACTIVE year therefore answered for the wrong one the moment the
        class was not this year's: SSS2 B holding twenty-five children reported
        "0 of 30 seats used" and an empty register, with nothing on the page
        saying which year it had looked in.

        Taking it from the class also means this route needs no ``?session=``.
        The class already names the year, so a parameter could only ever
        disagree with it - and the module has a rule for that disagreement,
        which is to refuse it.
        """
        return self._class.session

    def get_permissions(self):
        # Two keys: it is a fact about a class as much as about its students,
        # and a caller who cannot see classes has no business reading one's
        # register.
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get_queryset(self):
        self.assert_holds(PERM_VIEW, PERM_CLASS_VIEW)
        school_class = resolve_class(
            self.tenant, self.request.user, self.kwargs["class_id"],
        )
        self._class = school_class
        # The ROWS narrow. The seat count below deliberately does not - see
        # list(). A branch-bound caller already sees only their own children
        # here; ``?branch=`` lets a school-wide caller ask the same question.
        return self.narrow_to_branch(
            roster(
                self.tenant, self.request.user, school_class,
                self.class_session,
            ),
        ).select_related("branch").prefetch_related(
            "enrolments__school_class", "guardian_links__guardian",
            document_service.photo_prefetch(),
        )

    def list(self, request, *args, **kwargs):
        """The roster, with the class's own seat count beside it.

        The count is a top-level sibling of ``data`` and not a key inside it,
        because ``data`` is the paginated LIST of students - writing into it
        silently did nothing and the screen showed no seats at all.

        The seat count is deliberately NOT narrowed by branch. The roster rows
        are - a Lekki admin sees Lekki's children in a school-wide class - but
        "29 of 30 seats used" is a fact about the class, and a branch admin who
        was shown 12 of 30 would fill a class that is already full.
        """
        response = super().list(request, *args, **kwargs)
        used, cap, _ = capacity_state(self._class, self.class_session, adding=0)
        response.data["seats_used"] = used
        response.data["capacity"] = cap
        response.data["class_name"] = self._class.name
        return response


class ClassSeatsView(StudentsViewMixin, APIView):
    """GET /v1/students/classes/seats/

    Every class with its live seat count, in one request.

    The pickers that place a child - the enrolment form, the transfer drawer
    and the assign bar - all render "JSS1 A - 26/30" for every class at once.
    Without this each of them either showed no numbers or would have needed a
    roster request per class, which grows with the school.

    Not paginated: this is a dropdown's contents, and a school runs tens of
    classes rather than thousands. Answers with an empty list rather than
    NO_ACTIVE_SESSION when the school is between years, so a form can still be
    opened and say what it does not know.

    docstring-name: Class seat counts
    """

    def get_permissions(self):
        # Two keys: it is a fact about classes as much as about placement, and
        # a caller who cannot see classes has no business reading their loads.
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get(self, request):
        self.assert_holds(PERM_VIEW, PERM_CLASS_VIEW)
        # The year being READ, not the school's current one: a class belongs to
        # a year, so last year's classes had last year's loads.
        session = self.session_filter or self.session_or_none
        if session is None:
            return success_response(data=[])
        return success_response(data=class_seats(
            self.tenant, request.user, session, branch=self.branch_filter,
        ))


class AdmissionPolicyView(StudentsViewMixin, APIView):
    """GET, PUT /v1/students/admission-number-policy/

    The school's own rule about admission numbers. Reading it needs only
    ``view`` because the enrolment form has to render the hint; setting it
    needs ``update``.

    docstring-name: Admission number policy
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_UPDATE if self.request.method == "PUT" else PERM_VIEW
        )
        return super().get_permissions()

    def get(self, request):
        from ..services.policy import read_policy, suggest_number

        policy = read_policy(self.tenant)
        # A suggestion, not a reservation: two registrars enrolling at once can
        # be handed the same number, and the unique constraint is what actually
        # stops the collision. "" means the school's series cannot be continued
        # honestly - see suggest_number for when that happens.
        return success_response(data={
            **policy.as_dict(),
            "suggestion": suggest_number(self.tenant, policy=policy),
        })

    def put(self, request):
        from ..services.policy import write_policy

        writer = AdmissionPolicySerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        policy = write_policy(
            self.tenant, request.user,
            required=data["required"], pattern=data["pattern"], hint=data["hint"],
        )
        return success_response(
            "Admission number rule saved.", data=policy.as_dict(),
        )
