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
    PERM_SETTINGS_UPDATE,
    PERM_UPDATE,
    PERM_VIEW,
    DocumentType,
)
from ..models import StudentDocument
from ..serializers import (
    AdmissionPolicySerializer,
    EnrolmentRulesSerializer,
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
            rows = past.checklist_at(
                student.pk, as_at, request=request, tenant=self.tenant,
            )
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
    class moves, branch moves, guardian links and field edits in one stream
    (``kind`` is ``status``, ``class``, ``branch``, ``guardian``, ``document``
    or ``edit``), so this merges the module's own log with the platform's
    audit trail rather than duplicating either.

    The reason behind a status move, or behind an applicant's move between
    admission stages, is a Field Access field
    (``school.students.status_reason``), held to the same Read switch here as
    on the status history and the profile's suspension block. It never travels
    inside ``text``, because a sentence cannot be partly withheld: an entry for
    either move carries it as ``reason``, and the key is absent for a caller
    whose roles do not grant Read. An audit summary that ends in the reason is
    cut where the reason begins (``SUMMARY_REASON_MARKER``), since the trail
    is immutable and can still hold such rows. Which rows carry a reason is
    :mod:`vs_audit.protected_words`'s answer, shared with the audit log.

    ``AuditEvent`` is ordered by ``event_at``, when the action happened, which
    is not always when its row was written.

    docstring-name: A student's record history
    """

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get(self, request, pk):
        from vs_audit.models import AuditEvent

        from .. import as_at as past
        from ..field_access import STATUS_REASON_FIELD

        student = self.student(pk)
        as_at = parse_as_at(request)
        if as_at is not None:
            past.student_at(student, as_at)
        reason_open = can_read(request, STATUS_REASON_FIELD)
        entries = [
            self._entry(
                "status", self._status_text(row), row.changed_at,
                row.changed_by, row.reason if reason_open else None,
            )
            for row in student.status_logs.select_related("changed_by")
        ]
        events = AuditEvent.objects.filter(
            tenant=self.tenant, entity_type="Student", entity_id=str(student.pk),
        ).select_related("actor_user").order_by("-event_at")[:200]
        for event in events:
            text, reason = self._audit_text(event)
            entries.append(self._entry(
                self._kind(event.action_type), text, event.event_at,
                event.actor_user, reason if reason_open else None,
            ))
        if as_at is not None:
            entries = [entry for entry in entries if as_at.includes(entry["when"])]
        entries.sort(key=lambda e: e["when"], reverse=True)

        page = self.paginate_queryset(entries)
        if page is not None:
            return self.get_paginated_response(page)
        return success_response(data=entries)

    @staticmethod
    def _kind(action_type):
        if action_type == "STUDENT_BRANCH_CHANGED":
            return "branch"
        if "GUARDIAN" in action_type:
            return "guardian"
        if "CLASS" in action_type or "PROMOTION" in action_type:
            return "class"
        if "DOCUMENT" in action_type:
            return "document"
        if action_type == "UPDATE":
            return "edit"
        return "status"

    def _entry(self, kind, text, when, user, reason):
        """One line of the tab; ``reason=None`` leaves the key out."""
        entry = {"kind": kind, "text": text, "when": when, "actor": self._actor(user)}
        if reason is not None:
            entry["reason"] = reason
        return entry

    @staticmethod
    def _audit_text(event):
        """An audit row's sentence, and the reason it carries if any.

        A status move and an applicant's admission stage move carry one, as
        registered by ``field_access.register_audit_words``, which is the
        same answer the platform audit log reads. The reason is ``None`` for
        any other row, so its entry has no ``reason`` key whoever reads it.
        """
        from vs_audit.protected_words import split_summary

        text, reason, _ = split_summary(event)
        return text, reason

    @staticmethod
    def _status_text(row):
        from ..constants import StudentStatus

        to_label = StudentStatus(row.to_status).label
        if not row.from_status:
            return f"Record created as {to_label}."
        return (
            f"Status moved from {StudentStatus(row.from_status).label} to "
            f"{to_label}."
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
        ).select_related("branch", "admission_stage").prefetch_related(
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
    """GET, PUT, DELETE /v1/students/admission-number-policy/

    The admission-number rule: the school's, or with ``?branch=<id>`` the rule
    that branch's students follow. Reading it needs only ``view`` because the
    enrolment form has to render the hint; setting or removing it needs
    ``update``.

    The body is ``{required, pattern, hint, auto_issue, source, suggestion}``.
    ``source`` is ``branch`` when the branch has a rule of its own, ``school``
    when the school has set one, and ``default`` when nobody has. PUT with a
    branch writes that branch's own rule, all four values; DELETE with a
    branch removes it, so the branch follows the school's again. DELETE with
    no branch is a 400: the school's rule is changed with PUT, never removed.

    The branch must be this school's and one the caller can see, or the answer
    is 404, whatever the reason: a distinct answer for another school's branch
    would confirm it exists.

    Who may write follows the rule's reach. The school's rule binds every
    branch, so a PUT with no branch needs a caller whose reach is the whole
    school, and a branch-bound caller is refused with a 403
    (SHARED_RECORD_READ_ONLY) even though their role carries ``update``. A
    branch's own rule, set or removed, needs only that branch in the caller's
    reach, which the 404 above already guarantees.

    docstring-name: Admission number policy
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_VIEW if self.request.method in ("GET", "HEAD", "OPTIONS")
            else PERM_UPDATE
        )
        return super().get_permissions()

    def _branch(self):
        from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids
        from vs_tenants.references import find_branch_in_tenant

        raw = (self.request.query_params.get("branch") or "").strip()
        if not raw:
            return None
        branch = find_branch_in_tenant(self.tenant, raw)
        if branch is None:
            raise NotFound("No such branch at this school.")
        visible = visible_branch_ids(self.request.user, self.tenant)
        if visible is not WHOLE_TENANT and branch.pk not in (visible or ()):
            raise NotFound("No such branch at this school.")
        return branch

    def _body(self, policy, branch):
        from ..services.policy import suggest_number

        # A suggestion, not a reservation: two registrars enrolling at once can
        # be handed the same number, and the unique constraint is what actually
        # stops the collision. "" means the series cannot be continued honestly;
        # see suggest_number for when that happens.
        return {
            **policy.as_dict(),
            "suggestion": suggest_number(self.tenant, policy=policy, branch=branch),
        }

    def get(self, request):
        from ..services.policy import read_policy

        branch = self._branch()
        return success_response(
            data=self._body(read_policy(self.tenant, branch), branch),
        )

    def put(self, request):
        from ..services.policy import write_policy

        from vs_rbac.scoping import assert_caller_may_configure

        branch = self._branch()
        assert_caller_may_configure(
            request.user, self.tenant, branch,
            message=(
                "Only a school-wide administrator can change the school's "
                "admission number rule. Choose one of your branches to set "
                "its own."
            ),
        )
        writer = AdmissionPolicySerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        policy = write_policy(
            self.tenant, request.user, branch=branch,
            required=data["required"], pattern=data["pattern"], hint=data["hint"],
            auto_issue=data.get("auto_issue"),
        )
        return success_response(
            "Admission number rule saved.", data=self._body(policy, branch),
        )

    def delete(self, request):
        from rest_framework.exceptions import ValidationError

        from ..services.policy import reset_branch_policy

        branch = self._branch()
        if branch is None:
            raise ValidationError({
                "branch": "Name the branch whose own rule to remove. The "
                          "school's rule is changed, never removed.",
            })
        policy = reset_branch_policy(self.tenant, branch, request.user)
        return success_response(
            f"{branch.name} follows the school's admission number rule again.",
            data=self._body(policy, branch),
        )


