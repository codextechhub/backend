"""A pupil's or a guardian's data request: a copy of their data, or erasing it.

The school answers the request; these are its two tools (``core.personal_data``).

* ``GET /v1/students/<id>/personal-data/`` and
  ``GET /v1/guardians/<id>/personal-data/`` download everything held about the
  person as one JSON file. ``school.personal_data.export``.
* ``POST /v1/students/<id>/erase/`` and ``POST /v1/guardians/<id>/erase/`` erase
  them now instead of 6 years after they left, exactly as the nightly erasure
  would (``schools.vs_students.erasure``). ``school.personal_data.erase``, and a
  ``reason`` saying whose request it is. A pupil must have left (withdrawn,
  graduated, transferred or rejected); a guardian, have no child still at the
  school. A parent whose last child at the school is erased is erased by the
  nightly run the same night, which the response says.

Both are recorded in the audit trail with the person's name as it was.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from core import personal_data
from core.response import success_response
from vs_audit.models import AuditModuleKey

from ..constants import PERM_PERSONAL_DATA_ERASE, PERM_PERSONAL_DATA_EXPORT
from ..erasure import LEFT, erase_parent, erase_pupil
from ..models import Guardian, StudentGuardian
from .base import StudentsViewMixin


class StudentPersonalDataView(StudentsViewMixin, APIView):
    """GET /v1/students/<id>/personal-data/

    docstring-name: A pupil's personal data copy
    """

    rbac_permission = PERM_PERSONAL_DATA_EXPORT

    def get(self, request, pk):
        return personal_data.download(
            personal_data.PUPIL, self.student(pk), request=request, tenant=self.tenant,
            entity_type="Student", module_key=AuditModuleKey.STUDENT,
        )


class GuardianPersonalDataView(StudentsViewMixin, APIView):
    """GET /v1/guardians/<id>/personal-data/

    docstring-name: A guardian's personal data copy
    """

    rbac_permission = PERM_PERSONAL_DATA_EXPORT

    def get(self, request, pk):
        return personal_data.download(
            personal_data.GUARDIAN, self.guardian(pk), request=request, tenant=self.tenant,
            entity_type="Guardian", module_key=AuditModuleKey.STUDENT,
        )


class StudentEraseView(StudentsViewMixin, APIView):
    """POST /v1/students/<id>/erase/  body: {"reason": "..."}

    docstring-name: Erase a pupil who has left
    """

    rbac_permission = PERM_PERSONAL_DATA_ERASE

    @transaction.atomic
    def post(self, request, pk):
        pupil = self.student(pk)
        reason = personal_data.erasure_reason(request)
        if pupil.erased_at is not None:
            raise ValidationError({"detail": "This pupil's details have already been erased."})
        if pupil.status not in LEFT:
            raise ValidationError({"detail": (
                "Only a pupil who has left can be erased. Withdraw them first; "
                "their results, attendance and fee records stay either way."
            )})
        label = str(pupil)
        last_child_parents = [
            link.guardian for link in StudentGuardian._base_manager.filter(student=pupil)
            .select_related("guardian")
            if not StudentGuardian._base_manager.filter(
                guardian=link.guardian, student__erased_at__isnull=True,
            ).exclude(student=pupil).exists()
        ]
        erase_pupil(pupil)
        personal_data.record_erasure(
            pupil, request=request, tenant=self.tenant, entity_type="Student",
            module_key=AuditModuleKey.STUDENT, label=label, reason=reason,
        )
        message = f"{label}'s personal details are erased. Their name, results, attendance and fee records stay."
        if last_child_parents:
            message += (
                " Their parents with no other child at the school are erased tonight: "
                + ", ".join(str(g) for g in last_child_parents) + "."
            )
        return success_response(message)


class GuardianEraseView(StudentsViewMixin, APIView):
    """POST /v1/guardians/<id>/erase/  body: {"reason": "..."}

    docstring-name: Erase a guardian with no child at the school
    """

    rbac_permission = PERM_PERSONAL_DATA_ERASE

    @transaction.atomic
    def post(self, request, pk):
        guardian = self.guardian(pk)
        reason = personal_data.erasure_reason(request)
        if guardian.erased_at is not None:
            raise ValidationError({"detail": "This guardian's details have already been erased."})
        current = StudentGuardian._base_manager.filter(
            guardian=guardian, student__erased_at__isnull=True,
        ).exclude(student__status__in=LEFT)
        if current.exists():
            raise ValidationError({"detail": (
                "This guardian still has a child at the school. A guardian can be "
                "erased once none of their children is still here."
            )})
        label = str(guardian)
        erase_parent(Guardian._base_manager.get(pk=guardian.pk))
        personal_data.record_erasure(
            guardian, request=request, tenant=self.tenant, entity_type="Guardian",
            module_key=AuditModuleKey.STUDENT, label=label, reason=reason,
        )
        return success_response(f"{label}'s personal details are erased.")
