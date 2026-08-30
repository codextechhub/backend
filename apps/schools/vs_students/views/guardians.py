"""Guardians: a student's, and the school's directory of them."""
from __future__ import annotations

from django.db import transaction
from django.db.models import Count
from rest_framework import generics
from rest_framework.exceptions import NotFound
from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_UPDATE, PERM_VIEW
from ..models import Guardian, Student, StudentGuardian
from ..serializers import (
    GuardianDirectorySerializer,
    GuardianLinkSerializer,
    GuardianSerializer,
    GuardianWriteSerializer,
    StudentListSerializer,
)
from ..services import guardians as guardian_service
from ..services.scoping import scope_students
from .base import StudentsViewMixin


def _wards_by_guardian(tenant, user, guardian_ids):
    """Ward names per guardian, narrowed to the caller's branches.

    The narrowing is here rather than on the guardian row because Guardian
    carries no branch. A branch-bound caller sees a guardian with the children
    of their own branches beside them and no others - the one place in this
    module where a row is visible while part of its content is not, and it
    follows directly from a school-level guardian serving branch-level
    children.
    """
    links = StudentGuardian.objects.filter(
        tenant=tenant, guardian_id__in=guardian_ids,
    ).select_related("student")
    visible = set(
        scope_students(Student.objects.filter(tenant=tenant), user, tenant)
        .values_list("pk", flat=True),
    )
    out: dict[int, list] = {}
    for link in links:
        if link.student_id in visible:
            out.setdefault(link.guardian_id, []).append(link.student.full_name)
    return out


