"""Staff profile section history, scoped by the section being opened."""
from __future__ import annotations

from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from core.response import success_response
from vs_history.as_at import parse_as_at

from ..section_history import SECTION_GROUPS, section_changes
from ..serializers import StaffDetailSerializer
from ..services.visibility import GROUP_CONTACT, GROUP_PERMISSIONS
from .base import StaffViewMixin


class StaffSectionHistoryView(StaffViewMixin, APIView):
    """A dated, safe before-and-after timeline for one visible profile section.

    The section's ordinary read admission governs its history. A person may
    read their own visible sections; a manager or colleague may read only
    sections the school's profile policy grants them. The platform audit API
    remains closed, and no stored snapshot or free-text note is serialized.
    """

    profile_group = GROUP_CONTACT
    pending_tenant_surface = True

    def get(self, request, pk):
        section = request.query_params.get("section", "")
        if section not in SECTION_GROUPS:
            raise ValidationError({"section": "Choose a staff profile section."})
        self.profile_group = SECTION_GROUPS[section]
        self.rbac_permission = GROUP_PERMISSIONS[self.profile_group]
        staff, access, admission = self.admit_profile_read(pk)
        as_at = parse_as_at(request)
        self.refuse_as_at_unless_full(as_at, admission)
        visible_fields = None
        if section == "overview":
            visible_fields = set(StaffDetailSerializer(
                staff, context={**self.serializer_context(), "profile_access": access},
            ).data)
        return success_response(data=section_changes(
            tenant=self.tenant, staff=staff, section=section,
            page=request.query_params.get("page", "1"),
            visible_fields=visible_fields, before=as_at,
        ))
