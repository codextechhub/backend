"""Qualifications and documents held against a person.

Two rules run through both. **A person always reads their own**, whatever they
hold, and **nobody writes their own qualifications**: a qualification somebody
can type about themselves is a claim rather than a record, and the whole point
of the table is that it holds what the school was given.

Nothing here carries a verified state, and no endpoint may add one. Nothing in
the platform checks a qualification or a document, there is no register to check
one against, and a field somebody sets by hand is read by everybody else as a
check that was made.

FRD M12 v2.1, FR-006 and FR-007.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_RECORDS_UPDATE, PERM_RECORDS_VIEW
from ..models import StaffDocument, StaffQualification
from ..serializers import (
    DocumentCreateSerializer,
    DocumentSerializer,
    QualificationSerializer,
)
from ..services.scoping import is_self
from .base import StaffViewMixin


class _StaffChildView(StaffViewMixin, APIView):
    """A collection hanging off one person, scoped through them.

    The parent is resolved first and scoped, so a child id belonging to another
    school's person is unreachable before its own row is ever looked at.
    """

    pending_tenant_surface = True

    def get_permissions(self):
        """Reading your own needs nothing; writing about yourself is refused.

        The asymmetry is the point. A person may read the CV the school holds on
        them, and may not add a degree to their own record.
        """
        from vs_rbac.permissions import IsAuthenticatedAndActive

        if self.request.method in ("GET", "HEAD", "OPTIONS") and self._is_own():
            return [IsAuthenticatedAndActive()]
        return super().get_permissions()

    def _is_own(self) -> bool:
        from ..models import StaffProfile

        pk = self.kwargs.get("pk")
        user = getattr(self.request, "user", None)
        tenant = getattr(self.request, "tenant", None)
        if pk is None or not getattr(user, "pk", None) or tenant is None:
            return False
        return StaffProfile.objects.filter(
            tenant=tenant, pk=pk, user_id=user.pk,
        ).exists()

    @property
    def rbac_permission(self):
        method = (getattr(self.request, "method", "") or "").upper()
        return (
            PERM_RECORDS_VIEW if method in ("GET", "HEAD", "OPTIONS")
            else PERM_RECORDS_UPDATE
        )


class QualificationListCreateView(_StaffChildView):
    """GET/POST /v1/i/me/staff/<id>/qualifications/

    docstring-name: A staff member's qualifications
    """

    def get(self, request, pk):
        staff = self.get_staff(pk)
        return success_response(
            data=QualificationSerializer(
                staff.qualifications.all(), many=True,
            ).data,
        )

    def post(self, request, pk):
        staff = self.get_staff(pk)
        payload = QualificationSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        row = StaffQualification.objects.create(
            tenant=self.tenant, staff=staff, created_by=request.user,
            **payload.validated_data,
        )
        return success_response(
            message="Qualification added.",
            data=QualificationSerializer(row).data,
            status=status.HTTP_201_CREATED,
        )


class QualificationDetailView(StaffViewMixin, APIView):
    """PATCH/DELETE /v1/i/me/staff/qualifications/<id>/

    docstring-name: One qualification
    """

    rbac_permission = PERM_RECORDS_UPDATE
    pending_tenant_surface = True

    def _row(self, pk):
        row = (
            StaffQualification.objects.filter(tenant=self.tenant, pk=pk)
            .select_related("staff__user", "staff__branch")
            .first()
        )
        if row is None:
            raise NotFound("No such qualification at this school.")
        # Scoped through the person it belongs to, so a branch admin cannot
        # reach a qualification on somebody they cannot open.
        self.get_staff(row.staff_id)
        return row

    def patch(self, request, pk):
        row = self._row(pk)
        payload = QualificationSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)
        for field, value in payload.validated_data.items():
            setattr(row, field, value)
        row.save()
        return success_response(
            message="Qualification updated.", data=QualificationSerializer(row).data,
        )

    def delete(self, request, pk):
        self._row(pk).delete()
        return success_response(message="Qualification removed.")


class DocumentListCreateView(_StaffChildView):
    """GET/POST /v1/i/me/staff/<id>/documents/

    The file goes through the database-backed storage and is served by the
    authenticated media view, so the payload carries a media path and never a
    signed or guessable direct link.

    docstring-name: A staff member's documents
    """

    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, pk):
        staff = self.get_staff(pk)
        return success_response(
            data=DocumentSerializer(
                staff.documents.select_related("uploaded_by"), many=True,
                context={"request": request},
            ).data,
        )

    def post(self, request, pk):
        staff = self.get_staff(pk)
        payload = DocumentCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        row = StaffDocument.objects.create(
            tenant=self.tenant, staff=staff, uploaded_by=request.user,
            **payload.validated_data,
        )
        return success_response(
            message="Document uploaded.",
            data=DocumentSerializer(row, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class DocumentDetailView(StaffViewMixin, APIView):
    """DELETE /v1/i/me/staff/documents/<id>/

    The row and the stored bytes go together. Deleting the row alone would leave
    a passport scan sitting in storage behind a URL somebody may still be
    holding, which is the opposite of what a school pressing Remove means.

    docstring-name: One staff document
    """

    rbac_permission = PERM_RECORDS_UPDATE
    pending_tenant_surface = True

    @transaction.atomic
    def delete(self, request, pk):
        row = (
            StaffDocument.objects.filter(tenant=self.tenant, pk=pk)
            .select_related("staff__user", "staff__branch")
            .first()
        )
        if row is None:
            raise NotFound("No such document at this school.")
        self.get_staff(row.staff_id)
        # The file is removed by the post_delete receiver, so the two cannot
        # drift apart depending on which path did the delete.
        row.delete()
        return success_response(message="Document removed.")
