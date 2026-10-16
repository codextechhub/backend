"""The school's rules for its student portal."""
from __future__ import annotations

from django.db import transaction
from rest_framework import serializers
from rest_framework.views import APIView

from core.response import success_response

from ..constants import (
    CFG_PORTAL_PHOTO_SELF_EDIT,
    CFG_PORTAL_SESSION_DAYS,
    PERM_SETTINGS_UPDATE,
    PERM_VIEW,
)
from ..exceptions import StudentSettingNotRegistered
from ..services.rules import resolve_many
from .base import StudentsViewMixin


def read_portal_rules(tenant) -> dict:
    """The portal rules as the screen shows them, with each default applied."""
    from ..sign_in import portal_session_days

    found = resolve_many((CFG_PORTAL_PHOTO_SELF_EDIT,), tenant=tenant)
    value, _ = found.get(CFG_PORTAL_PHOTO_SELF_EDIT, (True, None))
    return {
        "photo_self_edit": value is not False,
        "session_days": portal_session_days(tenant),
    }


class PortalRulesSerializer(serializers.Serializer):
    photo_self_edit = serializers.BooleanField(
        required=False,
        error_messages={
            "invalid": "Say whether students may change their own photograph: true or false.",
        },
    )
    session_days = serializers.IntegerField(
        required=False, min_value=1, max_value=90,
        error_messages={
            "invalid": "Enter a whole number of days, from 1 to 90.",
            "min_value": "Enter a whole number of days, from 1 to 90.",
            "max_value": "Enter a whole number of days, from 1 to 90.",
        },
    )
    reason = serializers.CharField(required=False, allow_blank=True, max_length=200)


class PortalRulesView(StudentsViewMixin, APIView):
    """GET, PUT /v1/students/portal-rules/

    What students may do for themselves in the student portal. Reading needs
    ``school.students.view``; changing needs ``school.settings.update`` and a
    whole-school reach, because the rules bind every branch, as the promotion
    rules do.

    ``photo_self_edit`` (default true) lets a student change or remove their
    own photograph. ``session_days`` (1 to 90, default 30) is how long a
    student stays signed in to the student app between uses; a change applies
    from each student's next refresh. A save may carry either or both.

    docstring-name: Student portal rules
    """

    def get_permissions(self):
        self.rbac_permission = (
            PERM_VIEW if self.request.method in ("GET", "HEAD", "OPTIONS")
            else PERM_SETTINGS_UPDATE
        )
        return super().get_permissions()

    def get(self, request):
        return success_response(data=read_portal_rules(self.tenant))

    @transaction.atomic
    def put(self, request):
        from vs_config.models import ConfigurationDefinition
        from vs_config.services.resolution import resolve_value, set_value
        from vs_rbac.scoping import assert_caller_may_configure

        assert_caller_may_configure(
            request.user, self.tenant,
            message="Only a school-wide administrator can change the student portal rules.",
        )
        writer = PortalRulesSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        reason = (data.get("reason") or "").strip() or "Student portal rules set from Student settings."
        for field, key in (
            ("photo_self_edit", CFG_PORTAL_PHOTO_SELF_EDIT),
            ("session_days", CFG_PORTAL_SESSION_DAYS),
        ):
            if field not in data:
                continue
            definition = ConfigurationDefinition.objects.filter(key=key, is_active=True).first()
            if definition is None:
                raise StudentSettingNotRegistered(key=key)
            current, _ = resolve_value(definition, tenant=self.tenant)
            if current != data[field]:
                set_value(
                    definition=definition, value=data[field], actor=request.user,
                    tenant=self.tenant, reason=reason,
                )
        return success_response("Student portal rules saved.", data=read_portal_rules(self.tenant))
