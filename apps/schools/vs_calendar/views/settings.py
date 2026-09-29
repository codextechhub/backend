"""The school's calendar and timetable settings."""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_SETTINGS_UPDATE
from ..serializers import CalendarRulesSerializer
from .base import CalendarViewMixin


class CalendarRulesView(CalendarViewMixin, APIView):
    """GET, PUT /v1/academics/calendar/rules/

    The days the school teaches, the day its week starts, which kinds of
    calendar entry close the school, what publishing a class timetable needs,
    who may invigilate, and the default length of a period. See
    ``services.calendar_rules`` for what each one changes.

    Reading needs no key beyond signing in to this school: the teaching days
    and the week start shape every calendar screen and every grid, a teacher's
    own week as much as the settings screen. The tenant is the one the caller's
    own token names, so a member of another school asking for this one is
    answered 404 before the view runs. Changing needs ``school.settings.update``,
    because these are the school's settings, and a caller whose reach is the
    whole school, because they bind every branch. A branch-bound caller holding
    the key is refused with a 403 (SHARED_RECORD_READ_ONLY) and nothing is
    written.

    PUT takes all seven settings every time, plus an optional ``reason`` for
    the audit trail, and answers with the GET body. Refusals are 400s keyed on
    the field, in sentences. Nothing already stored changes: an event keeps its
    own ``closes_school``, a lesson on a day the school stops teaching stays
    (and is drawn, flagged, until it is cleared), and a published timetable
    stays published.

    docstring-name: Calendar and timetable settings
    """

    pagination_class = None

    def get_permissions(self):
        self.rbac_permission = (
            None if self.request.method in ("GET", "HEAD", "OPTIONS")
            else PERM_SETTINGS_UPDATE
        )
        return super().get_permissions()

    def get(self, request):
        from ..services.calendar_rules import calendar_rules_body

        return success_response(data=calendar_rules_body(self.tenant))

    def put(self, request):
        from vs_rbac.scoping import assert_caller_may_configure

        from ..services.calendar_rules import calendar_rules_body, write_calendar_rules

        assert_caller_may_configure(
            request.user, self.tenant,
            message=(
                "Only a school-wide administrator can change the school's "
                "calendar and timetable settings."
            ),
        )
        writer = CalendarRulesSerializer(
            data=request.data, context={"tenant": self.tenant},
        )
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        rules = write_calendar_rules(
            self.tenant, request.user, reason=data.pop("reason", ""), **data,
        )
        return success_response(
            "Calendar and timetable settings saved.",
            data=calendar_rules_body(self.tenant, rules),
        )
