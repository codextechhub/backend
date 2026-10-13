"""From a class's sheets to a published term: the broadsheet, compiling, approval and publishing.

All routes sit under ``/v1/gradebook/classes/`` and take the term as
``?term=<id>`` (the school's current term when it is absent). The key says
whether the caller may use the screen at all; which class is a rule of the class
(:mod:`..services.classbook`). A class of another school, or one in a branch the
caller cannot see, answers 404, and so does a term of another school.

* The class teacher compiles and submits their class: ``class.submit`` and being
  the class teacher.
* The approver (``class.publish``) reads the queue, returns a class, writes the
  principal's comments and publishes.
"""
from __future__ import annotations

from collections.abc import Mapping

from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.field_enforcement import assert_writable

from ..constants import (
    PERM_CLASS_PUBLISH,
    PERM_CLASS_SUBMIT,
    PERM_HUB_VIEW,
    PERM_SHEET_VIEW,
    RESULT_FIELD_RESOURCE,
    SHEET_FIELD_RESOURCE,
)
from ..serializers import (
    ApprovalsSerializer,
    BroadsheetSerializer,
    ClassReturnBodySerializer,
    CompileSerializer,
    CompileWriteSerializer,
    PrincipalCommentsSerializer,
    PrincipalCommentsWriteSerializer,
    PublishBodySerializer,
    PublishPreviewSerializer,
    PublishResultSerializer,
)
from ..services import approvals, classbook, compiling, publishing
from .base import GradebookViewMixin


def _sent(request, names) -> list:
    """The names in *names* the request body carries, however empty their values."""
    body = request.data
    return [name for name in names if isinstance(body, Mapping) and name in body]


class _ClassView(GradebookViewMixin, APIView):
    def open(self, class_id, *, rule):
        return classbook.open_class(
            self.tenant, self.request.user, class_id, self.term_from_query(), rule=rule,
        )

    def broadsheet(self, ctx) -> dict:
        """The class's broadsheet as the caller may read it."""
        bk = classbook.load_book(ctx.tenant, ctx.school_class, ctx.branch, ctx.term)
        body = classbook.broadsheet_payload(bk, self.request.user)
        return dict(BroadsheetSerializer(body, context={"request": self.request}).data)


class BroadsheetView(_ClassView):
    """GET /v1/gradebook/classes/<class_id>/broadsheet/?term=

    Every student on the class roll against every subject the class owes: a
    total, ``NOT_TAKEN`` for an optional subject the student does not take, or
    ``NOT_IN`` while its sheet is not in. Each row carries the overall average
    (two places), the position and the roll size where the school ranks (the
    keys are absent where it does not) and whether the class teacher's comment is
    written. ``gaps`` names each subject not in with its teacher,
    ``attendance_warning`` says how many school days had no register, and
    ``can_submit`` and ``submit_blockers`` say whether the class teacher can send
    the class to the principal and what stops them. The class teacher reads their
    own class; a holder of ``gradebook.hub.view`` or ``gradebook.class.publish``
    reads every class in their branches.

    docstring-name: Class broadsheet
    """

    rbac_permission = [PERM_SHEET_VIEW, PERM_HUB_VIEW, PERM_CLASS_PUBLISH]

    def get(self, request, class_id):
        ctx = self.open(class_id, rule=classbook.READ_BOOK)
        return success_response(data=self.broadsheet(ctx))


