"""A school's display settings: its time zones, its date format and its clock.

Three values in the configuration engine:

* ``display.timezone`` decides which calendar day "today" is: when a fee falls
  overdue, which day the calendar highlights, the date an invoice raised just
  after midnight carries. It is read everywhere through
  :mod:`vs_config.clock`. Every school starts on Africa/Lagos, the platform
  default. A branch in another zone may keep its own, and everything that
  belongs to that branch (its students, the staff posted there, its invoices)
  is then judged on the branch's day.
* ``display.date_format`` and ``display.clock`` decide how the school's
  screens write a date and a time (:mod:`vs_config.display`). One each for the
  whole school; a branch has no format of its own.

Every signed-in member reads the effective values from the tenant block of the
login and ``/me`` responses. This view is where a school changes them.

The view shares :class:`~.settings.SchoolSettingsView` with the security and
payroll screens, so the same rules hold: ``school.settings.view`` reads,
``school.settings.update`` writes, the tenant is always ``request.tenant``, a
caller that is not a school gets a 404, and a school that has not gone live is
refused with TENANT_NOT_LIVE, whichever of the three values it asks about. A
school's values need a caller whose reach is the whole school; a branch's own
zone needs only that branch in the caller's reach. The module docstring of
``settings`` explains each.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError

from core.response import success_response
from vs_config.clock import (
    DEFAULT_TIME_ZONE,
    TIME_ZONE_KEY,
    forget_tenant_zone,
    is_valid_time_zone,
    tenant_today,
)
from vs_config.display import (
    CLOCK_KEY,
    CLOCKS,
    DATE_FORMAT_KEY,
    DATE_FORMATS,
    clock_options,
    date_format_options,
    resolve_display_choices,
)
from vs_config.exceptions import ConfigurationError
from vs_config.models import ConfigurationDefinition, ConfigurationValue
from vs_config.services.resolution import clear_value, resolve_value, set_value
from vs_rbac.scoping import (
    assert_caller_may_configure,
    only_branch_id,
    visible_branch_ids,
)

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

#: The keys this view writes, by body field.
_KEYS = {
    "timezone": TIME_ZONE_KEY,
    "date_format": DATE_FORMAT_KEY,
    "clock": CLOCK_KEY,
}

SCHOOL_REFUSAL = (
    "Only a school-wide administrator can change the school's display "
    "settings. Choose one of your branches to set its own time zone."
)
NOTHING_SENT = "Send a time zone, a date format or a clock to change."
BRANCH_FIELDS_REFUSAL = (
    "The date format and the clock are the school's, not a branch's. Save "
    "them without choosing a branch."
)
DELETE_NEEDS_BRANCH = (
    "Name the branch whose own time zone to remove. The school's time zone is "
    "changed, never removed."
)


def _zone_error(value) -> str:
    return (
        f"'{value}' is not a recognised time zone. Use an IANA name such as "
        f"{DEFAULT_TIME_ZONE}."
    )


def _zone_options(zone):
    """The suggested zones, with *zone* first when it is not among them."""
    options = [dict(option) for option in TIME_ZONE_OPTIONS]
    if not any(option["value"] == zone for option in options):
        options.insert(0, {"value": zone, "label": zone.replace("_", " ")})
    return options


class _ZoneField(serializers.CharField):
    def __init__(self, **kwargs):
        super().__init__(max_length=64, trim_whitespace=True, **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not is_valid_time_zone(value):
            raise serializers.ValidationError(_zone_error(value))
        return value


class DisplaySettingsUpdateSerializer(serializers.Serializer):
    """The body of a school-level ``PATCH``: any of the three, and a reason."""

    timezone = _ZoneField(required=False)
    date_format = serializers.ChoiceField(
        choices=DATE_FORMATS, required=False,
        error_messages={"invalid_choice": (
            "'{input}' is not a date format this school can use. Choose "
            "D_MMM_YYYY, DD_MM_YYYY or YYYY_MM_DD."
        )},
    )
    clock = serializers.ChoiceField(
        choices=CLOCKS, required=False,
        error_messages={"invalid_choice": (
            "'{input}' is not a clock this school can use. Choose H12 or H24."
        )},
    )
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if not any(field in attrs for field in _KEYS):
            raise serializers.ValidationError(NOTHING_SENT)
        return attrs


class BranchZoneUpdateSerializer(serializers.Serializer):
    """The body of a branch-level ``PATCH``: the branch's zone, and a reason."""

    timezone = _ZoneField()
    reason = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        refused = [
            field for field in ("date_format", "clock") if field in self.initial_data
        ]
        if refused:
            raise serializers.ValidationError(
                {field: [BRANCH_FIELDS_REFUSAL] for field in refused},
            )
        return attrs


