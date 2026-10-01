"""Settings, Staff: the school's own rules for its staff, and its staff-number rule.

Both are the school's settings rather than a staff record, so both change under
``school.settings.update`` and are read under the register's view key, which
every screen that renders from them already holds. Who may write follows the
rule's reach, through ``vs_rbac.scoping.assert_caller_may_configure``: a rule
that binds every branch needs a caller whose reach is the whole school, and a
branch-bound caller holding the key is refused with a 403
(``SHARED_RECORD_READ_ONLY``) with nothing written. A branch's own staff-number
rule needs only that branch in the caller's reach.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound, ValidationError
from rest_framework import serializers
from rest_framework.views import APIView

from core.response import success_response

from ..constants import PERM_SETTINGS_UPDATE, PERM_VIEW
from ..serializers import StaffNumberPolicySerializer, StaffRulesSerializer
from .base import StaffViewMixin


class _SettingsView(StaffViewMixin, APIView):
    def get_permissions(self):
        self.rbac_permission = (
            PERM_VIEW if self._is_read() else PERM_SETTINGS_UPDATE
        )
        return super().get_permissions()


class StaffRulesView(_SettingsView):
    """GET, PUT /v1/i/me/staff/rules/

    The school's own staff rules: the role new staff start with, the documents
    expected on every record, what a person may change about themselves,
    whether a new hire waits for approval before being invited, and how leave
    is limited and counted. Each list of choices the screen offers travels with
    the values.

    PUT takes every rule every time, plus an optional ``reason`` for the audit
    trail, and answers with the GET body. Refusals are 400s keyed on the field,
    in sentences. A starting role carrying restricted permissions the caller
    does not hold is refused, so the setting is never a way round the grant
    ceiling.

    docstring-name: Staff rules
    """

    def get(self, request):
        from ..services.rules import read_staff_rules

        return success_response(data=read_staff_rules(self.tenant).as_dict(self.tenant))

    def put(self, request):
        from vs_rbac.scoping import assert_caller_may_configure

        from ..services.rules import write_staff_rules

        assert_caller_may_configure(
            request.user, self.tenant,
            message="Only a school-wide administrator can change the school's staff rules.",
        )
        writer = StaffRulesSerializer(
            data=request.data, context={"tenant": self.tenant, "request": request},
        )
        writer.is_valid(raise_exception=True)
        data = dict(writer.validated_data)
        rules = write_staff_rules(
            self.tenant, request.user, reason=data.pop("reason", ""), **data,
        )
        return success_response("Staff rules saved.", data=rules.as_dict(self.tenant))


class StaffLeaveGroupView(StaffViewMixin, APIView):
    """Assign a school's named leave group to one manageable staff record.

    An empty group clears the assignment. The school's settings writer may
    change a person only within their existing staff management scope.
    """

    rbac_permission = PERM_SETTINGS_UPDATE

    def put(self, request, pk):
        from ..services.rules import leave_rules

        class Assignment(serializers.Serializer):
            group_id = serializers.UUIDField(allow_null=True, required=True)

        staff = self.get_staff_for_write(pk)
        writer = Assignment(data=request.data)
        writer.is_valid(raise_exception=True)
        group_id = writer.validated_data["group_id"]
        group = next(
            (row for row in leave_rules(self.tenant).groups if row["id"] == str(group_id)),
            None,
        ) if group_id else None
        if group_id and group is None:
            raise ValidationError({"group_id": "Choose a leave group at this school."})
        wanted = group["id"] if group else ""
        if staff.leave_group != wanted:
            staff.leave_group = wanted
            staff.save(update_fields=["leave_group", "updated_at"])
        return success_response("Leave group saved.", data={"leave_group": group})


class StaffNumberPolicyView(_SettingsView):
    """GET, PUT, DELETE /v1/i/me/staff/number-policy/

    The staff-number rule: the school's, or with ``?branch=<id>`` the rule that
    branch's staff follow (their main posting). The body is ``{required,
    pattern, hint, auto_issue, source, suggestion}``, as the admission-number
    rule's is. ``source`` is ``branch`` when the branch has a rule of its own,
    ``school`` when the school has set one, and ``default`` when nobody has.
    ``suggestion`` is the number the next person would be given, or "" when
    the series cannot be continued.

    PUT with a branch writes that branch's own rule, all four values; DELETE
    with a branch removes it, so the branch follows the school's again. DELETE
    with no branch is a 400: the school's rule is changed, never removed.

    The branch must be this school's and one the caller can see, or the answer
    is 404. Readable before go-live, because the Add form renders the hint
    while a school is still onboarding.

    docstring-name: Staff number rule
    """

    pending_tenant_surface = ("get",)

    def _branch(self):
        from vs_rbac.scoping import WHOLE_TENANT, visible_branch_ids
        from vs_tenants.references import find_branch_in_tenant

        raw = (self.request.query_params.get("branch") or "").strip()
        if not raw:
            return None
        branch = find_branch_in_tenant(self.tenant, raw)
        if branch is None:
            raise NotFound("No such branch at this school.")
        visible = visible_branch_ids(self.request.user, self.tenant)
        if visible is not WHOLE_TENANT and branch.pk not in (visible or ()):
            raise NotFound("No such branch at this school.")
        return branch

    def _body(self, policy, branch):
        from ..services.number_policy import suggest_number

        return {
            **policy.as_dict(),
            "suggestion": suggest_number(self.tenant, policy=policy, branch=branch),
        }

    def _assert_may_write(self, branch):
        from vs_rbac.scoping import assert_caller_may_configure

        assert_caller_may_configure(
            self.request.user, self.tenant, branch,
            message=(
                "Only a school-wide administrator can change the school's staff "
                "number rule. Choose one of your branches to set its own."
            ),
        )

    def get(self, request):
        from ..services.number_policy import read_policy

        branch = self._branch()
        return success_response(data=self._body(read_policy(self.tenant, branch), branch))

    def put(self, request):
        from ..services.number_policy import write_policy

        branch = self._branch()
        self._assert_may_write(branch)
        writer = StaffNumberPolicySerializer(data=request.data)
        writer.is_valid(raise_exception=True)
        data = writer.validated_data
        policy = write_policy(
            self.tenant, request.user, branch=branch,
            required=data["required"], pattern=data["pattern"], hint=data["hint"],
            auto_issue=data.get("auto_issue"), reason=data.get("reason", ""),
        )
        return success_response("Staff number rule saved.", data=self._body(policy, branch))

    def delete(self, request):
        from ..services.number_policy import reset_branch_policy

        branch = self._branch()
        if branch is None:
            raise ValidationError({
                "branch": "Name the branch whose own rule to remove. The "
                          "school's rule is changed, never removed.",
            })
        self._assert_may_write(branch)
        policy = reset_branch_policy(self.tenant, branch, request.user)
        return success_response(
            f"{branch.name} follows the school's staff number rule again.",
            data=self._body(policy, branch),
        )