class CompileView(_ClassView):
    """GET, PUT /v1/gradebook/classes/<class_id>/students/<student_id>/compile/?term=

    ``GET`` is a student's compile drawer: their subjects with total and grade
    (``NOT_IN`` while a sheet is not in), overall average, the term's attendance
    (opened, present, absent), the ratings and the school's traits where it
    records them (otherwise both keys are absent), the class teacher's comment,
    and the neighbours on the roll (``index``, ``of``, ``previous``, ``next``).
    The class teacher and a holder of ``gradebook.class.publish`` may read it.

    ``PUT`` saves ``{"teacher_comment": text, "ratings": {trait: 1..5}}`` and is
    the class teacher's alone (``NOT_CLASS_TEACHER``). The comment is required
    (``COMMENT_REQUIRED``); ratings, when sent, replace the student's ratings and
    must be the school's traits with whole values from 1 to 5
    (``INVALID_RATING``). Refused once the class is awaiting approval or
    published (``CLASS_LOCKED``). A role that may not write a field it sends is
    refused the request. Answers with the drawer.

    docstring-name: Compile a student's report
    """

    rbac_permission = [PERM_CLASS_SUBMIT, PERM_CLASS_PUBLISH]

    def get_permissions(self):
        self.rbac_permission = (
            PERM_CLASS_SUBMIT if self.request.method == "PUT"
            else [PERM_CLASS_SUBMIT, PERM_CLASS_PUBLISH]
        )
        return super().get_permissions()

    def drawer(self, ctx, student_id) -> dict:
        bk = classbook.load_book(ctx.tenant, ctx.school_class, ctx.branch, ctx.term)
        body = classbook.compile_payload(bk, student_id, self.request.user)
        return dict(CompileSerializer(body, context={"request": self.request}).data)

    def get(self, request, class_id, student_id):
        ctx = self.open(class_id, rule=classbook.READ_COMPILE)
        return success_response(data=self.drawer(ctx, student_id))

    def put(self, request, class_id, student_id):
        ctx = self.open(class_id, rule=classbook.COMPILE)
        assert_writable(
            request, RESULT_FIELD_RESOURCE, _sent(request, ("teacher_comment", "ratings")),
        )
        body = CompileWriteSerializer(data=request.data, context={"request": request})
        body.is_valid(raise_exception=True)
        data = body.validated_data
        compiling.save_compile(
            ctx, student_id, data.get("teacher_comment"),
            data["ratings"] if "ratings" in data else None,
        )
        return success_response("Saved.", self.drawer(ctx, student_id))


class ClassSubmitView(_ClassView):
    """POST /v1/gradebook/classes/<class_id>/submit/?term=

    The class teacher sends the class to the principal. Refused with
    ``SUBJECTS_MISSING`` (``error.detail.subjects`` lists the subjects not in)
    while any sheet is not in, and ``COMMENTS_MISSING`` (``error.detail.count``)
    while any student has no comment. The class then awaits approval, and a note
    it was last returned with is cleared. Answers with the broadsheet.

    docstring-name: Submit a class
    """

    rbac_permission = PERM_CLASS_SUBMIT

    def post(self, request, class_id):
        ctx = self.open(class_id, rule=classbook.COMPILE)
        compiling.submit_class(ctx)
        return success_response("Class submitted.", self.broadsheet(ctx))


class ApprovalsView(GradebookViewMixin, APIView):
    """GET /v1/gradebook/approvals/?term=&branch=

    The classes that are awaiting the principal or published, in the caller's
    branches: each with its class teacher, how many of its subjects are in, its
    stage, the students whose results are held and who held them (never why), and
    whether the caller can decide on it now (``can_decide``, while awaiting).
    ``branch`` is null at a school with one branch.

    docstring-name: Approvals queue
    """

    rbac_permission = PERM_CLASS_PUBLISH

    def get(self, request):
        body = approvals.queue(
            self.tenant, request.user, self.term_from_query(), self.branch_from_query(),
        )
        return success_response(
            data=dict(ApprovalsSerializer(body, context={"request": request}).data),
        )


class ClassReturnView(_ClassView):
    """POST /v1/gradebook/classes/<class_id>/return/?term=

    Body ``{"note": "...", "subject": id?}``. The note is required
    (``NOTE_REQUIRED``). Without ``subject`` the class goes back to its class
    teacher, who is told in the app; with it, that one sheet goes back to its
    main teacher as returned, who is told in the app, and the class's stage is
    cleared. Refused unless the class is awaiting approval (``NOT_AWAITING``).
    Answers ``{"returned": "CLASS_TEACHER" | "SUBJECT", "stage": ...}``.

    docstring-name: Return a class
    """

    rbac_permission = PERM_CLASS_PUBLISH

    def post(self, request, class_id):
        ctx = self.open(class_id, rule=classbook.APPROVER)
        assert_writable(request, SHEET_FIELD_RESOURCE, _sent(request, ("note",)))
        body = ClassReturnBodySerializer(data=request.data, context={"request": request})
        body.is_valid(raise_exception=True)
        subject = body.validated_data.get("subject")
        approvals.return_class(ctx, body.validated_data.get("note", ""), subject)
        if subject is None:
            return success_response(
                "Class returned to its class teacher.",
                {"returned": "CLASS_TEACHER", "stage": "COMPILING"},
            )
        return success_response(
            "Sheet returned to its teacher.", {"returned": "SUBJECT", "stage": "ENTERING"},
        )