class SchoolDisplaySettingsView(SchoolSettingsView):
    """GET/PATCH/DELETE /v1/i/me/settings/display/ - zones, date format, clock.

    Without ``?branch=`` the school's settings::

        {"timezone": "Africa/Lagos",
         "source": "school" | "platform" | "default",
         "options": [{"value": "Africa/Lagos",
                      "label": "Lagos (West Africa Time, UTC+1)"}, ...],
         "date_format": "D_MMM_YYYY",
         "date_format_options": [{"value": "D_MMM_YYYY", "label": "29 Sep 2026"}, ...],
         "clock": "H12",
         "clock_options": [{"value": "H12", "label": "12-hour (8:00 am, 2:30 pm)"}, ...],
         "branches": [{"id": 12, "name": "Nairobi Branch",
                       "timezone": "Africa/Nairobi", "source": "branch"}, ...]}

    ``source`` is ``school`` when this school has chosen its zone, ``platform``
    when the platform value applies, ``default`` when neither has been set and
    the definition's Africa/Lagos applies. A zone outside the suggested list is
    offered first in ``options``, so a select can show it. The date format
    labels are the school's own today written each way.

    ``branches`` are the branches the reader can see, the main branch first, each
    with the zone it keeps: ``source`` ``branch`` for its own, ``school`` when
    it follows the school's. A school with one branch lists none, because that
    branch's zone is the school's.

    With ``?branch=<id>`` the branch's zone alone::

        {"branch": {"id": 12, "name": "Nairobi Branch"},
         "timezone": "Africa/Nairobi", "source": "branch" | "school",
         "options": [...]}

    PATCH without a branch takes any of ``timezone``, ``date_format`` and
    ``clock`` (at least one) and an optional ``reason``, and needs a caller
    whose reach is the whole school. PATCH with a branch takes ``timezone`` and
    ``reason`` and sets that branch's own zone; DELETE with a branch removes it,
    so the branch follows the school's again. Both answer with the refreshed
    GET body for the same scope.

    Refusals: a zone the tz database does not know, a format or clock outside
    the choices, or a date format or clock sent for a branch, is a 400 keyed on
    the field and nothing is written. An empty body is a 400. A branch that is
    not this school's, or not one the caller can see, is a 404. A branch-level
    PATCH at a school with one branch, and a DELETE naming no branch, are 400s
    keyed on ``branch``. Every save is audited per value as
    ``config.value.updated`` (a removal as ``config.value.cleared``) with the
    value before and after.

    docstring-name: My school display settings
    """

    # -- scope ------------------------------------------------------------ #

    def _branch(self, request, tenant):
        """The ``?branch=`` this request names, or ``None`` for the school.

        The same 404 for another school's branch, an unknown one and one
        outside the caller's reach, so the parameter confirms nothing.
        """
        from vs_tenants.references import find_branch_in_tenant

        raw = (request.query_params.get("branch") or "").strip()
        if not raw:
            return None
        branch = find_branch_in_tenant(tenant, raw)
        if branch is None:
            raise NotFound("No such branch at this school.")
        visible = visible_branch_ids(request.user, tenant)
        if visible is not None and branch.pk not in visible:
            raise NotFound("No such branch at this school.")
        return branch

    @staticmethod
    def _definitions():
        found = {
            row.key: row
            for row in ConfigurationDefinition.objects.filter(
                key__in=list(_KEYS.values()), is_active=True,
            )
        }
        if TIME_ZONE_KEY not in found:
            raise NotFound("Display settings are not available.")
        return found

    # -- bodies ----------------------------------------------------------- #

    @staticmethod
    def _school_zone(definition, tenant):
        value, row = resolve_value(definition, tenant=tenant)
        if row is None:
            source = "default"
        elif row.scope_key == "platform":
            source = "platform"
        else:
            source = "school"
        zone = value if is_valid_time_zone(value) else DEFAULT_TIME_ZONE
        return zone, source

    @staticmethod
    def _branch_values(definition, tenant):
        """``{branch id: zone}`` for every branch of *tenant* with its own."""
        return {
            branch_id: value
            for branch_id, value in ConfigurationValue.all_objects.filter(
                definition=definition, tenant_id=tenant.pk, branch__isnull=False,
            ).values_list("branch_id", "value")
            if is_valid_time_zone(value)
        }

    def _school_payload(self, request, definitions, tenant):
        from vs_tenants.models import Branch

        zone, source = self._school_zone(definitions[TIME_ZONE_KEY], tenant)
        choices = resolve_display_choices(tenant)
        branches = []
        if only_branch_id(tenant) is None:
            own = self._branch_values(definitions[TIME_ZONE_KEY], tenant)
            rows = Branch.all_objects.filter(tenant=tenant).order_by("-is_main", "name")
            visible = visible_branch_ids(request.user, tenant)
            if visible is not None:
                rows = rows.filter(pk__in=visible)
            branches = [
                {
                    "id": branch.pk, "name": branch.name,
                    "timezone": own.get(branch.pk, zone),
                    "source": "branch" if branch.pk in own else "school",
                }
                for branch in rows
            ]
        return {
            "timezone": zone,
            "source": source,
            "options": _zone_options(zone),
            "date_format": choices[DATE_FORMAT_KEY][0],
            "date_format_options": date_format_options(tenant_today(tenant)),
            "clock": choices[CLOCK_KEY][0],
            "clock_options": clock_options(),
            "branches": branches,
        }

    def _branch_payload(self, definitions, tenant, branch):
        own = self._branch_values(definitions[TIME_ZONE_KEY], tenant).get(branch.pk)
        if own is None:
            zone, _ = self._school_zone(definitions[TIME_ZONE_KEY], tenant)
        else:
            zone = own
        return {
            "branch": {"id": branch.pk, "name": branch.name},
            "timezone": zone,
            "source": "branch" if own is not None else "school",
            "options": _zone_options(zone),
        }

    # -- verbs ------------------------------------------------------------ #

    def get(self, request):
        tenant = self.school_tenant(request)
        branch = self._branch(request, tenant)
        definitions = self._definitions()
        payload = (
            self._school_payload(request, definitions, tenant) if branch is None
            else self._branch_payload(definitions, tenant, branch)
        )
        return success_response("Display settings retrieved.", payload)

    def patch(self, request):
        tenant = self.school_tenant(request)
        branch = self._branch(request, tenant)
        if branch is not None:
            return self._patch_branch(request, tenant, branch)
        assert_caller_may_configure(request.user, tenant, message=SCHOOL_REFUSAL)
        definitions = self._definitions()
        serializer = DisplaySettingsUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        reason = data.get("reason") or "Updated from the school's display settings"
        self._write(
            definitions, tenant,
            {field: data[field] for field in _KEYS if field in data},
            actor=request.user, reason=reason,
        )
        return success_response(
            "Display settings saved.",
            self._school_payload(request, definitions, tenant),
        )

    def _patch_branch(self, request, tenant, branch):
        assert_caller_may_configure(
            request.user, tenant, branch, message=SCHOOL_REFUSAL,
        )
        if only_branch_id(tenant) is not None:
            raise ValidationError({"branch": [
                f"{tenant.name} has one branch, so its time zone is the "
                f"school's. Change the school's time zone instead.",
            ]})
        definitions = self._definitions()
        serializer = BranchZoneUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        reason = data.get("reason") or f"Updated from {branch.name}'s display settings"
        self._write(
            definitions, tenant, {"timezone": data["timezone"]},
            actor=request.user, reason=reason, branch=branch,
        )
        return success_response(
            f"{branch.name} keeps its own time zone.",
            self._branch_payload(definitions, tenant, branch),
        )

    def delete(self, request):
        tenant = self.school_tenant(request)
        branch = self._branch(request, tenant)
        if branch is None:
            raise ValidationError({"branch": [DELETE_NEEDS_BRANCH]})
        assert_caller_may_configure(
            request.user, tenant, branch, message=SCHOOL_REFUSAL,
        )
        definitions = self._definitions()
        reason = (
            str(request.data.get("reason") or "") if hasattr(request.data, "get") else ""
        ) or f"{branch.name} follows the school's time zone"
        clear_value(
            definition=definitions[TIME_ZONE_KEY], actor=request.user,
            tenant=tenant, branch=branch, reason=reason,
        )
        forget_tenant_zone(tenant)
        return success_response(
            f"{branch.name} follows the school's time zone again.",
            self._branch_payload(definitions, tenant, branch),
        )

    @staticmethod
    def _write(definitions, tenant, values, *, actor, reason, branch=None):
        """Write every value or none. A refusal is a 400 keyed on its field.

        The engine's own refusal (its write guard, its choices) keeps its
        error code, as the console's value endpoint reports it.
        """
        missing = [field for field in values if _KEYS[field] not in definitions]
        if missing:
            raise NotFound("Display settings are not available.")
        field = None
        try:
            with transaction.atomic():
                for field, value in values.items():
                    set_value(
                        definition=definitions[_KEYS[field]], value=value,
                        actor=actor, tenant=tenant, branch=branch, reason=reason,
                    )
        except ConfigurationError as exc:
            raise _Refused(field, exc)
        finally:
            forget_tenant_zone(tenant)


class _Refused(Exception):
    """The engine refused a value: a 400 keyed on the field, with its code.

    Rendered by ``core.exceptions.custom_exception_handler`` from its
    ``error_code``, ``message``, ``extra`` and ``http_status``.
    """

    http_status = status.HTTP_400_BAD_REQUEST

    def __init__(self, field, exc):
        super().__init__(exc.message)
        self.error_code = exc.error_code
        self.message = exc.message
        self.extra = {field: [exc.message]}
