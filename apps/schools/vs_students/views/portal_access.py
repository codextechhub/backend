"""Giving pupils their own sign-in account for the pupil portal.

Reading where a pupil's access stands needs only the student view key, since it
is part of the record. Giving, resending and taking away access need
``school.portal_access.issue``, because each creates or closes a login. Every
route reaches the pupil through the caller's branch scope, so another branch's
or another school's pupil answers 404.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import serializers
from rest_framework.views import APIView

from core.response import success_response
from vs_rbac.field_enforcement import can_read

from ..constants import BULK_MAX, PERM_PORTAL_ACCESS, PERM_VIEW
from ..exceptions import BulkTooLarge
from ..services import portal_access
from .base import StudentsViewMixin
from .movements import _BulkAction

def access_block(request, student) -> dict:
    """The access block, as far as the caller's Field Access reaches.

    The block is one switch (``school.students.portal_access``): a caller who
    may not read it gets an empty block. The sign-in identifiers inside it are
    the pupil's student number and email under another name, so each follows
    its own switch and cannot reappear here when hidden on the record.
    """
    if not can_read(request, "school.students.portal_access"):
        return {}
    block = portal_access.describe(student)
    ids = block["sign_in_identifiers"]
    if not can_read(request, "school.students.student_number"):
        ids.pop("admission_number")
    if not can_read(request, "school.students.email"):
        ids.pop("email")
    return block


class PortalAccessView(StudentsViewMixin, APIView):
    """GET, POST, DELETE /v1/students/<id>/portal-access/

    GET says whether the pupil has an account and how they sign in. POST gives
    them one and sends the invitation to their email and their primary
    guardian's. DELETE closes it and ends every session it holds.

    docstring-name: A pupil's portal access
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_VIEW if self.request.method == "GET" else PERM_PORTAL_ACCESS
        )
        return super().get_permissions()

    def get(self, request, pk):
        return success_response(data=access_block(request, self.student(pk)))

    def post(self, request, pk):
        student = portal_access.issue(self.student(pk), actor=request.user)
        return success_response(
            f"{student.first_name} has been given portal access. The invitation is on its way.",
            data=access_block(request, student), status=201,
        )

    def delete(self, request, pk):
        student = portal_access.revoke(self.student(pk), actor=request.user)
        return success_response(
            f"{student.first_name}'s portal access has been taken away.",
            data=access_block(request, student),
        )


class PortalAccessReissueView(StudentsViewMixin, APIView):
    """POST /v1/students/<id>/portal-access/reissue/

    A fresh invitation link for a pupil who has not activated yet. The old
    link stops working.

    docstring-name: Resend a pupil's portal invitation
    """

    def get_permissions(self):
        self.rbac_permission = PERM_PORTAL_ACCESS
        return super().get_permissions()

    def post(self, request, pk):
        student = portal_access.reissue(self.student(pk), actor=request.user)
        return success_response(
            f"{student.first_name}'s invitation has been sent again.",
            data=access_block(request, student),
        )


class BulkPortalAccessSerializer(serializers.Serializer):
    """Either named pupils or a whole class's current roll, not both."""

    student_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=False,
    )
    school_class = serializers.IntegerField(required=False)

    def validate(self, attrs):
        if bool(attrs.get("student_ids")) == bool(attrs.get("school_class")):
            raise serializers.ValidationError(
                {"student_ids": "Choose some pupils or one class."},
            )
        return attrs


class BulkPortalAccessView(_BulkAction):
    """POST /v1/students/portal-access/

    Gives portal access to several pupils at once: named ones, or every pupil
    in a class this year. One pupil's refusal never stops the others, and a
    pupil who already has access is reported as such rather than invited again.

    docstring-name: Give several pupils portal access
    """

    def get_permissions(self):
        self.rbac_permission = PERM_PORTAL_ACCESS
        return super().get_permissions()

    def post(self, request):
        from ..services.placement import resolve_class, roster

        writer = BulkPortalAccessSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        if data.get("school_class"):
            school_class = resolve_class(self.tenant, request.user, data["school_class"])
            students = list(roster(self.tenant, request.user, school_class, self.active_session))
            if len(students) > BULK_MAX:
                raise BulkTooLarge(
                    f"That class has {len(students)} pupils. Give access to "
                    f"{BULK_MAX} or fewer at a time.",
                    limit=BULK_MAX, given=len(students),
                )
            pairs = [(s.pk, s) for s in students]
        else:
            pairs = self._students(data["student_ids"])

        results = []
        for sid, student in pairs:
            if student is None:
                results.append(self._row(sid, None, False, "NOT_FOUND",
                                         "No such student at this school."))
                continue
            outcome = portal_access.issue_for_students([student], actor=request.user)[0]
            if outcome.outcome == "refused":
                results.append(self._row(sid, student, False, "REFUSED", outcome.message))
            else:
                row = self._row(sid, student, True)
                row["already"] = outcome.outcome == "already"
                results.append(row)

        issued = sum(1 for r in results if r["ok"] and not r.get("already"))
        return success_response(
            f"{issued} of {len(results)} pupils given portal access.",
            data={"results": results, "issued": issued},
        )