class PrincipalCommentsView(_ClassView):
    """GET, PUT /v1/gradebook/classes/<class_id>/principal-comments/?term=

    ``GET`` lists each band of the school's remark templates with the template's
    sentence and, in ``override`` (``text``, ``written_by``, ``written_at``), the
    comment written for this class, or null. ``PUT`` takes
    ``{"comments": [{"band_min": 70, "text": "..."}]}``: each text is required
    (``COMMENT_REQUIRED``) and each band must be a template band
    (``UNKNOWN_BAND``); bands not named keep what they have. Refused once the
    class is published (``CLASS_LOCKED``). Answers with the list.

    docstring-name: Principal's comments
    """

    rbac_permission = PERM_CLASS_PUBLISH

    def render(self, ctx) -> dict:
        return dict(PrincipalCommentsSerializer(
            approvals.principal_payload(ctx), context={"request": self.request},
        ).data)

    def get(self, request, class_id):
        ctx = self.open(class_id, rule=classbook.APPROVER)
        return success_response(data=self.render(ctx))

    def put(self, request, class_id):
        ctx = self.open(class_id, rule=classbook.APPROVER)
        assert_writable(request, RESULT_FIELD_RESOURCE, _sent(request, ("comments",)))
        body = PrincipalCommentsWriteSerializer(data=request.data, context={"request": request})
        body.is_valid(raise_exception=True)
        approvals.save_principal_comments(ctx, body.validated_data["comments"])
        return success_response("Comments saved.", self.render(ctx))


class PublishPreviewView(_ClassView):
    """GET /v1/gradebook/classes/<class_id>/publish/preview/?term=

    What publishing the class now would do: ``released`` (results that will be
    released), ``emailed`` (of those, how many primary guardians have an email),
    ``no_email`` (the rest), and ``held`` (each held student, who held them and
    when). Refused unless the class is awaiting approval (``NOT_AWAITING``).

    docstring-name: Publish preview
    """

    rbac_permission = PERM_CLASS_PUBLISH

    def get(self, request, class_id):
        ctx = self.open(class_id, rule=classbook.APPROVER)
        body = publishing.preview(ctx)
        return success_response(
            data=dict(PublishPreviewSerializer(body, context={"request": request}).data),
        )


class PublishView(GradebookViewMixin, APIView):
    """POST /v1/gradebook/classes/publish/?term=

    Body ``{"classes": [id, ...]}``. Each class is published in a transaction of
    its own and answers ``{class, published, released, emailed, no_email, held}``,
    with ``code`` and ``message`` when it was refused: a class that is not
    awaiting approval is ``NOT_AWAITING`` and the others still go ahead. Every
    result not held is released and its primary guardian is emailed that the
    report card is ready to collect; a held result is not. A class of another
    school, or outside the caller's branches, answers 404 for the whole request
    and nothing is published.

    docstring-name: Publish classes
    """

    rbac_permission = PERM_CLASS_PUBLISH

    def post(self, request):
        term = self.term_from_query()
        body = PublishBodySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        seen, contexts = set(), []
        for class_id in body.validated_data["classes"]:
            if class_id in seen:
                continue
            seen.add(class_id)
            contexts.append(classbook.open_class(
                self.tenant, request.user, class_id, term, rule=classbook.APPROVER,
            ))
        if not contexts:
            raise NotFound("No such class at this school.")
        results = publishing.publish_classes(contexts)
        done = sum(1 for row in results if row["published"])
        return success_response(
            f"{done} of {len(results)} classes published.",
            PublishResultSerializer(results, many=True, context={"request": request}).data,
        )
