"""A member of staff's data request: a copy of their data, or erasing it.

* ``GET /v1/staff/<id>/personal-data/`` downloads everything held about them
  as one JSON file (``core.personal_data``). ``school.personal_data.export``.
* ``POST /v1/staff/<id>/erase/`` erases someone off the roll now instead of 6
  years after they left, exactly as the nightly erasure would
  (``schools.vs_staff.erasure``): their name, service, teaching records and
  payslips stay. ``school.personal_data.erase``, and a ``reason`` saying whose
  request it is.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from core import personal_data
from core.response import success_response
from vs_audit.models import AuditModuleKey

from ..constants import OFF_ROLL_STATUSES, PERM_PERSONAL_DATA_ERASE, PERM_PERSONAL_DATA_EXPORT
from ..erasure import erase_staff
from .base import StaffViewMixin


class StaffPersonalDataView(StaffViewMixin, APIView):
    """GET /v1/staff/<id>/personal-data/

    docstring-name: A staff member's personal data copy
    """

    rbac_permission = PERM_PERSONAL_DATA_EXPORT

    def get(self, request, pk):
        return personal_data.download(
            personal_data.STAFF, self.get_staff(pk), request=request, tenant=self.tenant,
            entity_type="StaffProfile", module_key=AuditModuleKey.STAFF,
        )


class StaffEraseView(StaffViewMixin, APIView):
    """POST /v1/staff/<id>/erase/  body: {"reason": "..."}

    docstring-name: Erase a staff member who has left
    """

    rbac_permission = PERM_PERSONAL_DATA_ERASE

    @transaction.atomic
    def post(self, request, pk):
        staff = self.get_staff_for_write(pk)
        reason = personal_data.erasure_reason(request)
        if staff.erased_at is not None:
            raise ValidationError({"detail": "This person's details have already been erased."})
        if staff.employment_status not in OFF_ROLL_STATUSES:
            raise ValidationError({"detail": (
                "Only someone who has left can be erased. Record their exit first; "
                "their payslips and teaching records stay either way."
            )})
        label = str(staff)
        erase_staff(staff)
        personal_data.record_erasure(
            staff, request=request, tenant=self.tenant, entity_type="StaffProfile",
            module_key=AuditModuleKey.STAFF, label=label, reason=reason,
        )
        return success_response(
            f"{label}'s personal details are erased. Their name, service, teaching "
            f"records and payslips stay.",
        )
