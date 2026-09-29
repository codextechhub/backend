"""The school's academic structure settings: its word for a term, its term names, its arms."""
from __future__ import annotations

from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_SETTINGS_UPDATE
from ..serializers import AcademicRulesSerializer
from .base import AcademicsViewMixin


class AcademicRulesView(AcademicsViewMixin, APIView):
    """GET, PUT /v1/academics/rules/

    What the school calls a term (Term or Semester), the names a new year's
    terms are given, and the arms a level's classes are generated with.

    Reading needs no key beyond signing in to this school: these are words
    every screen prints, a teacher's register as much as the settings screen.
    The tenant is the one the caller's own token names, so a member of another
    school asking for this one is answered 404 before the view runs. Changing
    needs ``school.settings.update``, because these are the school's settings,
    and a caller whose reach is the whole school, because they bind every
    branch. A branch-bound caller holding the key is refused with a 403
    (SHARED_RECORD_READ_ONLY) and nothing is written.

    PUT takes all three settings every time, plus an optional ``reason`` for
    the audit trail, and answers with the GET body. Refusals are 400s keyed on
    the field, in sentences. Changing the word never renames a term that
    already exists, and changing the names never renames an existing year's
    terms: they pre-fill the years created afterwards.

    docstring-name: Academic structure settings
    """

    def get_permissions(self):
        self.rbac_permission = (
            None if self.request.method in ("GET", "HEAD", "OPTIONS")
            else PERM_SETTINGS_UPDATE
        )
        return super().get_permissions()

    def get(self, request):
        from ..services.academic_rules import read_academic_rules

        return success_response(data=read_academic_rules(self.tenant).as_dict())

    def put(self, request):
        from vs_rbac.scoping import assert_caller_may_configure

        from ..services.academic_rules import write_academic_rules

        assert_caller_may_configure(
            request.user, self.tenant,
            message=(
                "Only a school-wide administrator can change the school's "
                "academic structure settings."
            ),
        )
        writer = AcademicRulesSerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        rules = write_academic_rules(
            self.tenant, request.user, reason=data.pop("reason", ""), **data,
        )
        return success_response(
            "Academic structure settings saved.", data=rules.as_dict(),
        )
