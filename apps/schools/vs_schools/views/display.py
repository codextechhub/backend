"""A school's display settings: the time zone its calendar runs in.

The value is ``display.timezone`` in the configuration engine, read everywhere
through :mod:`vs_config.clock`. It decides which calendar day "today" is for the
school: when a fee falls overdue, which day the calendar highlights, the date
an invoice raised just after midnight carries. Every school starts on
Africa/Lagos, the platform default.

The view shares :class:`~.settings.SchoolSettingsView` with the security and
payroll screens, so the same rules hold: ``school.settings.view`` reads,
``school.settings.update`` writes, a write needs a caller whose reach is the
whole school, the tenant is always ``request.tenant``, a caller that is not a
school gets a 404, and a school that has not gone live is refused with
TENANT_NOT_LIVE. The module docstring of ``settings`` explains
each.
"""
from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.exceptions import NotFound

from core.response import error_response, success_response
from vs_config.clock import (
    DEFAULT_TIME_ZONE,
    TIME_ZONE_KEY,
    forget_tenant_zone,
    is_valid_time_zone,
)
from vs_config.exceptions import ConfigurationError
from vs_config.models import ConfigurationDefinition
from vs_config.services.resolution import resolve_value, set_value
from vs_rbac.scoping import assert_caller_may_configure

from .settings import SchoolSettingsView


#: The zones a school is offered, west to east. A PATCH accepts any IANA zone;
#: this list is only what the screen suggests.
TIME_ZONE_OPTIONS = (
    {"value": "Africa/Abidjan", "label": "Abidjan (Greenwich Mean Time, UTC+0)"},
    {"value": "Africa/Accra", "label": "Accra (Greenwich Mean Time, UTC+0)"},
    {"value": "Africa/Dakar", "label": "Dakar (Greenwich Mean Time, UTC+0)"},
    {"value": "Africa/Freetown", "label": "Freetown (Greenwich Mean Time, UTC+0)"},
    {"value": "Africa/Monrovia", "label": "Monrovia (Greenwich Mean Time, UTC+0)"},
    {"value": "Europe/London", "label": "London (UK time, UTC+0, UTC+1 in summer)"},
    {"value": "Africa/Casablanca", "label": "Casablanca (Morocco time, usually UTC+1)"},
    {"value": "Africa/Lagos", "label": "Lagos (West Africa Time, UTC+1)"},
    {"value": "Africa/Douala", "label": "Douala (West Africa Time, UTC+1)"},
    {"value": "Africa/Kinshasa", "label": "Kinshasa (West Africa Time, UTC+1)"},
    {"value": "Africa/Luanda", "label": "Luanda (West Africa Time, UTC+1)"},
    {"value": "Africa/Johannesburg", "label": "Johannesburg (South Africa Standard Time, UTC+2)"},
    {"value": "Africa/Harare", "label": "Harare (Central Africa Time, UTC+2)"},
    {"value": "Africa/Lusaka", "label": "Lusaka (Central Africa Time, UTC+2)"},
    {"value": "Africa/Kigali", "label": "Kigali (Central Africa Time, UTC+2)"},
    {"value": "Africa/Cairo", "label": "Cairo (Eastern European Time, UTC+2, UTC+3 in summer)"},
    {"value": "Africa/Nairobi", "label": "Nairobi (East Africa Time, UTC+3)"},
    {"value": "Africa/Kampala", "label": "Kampala (East Africa Time, UTC+3)"},
    {"value": "Africa/Dar_es_Salaam", "label": "Dar es Salaam (East Africa Time, UTC+3)"},
    {"value": "Africa/Addis_Ababa", "label": "Addis Ababa (East Africa Time, UTC+3)"},
)


class DisplaySettingsUpdateSerializer(serializers.Serializer):
    """The body of ``PATCH /v1/i/me/settings/display/``."""

    timezone = serializers.CharField(max_length=64, trim_whitespace=True)
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_timezone(self, value):
        if not is_valid_time_zone(value):
            raise serializers.ValidationError(
                f"'{value}' is not a recognised time zone. Use an IANA name such "
                f"as {DEFAULT_TIME_ZONE}."
            )
        return value


class SchoolDisplaySettingsView(SchoolSettingsView):
    """GET/PATCH /v1/i/me/settings/display/ - the school's time zone.

    A school-level setting only: ``display.timezone`` allows no branch value,
    so ``?branch=`` is not read, and a PATCH needs a caller whose reach is the
    whole school. The body::

        {"timezone": "Africa/Lagos",
         "source": "school" | "platform" | "default",
         "options": [{"value": "Africa/Lagos",
                      "label": "Lagos (West Africa Time, UTC+1)"}, ...]}

    ``source`` is ``school`` when this school has chosen, ``platform`` when the
    platform value applies, ``default`` when neither has been set and the
    definition's Africa/Lagos applies. A school on a zone outside the suggested
    list finds its own zone first in ``options``, so a select can show it.

    PATCH takes ``{"timezone": "<IANA name>", "reason"?: "..."}`` and answers
    with the refreshed GET body. Any zone the tz database knows is accepted; a
    name it does not know is a 400 keyed on ``timezone``, and nothing is
    written. A save is audited as ``config.value.updated`` with the zone before
    and after.

    docstring-name: My school display settings
    """

    def _definition(self):
        definition = ConfigurationDefinition.objects.filter(
            key=TIME_ZONE_KEY, is_active=True,
        ).first()
        if definition is None:
            raise NotFound("Display settings are not available.")
        return definition

    def _payload(self, definition, tenant):
        value, row = resolve_value(definition, tenant=tenant)
        if row is None:
            source = "default"
        elif row.scope_key == "platform":
            source = "platform"
        else:
            source = "school"
        zone = value if is_valid_time_zone(value) else DEFAULT_TIME_ZONE
        options = [dict(option) for option in TIME_ZONE_OPTIONS]
        if not any(option["value"] == zone for option in options):
            options.insert(0, {"value": zone, "label": zone.replace("_", " ")})
        return {"timezone": zone, "source": source, "options": options}

    def get(self, request):
        tenant = self.school_tenant(request)
        return success_response(
            "Display settings retrieved.",
            self._payload(self._definition(), tenant),
        )

    def patch(self, request):
        tenant = self.school_tenant(request)
        assert_caller_may_configure(
            request.user, tenant,
            message="Only a school-wide administrator can change the school's time zone.",
        )
        definition = self._definition()
        serializer = DisplaySettingsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = (
            serializer.validated_data.get("reason")
            or "Updated from the school's display settings"
        )
        try:
            set_value(
                definition=definition,
                value=serializer.validated_data["timezone"],
                actor=request.user,
                tenant=tenant,
                reason=reason,
            )
        except ConfigurationError as exc:
            return error_response(
                exc.message,
                error={"code": exc.error_code, "detail": {"timezone": [exc.message]}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        forget_tenant_zone(tenant)
        return success_response(
            "Display settings saved.",
            self._payload(definition, tenant),
        )