class StudentGuardiansView(StudentsViewMixin, APIView):
    """GET, POST /v1/students/<id>/guardians/

    docstring-name: A student's guardians
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_UPDATE if self.request.method == "POST" else PERM_VIEW
        )
        return super().get_permissions()

    def get(self, request, pk):
        student = self.student(pk)
        links = list(
            student.guardian_links.select_related("guardian").order_by(
                "-is_primary", "id",
            ),
        )
        siblings = self._siblings(student, [l.guardian_id for l in links])
        return success_response(data=GuardianLinkSerializer(
            links, many=True,
            context={**self.get_serializer_context(), "siblings": siblings,
                     "class_names": self._class_names(siblings)},
        ).data)

    def _siblings(self, student, guardian_ids):
        rows = StudentGuardian.objects.filter(
            tenant=self.tenant, guardian_id__in=guardian_ids,
        ).exclude(student=student).select_related("student")
        visible = set(
            scope_students(
                Student.objects.filter(tenant=self.tenant),
                self.request.user, self.tenant,
            ).values_list("pk", flat=True),
        )
        out: dict[int, list] = {}
        for row in rows:
            if row.student_id in visible:
                out.setdefault(row.guardian_id, []).append(row.student)
        return out

    def _class_names(self, siblings):
        from ..models import ClassEnrolment

        ids = [s.pk for rows in siblings.values() for s in rows]
        if not ids:
            return {}
        return {
            e.student_id: e.school_class.name
            for e in ClassEnrolment.objects.filter(
                student_id__in=ids, is_active=True,
            ).select_related("school_class")
        }

    @transaction.atomic
    def post(self, request, pk):
        student = self.student(pk)
        writer = GuardianWriteSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data

        gid = data.get("guardian_id")
        if gid:
            guardian = self.guardian(gid)
        else:
            guardian, _ = guardian_service.upsert_guardian(
                self.tenant,
                full_name=data.get("full_name", ""), phone=data.get("phone", ""),
                email=data.get("email", ""),
                occupation=data.get("occupation", ""),
                address=data.get("address", ""),
            )
        guardian_service.link(
            student, guardian, relationship=data["relationship"],
            is_primary=data.get("is_primary", False), actor=request.user,
        )
        return success_response(
            f"{guardian.full_name} linked to {student.full_name}.",
            data=GuardianSerializer(guardian).data, status=201,
        )


class StudentGuardianDetailView(StudentsViewMixin, APIView):
    """PATCH, DELETE /v1/students/<id>/guardians/<guardian_id>/

    The unlink lives under the student on purpose: a guardian is linked to
    several children, and "delete this guardian" would be ambiguous about which
    of them the school meant.

    docstring-name: One of a student's guardians
    """

    def get_permissions(self):
        self.rbac_permission = PERM_UPDATE
        return super().get_permissions()

    @transaction.atomic
    def patch(self, request, pk, guardian_id):
        student = self.student(pk)
        guardian = self.guardian(guardian_id)
        link = StudentGuardian.objects.filter(
            student=student, guardian=guardian,
        ).first()
        if link is None:
            raise NotFound("That guardian is not linked to this student.")

        if "relationship" in request.data:
            link.relationship = request.data["relationship"]
            link.save(update_fields=["relationship", "updated_at"])
        if request.data.get("is_primary"):
            guardian_service.set_primary(student, guardian, actor=request.user)
        return success_response(f"{guardian.full_name} updated.")

    @transaction.atomic
    def delete(self, request, pk, guardian_id):
        student = self.student(pk)
        guardian = self.guardian(guardian_id)
        promote_id = request.query_params.get("promote") or request.data.get("promote")
        promote = self.guardian(promote_id) if promote_id else None
        guardian_service.unlink(
            student, guardian, actor=request.user, promote=promote,
        )
        return success_response(f"{guardian.full_name} unlinked.")


class GuardianDirectoryView(StudentsViewMixin, generics.ListAPIView):
    """GET /v1/guardians/

    docstring-name: Guardians
    """

    serializer_class = GuardianDirectorySerializer

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get_queryset(self):
        params = self.request.query_params
        return guardian_service.guardian_directory(
            self.tenant, self.request.user,
            search=params.get("search", ""),
            include_unlinked=(params.get("unlinked") or "").lower() == "true",
            branch=self.branch_filter,
        ).annotate(ward_count=Count("student_links", distinct=True))

    def get_serializer_context(self):
        context = super().get_serializer_context()
        page = getattr(self, "_page_rows", None)
        context["wards"] = _wards_by_guardian(
            self.tenant, self.request.user,
            [g.pk for g in page] if page else [],
        )
        return context

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        rows = page if page is not None else list(queryset)
        # Ward names are fetched for the page rather than the whole queryset:
        # a school with 800 guardians would otherwise pull every link to draw
        # twelve rows.
        self._page_rows = rows
        serializer = self.get_serializer(rows, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return success_response(data=serializer.data)


class GuardianDetailView(StudentsViewMixin, APIView):
    """GET /v1/guardians/<id>/

    docstring-name: One guardian
    """

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get(self, request, pk):
        guardian = self.guardian(pk)
        wards = guardian_service.wards_queryset(
            guardian, request.user, self.tenant,
        ).select_related("branch")
        links = {
            l.student_id: l
            for l in StudentGuardian.objects.filter(guardian=guardian)
        }
        from ..models import ClassEnrolment

        classes = {
            e.student_id: e.school_class.name
            for e in ClassEnrolment.objects.filter(
                student__in=wards, is_active=True,
            ).select_related("school_class")
        }
        return success_response(data={
            **GuardianSerializer(guardian).data,
            "wards": [
                {
                    "id": s.pk, "name": s.full_name,
                    "student_number": s.student_number,
                    "status": s.status, "status_label": s.get_status_display(),
                    "class_name": classes.get(s.pk, ""),
                    "relationship": links[s.pk].relationship if s.pk in links else "",
                    "is_primary": links[s.pk].is_primary if s.pk in links else False,
                }
                for s in wards
            ],
        })


class GuardianStudentsView(StudentsViewMixin, generics.ListAPIView):
    """GET /v1/guardians/<id>/students/

    The siblings. Narrowed to the caller's branches like every other read.

    docstring-name: A guardian's students
    """

    serializer_class = StudentListSerializer

    def get_permissions(self):
        self.rbac_permission = PERM_VIEW
        return super().get_permissions()

    def get_queryset(self):
        guardian = self.guardian(self.kwargs["pk"])
        return guardian_service.wards_queryset(
            guardian, self.request.user, self.tenant,
        ).select_related("branch").prefetch_related(
            "enrolments__school_class", "guardian_links__guardian",
        )