class EnrolmentRulesView(StudentsViewMixin, APIView):
    """GET, PUT /v1/students/enrolment-rules/

    The school's own enrolment rules: the age range, the documents prompted
    for, the optional fields made required, what a full class does and the
    size a new class is given. Reading needs ``school.students.view``, because
    the enrolment form renders from it; changing needs
    ``school.settings.update``, because these are the school's settings rather
    than a student record, and a caller whose reach is the whole school,
    because the rules bind every branch. A branch-bound caller holding the key
    is refused with a 403 (SHARED_RECORD_READ_ONLY) and nothing is written.

    The school is ``request.tenant`` and there is nothing in the request that
    names another. PUT takes every rule every time, plus an optional
    ``reason`` for the audit trail, and answers with the same body as GET.
    Refusals are 400s keyed on the field, in sentences.

    docstring-name: Enrolment rules
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_VIEW if self.request.method in ("GET", "HEAD", "OPTIONS")
            else PERM_SETTINGS_UPDATE
        )
        return super().get_permissions()

    def get(self, request):
        from ..services.rules import read_rules

        return success_response(data=read_rules(self.tenant).as_dict())

    def put(self, request):
        from vs_rbac.scoping import assert_caller_may_configure

        from ..services.rules import write_rules

        assert_caller_may_configure(
            request.user, self.tenant,
            message="Only a school-wide administrator can change the school's enrolment rules.",
        )
        writer = EnrolmentRulesSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        rules = write_rules(
            self.tenant, request.user, reason=data.pop("reason", ""), **data,
        )
        return success_response("Enrolment rules saved.", data=rules.as_dict())


class AdmissionRulesView(StudentsViewMixin, APIView):
    """GET, PUT /v1/students/admission-rules/

    The school's own applicant rules: the admission stages it names, in its
    order, and the documents an applicant must hold before being confirmed.
    Reading needs ``school.students.view``, because the Applicants board
    renders its columns from it; changing needs ``school.settings.update``,
    because these are the school's settings rather than a student record, and
    a caller whose reach is the whole school, because the stages and documents
    bind every branch. A branch-bound caller holding the key is refused with a
    403 (SHARED_RECORD_READ_ONLY) and nothing is written.

    Each stage carries ``applicants``, the applicants at it that the caller
    can see: narrowed to their branches, and to ``?branch=`` where the school
    has more than one, like every other count in the module.

    PUT takes the whole set every time, plus an optional ``reason`` for the
    audit trail, and answers with the GET body. Refusals are 400s keyed on the
    field, in sentences (``services/admission.py``).

    docstring-name: Admission rules
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_VIEW if self.request.method in ("GET", "HEAD", "OPTIONS")
            else PERM_SETTINGS_UPDATE
        )
        return super().get_permissions()

    def _body(self):
        from ..models import Student
        from ..services.admission import read_admission_rules
        from ..services.scoping import scope_students

        visible = self.narrow_to_branch(
            scope_students(
                Student.objects.filter(tenant=self.tenant), self.request.user,
                self.tenant,
            ),
        )
        return read_admission_rules(self.tenant, students=visible).as_dict()

    def get(self, request):
        return success_response(data=self._body())

    def put(self, request):
        from ..serializers import AdmissionRulesSerializer
        from vs_rbac.scoping import assert_caller_may_configure

        from ..services.admission import write_admission_rules

        assert_caller_may_configure(
            request.user, self.tenant,
            message="Only a school-wide administrator can change the school's admission rules.",
        )
        writer = AdmissionRulesSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        write_admission_rules(
            self.tenant, request.user, stages=data["stages"],
            required_documents_to_confirm=data["required_documents_to_confirm"],
            reason=data.get("reason", ""),
        )
        return success_response("Admission rules saved.", data=self._body())
