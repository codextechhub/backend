"""Named Dynamic Roles: the API behind the Dynamic Role tab, and the engine path.

Security-critical cases first - permission refusals and tenant isolation on
every entry point, including the targets and values a rule may name - then the
checks that keep a Dynamic Role from reaching nobody by mistake, then the rules
deciding who approves, publishing, the activation audit and the previews.

A role a rule sends to is built with ``is_system_role=True`` because the engine
nominates only approving roles; a rule naming any other role is refused, which
is one of the cases below. Amounts are whole kobo, as every money column is.
"""
import itertools
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from schools.vs_staff.constants import LEAVE_DOCUMENT_TYPE
from vs_rbac.tests.helpers import (
    codex_tenant, make_assignment, make_branch, make_permission, make_role,
    make_role_permission, make_school, make_school_admin, make_vision_user,
)
from vs_tenants.models import Tenant
from vs_workflow.conditions import context as rule_context
from vs_workflow.constants import (
    AuditEventType, DocumentAudience, PERM_GROUP_MANAGE, PERM_GROUP_VIEW,
    PERM_TEMPLATE_VIEW,
)
from vs_workflow.exceptions import TemplateInvalidError
from vs_workflow.handlers.base import BaseWorkflowHandler
from vs_workflow.handlers.registry import list_registered_handlers
from vs_workflow.models import (
    WorkflowApproverGroup, WorkflowApproverGroupMember, WorkflowAuditLog,
    WorkflowDynamicRole, WorkflowInstance, WorkflowStageDynamicRule,
)
from vs_workflow.services import routing as routing_svc
from vs_workflow.services import templates as templates_svc
from vs_workflow.services.approvers import resolve_approvers
from vs_workflow.services.dynamic_roles import replace_rules, validate_rules
from vs_workflow.tests.test_services import _make_instance, _make_stage, _make_template
from vs_workflow.views import WorkflowDynamicRoleViewSet, WorkflowTemplateViewSet

_counter = itertools.count(1)

BASE = "/v1/workflow/dynamic-roles/"
LIST = WorkflowDynamicRoleViewSet.as_view({"get": "list", "post": "create"})
DETAIL = WorkflowDynamicRoleViewSet.as_view(
    {"get": "retrieve", "patch": "partial_update", "delete": "destroy"})
FIELDS = WorkflowDynamicRoleViewSet.as_view({"get": "fields"})
PREVIEW = WorkflowDynamicRoleViewSet.as_view({"post": "preview"})
TEMPLATE_PREVIEW = WorkflowTemplateViewSet.as_view({"post": "preview_approvers"})

factory = APIRequestFactory()

REFUND = "finance.refund"
WRITE_OFF = "finance.write_off"
USER_CREATION = "PLATFORM_USER_CREATION"
PAYOUT_BATCH = "payments.payout_batch"
LEAVE = LEAVE_DOCUMENT_TYPE
NAIRA = 100


def _grant(user, keys):
    role = make_role(user.tenant, name=f"dr-grant-{next(_counter)}", is_system_role=True)
    for key in keys:
        make_role_permission(role, make_permission(key))
    make_assignment(user.tenant, user, role)


def _call(view, method, user, tenant, data=None, path=BASE, **view_kwargs):
    if method == "get":
        request = factory.get(path, data or {})
    else:
        request = getattr(factory, method)(path, data, format="json")
    request.tenant = tenant
    request.rbac_tenant = tenant
    if user is not None:
        force_authenticate(request, user=user)
    return view(request, **view_kwargs)


def _body(resp):
    data = resp.data
    if isinstance(data, dict) and "data" in data and "success" in data:
        return data["data"]
    return data


def _rows(resp):
    """The rows of a list response, whatever envelope the pagination wraps them in."""
    data = _body(resp)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("results", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _amount_over(naira):
    return {"op": "gt", "field": "amount", "value": naira * NAIRA}


def _otherwise(**target):
    return {"condition": None, **target}


class _Fixture(TestCase):
    """Bright Star, with approving roles, a named person and a group - and
    Greenfield next door, whose people and rows must stay out of reach."""

    def setUp(self):
        n = next(_counter)
        self.school = make_school(slug=f"dr-bright-{n}", name="Bright Star")
        self.branch = make_branch(self.school)
        self.tenant = self.school.tenant

        self.manager = make_school_admin(self.branch, email=f"dr-manager-{n}@test.com")
        _grant(self.manager, [PERM_GROUP_MANAGE, PERM_GROUP_VIEW])
        self.viewer = make_school_admin(self.branch, email=f"dr-viewer-{n}@test.com")
        _grant(self.viewer, [PERM_GROUP_VIEW])
        self.nobody = make_school_admin(self.branch, email=f"dr-nobody-{n}@test.com")

        self.bursar_role = make_role(self.tenant, name="Bursar", key="dr-bursar",
                                     is_system_role=True)
        self.principal_role = make_role(self.tenant, name="Principal", key="dr-principal",
                                        is_system_role=True)
        self.lab_role = make_role(self.tenant, name="Lab technician", key="dr-lab-tech")
        self.bursar = make_school_admin(self.branch, email=f"dr-bursar-{n}@test.com")
        self.principal = make_school_admin(self.branch, email=f"dr-principal-{n}@test.com")
        self.adebayo = make_school_admin(self.branch, email=f"dr-adebayo-{n}@test.com")
        make_assignment(self.tenant, self.bursar, self.bursar_role)
        make_assignment(self.tenant, self.principal, self.principal_role)
        self.science = WorkflowApproverGroup.objects.create(
            tenant=self.tenant, code="dr-science", name="Science approvers")
        WorkflowApproverGroupMember.objects.create(
            group=self.science, kind="USER", user=self.adebayo)

        other = make_school(slug=f"dr-green-{n}", name="Greenfield")
        self.other_branch = make_branch(other)
        self.other_tenant = other.tenant
        self.outsider = make_school_admin(self.other_branch, email=f"dr-outsider-{n}@test.com")
        make_role(self.other_tenant, name="Bursar", key="dr-bursar", is_system_role=True)
        self.other_group = WorkflowApproverGroup.objects.create(
            tenant=self.other_tenant, code="dr-theirs", name="Theirs")

    def _dynamic_role(self, rules=None, *, code="spend", types=(REFUND,), tenant=None,
                      active=True):
        tenant = tenant or self.tenant
        dynamic_role = WorkflowDynamicRole.objects.create(
            tenant=tenant, code=code, name=code.title(), document_types=list(types),
            is_active=active)
        rules = rules or [_otherwise(target_kind="ROLE", role_key="dr-bursar")]
        replace_rules(dynamic_role, validate_rules(
            tenant=tenant, document_types=list(types), rules=rules))
        return dynamic_role

    def _stage_using(self, dynamic_role, doc_type=REFUND):
        template = _make_template(doc_type=doc_type, code=f"dr-tpl-{next(_counter)}")
        stage = _make_stage(template, code="spend")
        stage.approver_source = "DYNAMIC_ROLE"
        stage.dynamic_role = dynamic_role
        stage.save(update_fields=["approver_source", "dynamic_role"])
        return stage


# ── Access and isolation ─────────────────────────────────────────────────────

class DynamicRoleAccessTests(_Fixture):

    def test_anonymous_denied(self):
        resp = _call(LIST, "get", None, self.tenant)
        self.assertIn(resp.status_code,
                      (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_without_view_permission_denied(self):
        resp = _call(LIST, "get", self.nobody, self.tenant)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_view_permission_cannot_create(self):
        resp = _call(LIST, "post", self.viewer, self.tenant, {
            "code": "x", "name": "X", "document_types": [REFUND],
            "rules": [_otherwise(target_kind="ROLE", role_key="dr-bursar")]})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_view_permission_reads_the_list(self):
        self._dynamic_role()
        resp = _call(LIST, "get", self.viewer, self.tenant)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([row["code"] for row in _rows(resp)], ["spend"])

    def test_an_empty_list_is_still_a_200(self):
        resp = _call(LIST, "get", self.manager, self.tenant)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(_rows(resp), [])

    def test_the_list_holds_only_this_tenants_dynamic_roles(self):
        self._dynamic_role(tenant=self.other_tenant, code="theirs")
        self._dynamic_role(code="ours")
        rows = _rows(_call(LIST, "get", self.manager, self.tenant))
        self.assertEqual([row["code"] for row in rows], ["ours"])

    def test_another_tenants_dynamic_role_is_not_found(self):
        theirs = self._dynamic_role(tenant=self.other_tenant, code="theirs")
        resp = _call(DETAIL, "get", self.manager, self.tenant, pk=theirs.pk)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_another_tenants_dynamic_role_cannot_be_edited(self):
        theirs = self._dynamic_role(tenant=self.other_tenant, code="theirs")
        resp = _call(DETAIL, "patch", self.manager, self.tenant, {"name": "Ours now"},
                     pk=theirs.pk)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        theirs.refresh_from_db()
        self.assertEqual(theirs.name, "Theirs")


# ── Checks on save ───────────────────────────────────────────────────────────

class DynamicRoleRuleCheckTests(_Fixture):
    """Every way a rule could reach nobody, or reach outside the school, is refused on save."""

    def _create(self, rules, **extra):
        return _call(LIST, "post", self.manager, self.tenant, {
            "code": f"dr-{next(_counter)}", "name": "Spend", "document_types": [REFUND],
            "rules": rules, **extra})

    def _refused(self, rules, fragment, **extra):
        resp = self._create(rules, **extra)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)
        self.assertIn(fragment, str(resp.data))

    def _to_principal(self, condition):
        return {"condition": condition, "target_kind": "ROLE", "role_key": "dr-principal"}

    def _bursar_otherwise(self):
        return _otherwise(target_kind="ROLE", role_key="dr-bursar")

    def test_a_person_in_another_tenant_cannot_be_named(self):
        self._refused([_otherwise(target_kind="USER", user=str(self.outsider.pk))],
                      "no active user")

    def test_another_tenants_group_cannot_be_named(self):
        self._refused([_otherwise(target_kind="GROUP", group_code="dr-theirs")],
                      "no active approver group")

    def test_another_tenants_branch_cannot_be_tested(self):
        self._refused([
            self._to_principal({"op": "eq", "field": "branch",
                                "value": str(self.other_branch.pk)}),
            self._bursar_otherwise(),
        ], "not one of yours")

    def test_a_field_the_document_does_not_have_is_refused(self):
        self._refused([
            self._to_principal({"op": "gt", "field": "estimated_total", "value": 1}),
            self._bursar_otherwise(),
        ], "is not something")

    def test_an_operator_the_field_cannot_use_is_refused(self):
        self._refused([
            self._to_principal({"op": "contains", "field": "amount", "value": 1}),
            self._bursar_otherwise(),
        ], "cannot be compared")

    def test_an_amount_is_whole_kobo(self):
        self._refused([
            self._to_principal({"op": "gt", "field": "amount", "value": 12.5}),
            self._bursar_otherwise(),
        ], "whole kobo")

    def test_a_choice_must_be_on_the_list(self):
        self._refused([
            self._to_principal({"op": "eq", "field": "document.method", "value": "BARTER"}),
            self._bursar_otherwise(),
        ], "not one of the choices")

    def test_a_role_that_cannot_approve_is_refused_as_a_target(self):
        self._refused([_otherwise(target_kind="ROLE", role_key="dr-lab-tech")],
                      "not a role that can approve")

    def test_or_is_refused(self):
        self._refused([
            self._to_principal({"any": [_amount_over(1), _amount_over(2)]}),
            self._bursar_otherwise(),
        ], "joined by")

    def test_the_last_rule_must_be_otherwise(self):
        self._refused([self._to_principal(_amount_over(1))], "Otherwise")

    def test_otherwise_anywhere_but_last_is_refused(self):
        self._refused([self._bursar_otherwise(), self._to_principal(_amount_over(1))],
                      "must be last")

    def test_rules_are_required(self):
        resp = _call(LIST, "post", self.manager, self.tenant,
                     {"code": "dr-none", "name": "X", "document_types": [REFUND]})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_unknown_document_type_is_refused(self):
        self._refused([self._bursar_otherwise()], "is not a document type",
                      document_types=["no.such_type"])

    def test_the_code_is_unique_within_the_tenant(self):
        self._dynamic_role(code="spend")
        resp = _call(LIST, "post", self.manager, self.tenant, {
            "code": "spend", "name": "Again", "document_types": [REFUND],
            "rules": [self._bursar_otherwise()]})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_code_cannot_change(self):
        dynamic_role = self._dynamic_role(code="spend")
        resp = _call(DETAIL, "patch", self.manager, self.tenant, {"code": "renamed"},
                     pk=dynamic_role.pk)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


# ── Lifecycle and fields ─────────────────────────────────────────────────────

class DynamicRoleApiTests(_Fixture):

    def test_create_reads_back_rules_with_their_targets(self):
        resp = _call(LIST, "post", self.manager, self.tenant, {
            "code": "spend", "name": "Spend approver", "document_types": [REFUND],
            "rules": [
                {"condition": _amount_over(2_000_000), "target_kind": "ROLE",
                 "role_key": "dr-principal"},
                {"condition": {"all": [
                    {"op": "eq", "field": "requester.branch", "value": str(self.branch.pk)},
                    _amount_over(1_000_000),
                ]}, "target_kind": "USER", "user": str(self.adebayo.pk)},
                _otherwise(target_kind="GROUP", group_code="dr-science"),
            ]})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        rules = _body(resp)["rules"]
        self.assertEqual([r["target_kind"] for r in rules], ["ROLE", "USER", "GROUP"])
        self.assertEqual(rules[0]["role_name"], "Principal")
        self.assertEqual(rules[0]["condition"],
                         {"op": "gt", "field": "amount", "value": 200_000_000})
        self.assertEqual(rules[2]["group_name"], "Science approvers")
        self.assertTrue(rules[2]["is_fallback"])

    def test_a_patch_with_rules_replaces_them(self):
        dynamic_role = self._dynamic_role()
        resp = _call(DETAIL, "patch", self.manager, self.tenant,
                     {"rules": [_otherwise(target_kind="ROLE", role_key="dr-principal")]},
                     pk=dynamic_role.pk)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual([r.role_key for r in dynamic_role.rules.all()], ["dr-principal"])

    def test_a_dynamic_role_in_use_cannot_be_deleted(self):
        dynamic_role = self._dynamic_role()
        self._stage_using(dynamic_role)
        resp = _call(DETAIL, "delete", self.manager, self.tenant, pk=dynamic_role.pk)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(WorkflowDynamicRole.objects.filter(pk=dynamic_role.pk).exists())

    def test_an_unused_dynamic_role_is_deleted(self):
        dynamic_role = self._dynamic_role()
        resp = _call(DETAIL, "delete", self.manager, self.tenant, pk=dynamic_role.pk)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_a_document_type_still_in_use_cannot_be_dropped(self):
        dynamic_role = self._dynamic_role(types=(REFUND,))
        self._stage_using(dynamic_role, doc_type=REFUND)
        resp = _call(DETAIL, "patch", self.manager, self.tenant,
                     {"document_types": [WRITE_OFF]}, pk=dynamic_role.pk)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("still uses", str(resp.data))

    def _fields(self, *types):
        resp = _call(FIELDS, "get", self.viewer, self.tenant, {"document_type": list(types)},
                     path=BASE + "fields/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return _body(resp)

    def test_fields_for_one_type_include_its_own(self):
        fields = {f["key"]: f for f in self._fields(REFUND)["fields"]}
        self.assertTrue({"amount", "branch", "document.method", "requester.id",
                         "requester.role_keys", "requester.branch"} <= set(fields))
        self.assertEqual(fields["amount"]["type"], "MONEY")
        self.assertIn("CHEQUE", [c["value"] for c in fields["document.method"]["choices"]])

    def test_fields_for_several_types_offer_the_type_instead_of_its_fields(self):
        keys = {f["key"] for f in self._fields(REFUND, WRITE_OFF)["fields"]}
        self.assertIn("document_type", keys)
        self.assertIn("amount", keys)
        self.assertNotIn("document.method", keys)

    def test_only_approving_roles_are_offered_as_targets(self):
        keys = [r["key"] for r in self._fields(REFUND)["approver_roles"]]
        self.assertIn("dr-bursar", keys)
        self.assertNotIn("dr-lab-tech", keys)


# ── Who the rules choose ─────────────────────────────────────────────────────

class DynamicRoleResolutionTests(_Fixture):
    """The first rule that holds decides, and the Otherwise row catches the rest."""

    def setUp(self):
        super().setUp()
        self.requester = make_school_admin(self.branch, email=f"dr-req-{next(_counter)}@test.com")
        self._post(self.requester, self.branch)
        self.dynamic_role = self._dynamic_role([
            {"condition": _amount_over(2_000_000), "target_kind": "ROLE",
             "role_key": "dr-principal"},
            {"condition": {"op": "contains", "field": "requester.role_keys",
                           "value": "dr-lab-tech"},
             "target_kind": "GROUP", "group_code": "dr-science"},
            {"condition": {"all": [
                {"op": "eq", "field": "requester.branch", "value": str(self.branch.pk)},
                _amount_over(1_000_000),
            ]}, "target_kind": "USER", "user": str(self.adebayo.pk)},
            _otherwise(target_kind="ROLE", role_key="dr-bursar"),
        ])
        self.stage = self._stage_using(self.dynamic_role)

    @staticmethod
    def _post(user, branch):
        user.branch = branch
        user.save(update_fields=["branch"])

    def _instance(self, naira, requester=None):
        instance = _make_instance(self.stage.template, requester or self.requester)
        instance.branch = self.branch
        instance.save(update_fields=["branch"])
        document = SimpleNamespace(workflow_amount_field="amount", amount=naira * NAIRA,
                                   method="CASH")
        patcher = patch.object(WorkflowInstance, "document", document)
        patcher.start()
        self.addCleanup(patcher.stop)
        return instance

    def _approvers(self, naira, requester=None):
        return {e.user.pk for e in resolve_approvers(self.stage, self._instance(naira, requester))}

    def test_a_large_amount_goes_to_the_principal(self):
        self.assertEqual(self._approvers(3_000_000), {self.principal.pk})

    def test_the_requesters_role_sends_to_a_group(self):
        make_assignment(self.tenant, self.requester, self.lab_role)
        self.assertEqual(self._approvers(100), {self.adebayo.pk})

    def test_branch_and_amount_together_send_to_a_person(self):
        self.assertEqual(self._approvers(1_500_000), {self.adebayo.pk})

    def test_and_needs_every_part_to_hold(self):
        self._post(self.requester, make_branch(self.school, name="Ikeja Branch", is_main=False))
        self.assertEqual(self._approvers(1_500_000), {self.bursar.pk})

    def test_otherwise_catches_the_rest(self):
        self.assertEqual(self._approvers(100), {self.bursar.pk})

    def test_a_deactivated_dynamic_role_resolves_to_nobody(self):
        self.dynamic_role.is_active = False
        self.dynamic_role.save(update_fields=["is_active"])
        self.assertEqual(self._approvers(100), set())

    def test_the_requester_never_approves_their_own(self):
        self._post(self.principal, self.branch)
        self.assertEqual(self._approvers(3_000_000, requester=self.principal), set())

    def test_activation_records_the_dynamic_role_and_its_choice(self):
        instance = self._instance(3_000_000)
        routing_svc._activate_stage(instance, self.stage, attempt=1)
        log = WorkflowAuditLog.objects.get(
            instance=instance, event_type=AuditEventType.STAGE_ACTIVATED)
        recorded = log.context["dynamic_role"]
        self.assertEqual(recorded["code"], "spend")
        self.assertEqual(recorded["matched_target"],
                         {"kind": "ROLE", "key": "dr-principal", "name": "Principal"})
        self.assertEqual([e["picked"] for e in recorded["evaluations"]], [True])


class RequesterFactsTests(_Fixture):

    def test_a_domain_apps_facts_join_the_requester(self):
        provider = lambda user, tenant: {"job_title": "Lab technician", "id": "forged"}  # noqa: E731
        with patch.object(rule_context, "_PROVIDERS", [provider]):
            facts = rule_context.requester_facts(self.adebayo, self.tenant)
        self.assertEqual(facts["job_title"], "Lab technician")
        self.assertEqual(facts["id"], str(self.adebayo.pk))

    def test_a_failing_provider_contributes_nothing(self):
        def failing(user, tenant):
            raise RuntimeError("no staff record")

        with patch.object(rule_context, "_PROVIDERS", [failing]), \
                self.assertLogs("vs_workflow.conditions.context", level="ERROR"):
            facts = rule_context.requester_facts(self.adebayo, self.tenant)
        self.assertEqual(set(facts), {"id", "branch", "role_keys"})

    def test_role_keys_are_the_active_roles_held_in_this_tenant(self):
        make_assignment(self.tenant, self.adebayo, self.lab_role)
        self.assertIn("dr-lab-tech",
                      rule_context.requester_role_keys(self.adebayo, self.tenant))


# ── Publishing ───────────────────────────────────────────────────────────────

class PublishWithDynamicRoleTests(_Fixture):

    def _publish(self, stage_extra, *, tenant="own", doc=REFUND, code=None):
        return templates_svc.publish_template(
            tenant=self.tenant if tenant == "own" else tenant, document_type=doc,
            code=code or f"dr-pub-{next(_counter)}", name="T",
            stages_payload=[{"code": "s1", "label": "Approval", "kind": "APPROVAL",
                             "order": 1, "approver_source": "DYNAMIC_ROLE", **stage_extra}])

    def test_a_stage_names_its_dynamic_role(self):
        dynamic_role = self._dynamic_role()
        template = self._publish({"dynamic_role_code": "spend"})
        self.assertEqual(template.stages.get(code="s1").dynamic_role_id, dynamic_role.pk)

    def test_an_unknown_dynamic_role_is_refused(self):
        with self.assertRaises(TemplateInvalidError):
            self._publish({"dynamic_role_code": "nope"})

    def test_a_dynamic_role_for_another_document_type_is_refused(self):
        self._dynamic_role(types=(WRITE_OFF,))
        with self.assertRaises(TemplateInvalidError):
            self._publish({"dynamic_role_code": "spend"})

    def test_another_tenants_dynamic_role_is_refused(self):
        self._dynamic_role(tenant=self.other_tenant, code="theirs")
        with self.assertRaises(TemplateInvalidError):
            self._publish({"dynamic_role_code": "theirs"})

    def test_a_shared_template_cannot_use_one(self):
        self._dynamic_role()
        with self.assertRaises(TemplateInvalidError):
            self._publish({"dynamic_role_code": "spend"}, tenant=None)

    def test_naming_a_dynamic_role_clears_the_stages_own_rules(self):
        code = f"dr-pub-{next(_counter)}"
        self._publish({"dynamic_role_rules": [{"role_key": "dr-bursar", "condition": None}]},
                      code=code)
        dynamic_role = self._dynamic_role()
        stage = self._publish({"dynamic_role_code": "spend"}, code=code).stages.get(code="s1")
        self.assertEqual(stage.dynamic_role_id, dynamic_role.pk)
        self.assertEqual(WorkflowStageDynamicRule.objects.filter(stage=stage).count(), 0)


# ── Previews ─────────────────────────────────────────────────────────────────

class DynamicRolePreviewTests(_Fixture):

    def _rules(self):
        return [
            {"condition": _amount_over(2_000_000), "target_kind": "ROLE",
             "role_key": "dr-principal"},
            _otherwise(target_kind="ROLE", role_key="dr-bursar"),
        ]

    def _preview(self, data, user=None):
        return _call(PREVIEW, "post", user or self.viewer, self.tenant, data,
                     path=BASE + "preview/")

    def test_draft_rules_are_tried_for_a_requester(self):
        requester = make_school_admin(self.branch, email=f"dr-try-{next(_counter)}@test.com")
        resp = self._preview({"requester": str(requester.pk), "document_types": [REFUND],
                              "rules": self._rules(),
                              "sample": {"amount": 3_000_000 * NAIRA}})
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        body = _body(resp)
        self.assertEqual(body["dynamic_role"]["matched_order"], 0)
        self.assertEqual([a["user"]["id"] for a in body["approvers"]], [str(self.principal.pk)])

    def test_a_requester_from_another_tenant_is_not_found(self):
        resp = self._preview({"requester": str(self.outsider.pk), "document_types": [REFUND],
                              "rules": self._rules()})
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_rules_that_would_not_save_are_a_400(self):
        resp = self._preview({"requester": str(self.adebayo.pk), "document_types": [REFUND],
                              "rules": [{"condition": _amount_over(1), "target_kind": "ROLE",
                                         "role_key": "dr-principal"}]})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Otherwise", str(resp.data))

    def test_without_view_permission_denied(self):
        resp = self._preview({"requester": str(self.adebayo.pk), "document_types": [REFUND],
                              "rules": self._rules()}, user=self.nobody)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_template_preview_runs_a_saved_dynamic_role(self):
        _grant(self.viewer, [PERM_TEMPLATE_VIEW])
        self._dynamic_role(self._rules())
        requester = make_school_admin(self.branch, email=f"dr-tpl-try-{next(_counter)}@test.com")
        resp = _call(TEMPLATE_PREVIEW, "post", self.viewer, self.tenant, {
            "requester": str(requester.pk), "approver_source": "DYNAMIC_ROLE",
            "dynamic_role_code": "spend", "sample": {"amount": 100 * NAIRA},
        }, path="/v1/workflow/templates/preview-approvers/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual([a["user"]["id"] for a in _body(resp)["approvers"]],
                         [str(self.bursar.pk)])


# ── Who raises a document type ───────────────────────────────────────────────

class DocumentAudienceTests(_Fixture):
    """A Dynamic Role serves only the document types its own tenant raises.

    Bright Star never raises platform user creation or a payout batch, so rules
    for either would never run. It is not offered them, cannot save a Dynamic
    Role for them and cannot try one, while the platform still sees and saves
    its own types. Leave is the reverse case: kept on a school's staff records,
    never raised by the platform.
    """

    def setUp(self):
        super().setUp()
        self.platform = codex_tenant()
        self.cx_admin = make_vision_user(email=f"dr-cx-{next(_counter)}@codex.test")
        _grant(self.cx_admin, [PERM_GROUP_MANAGE, PERM_GROUP_VIEW])
        self.cx_role = make_role(self.platform, name="CX approver",
                                 key=f"dr-cx-approver-{next(_counter)}", is_system_role=True)

    def _types_offered(self, user, tenant):
        resp = _call(FIELDS, "get", user, tenant, path=BASE + "fields/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        return {t["value"] for t in _body(resp)["document_types"]}

    def test_a_school_is_not_offered_a_platform_only_type(self):
        offered = self._types_offered(self.viewer, self.tenant)
        self.assertNotIn(USER_CREATION, offered)
        self.assertNotIn(PAYOUT_BATCH, offered)
        self.assertTrue({REFUND, LEAVE} <= offered)

    def test_the_platform_is_offered_its_own_types_and_not_a_schools(self):
        offered = self._types_offered(self.cx_admin, self.platform)
        self.assertTrue({USER_CREATION, PAYOUT_BATCH, REFUND} <= offered)
        self.assertNotIn(LEAVE, offered)

    def test_a_school_asking_for_a_platform_only_types_fields_is_refused(self):
        resp = _call(FIELDS, "get", self.viewer, self.tenant,
                     {"document_type": [USER_CREATION]}, path=BASE + "fields/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("never raised here", str(resp.data))

    def test_a_school_cannot_save_one_for_a_platform_only_type(self):
        resp = _call(LIST, "post", self.manager, self.tenant, {
            "code": "dr-cx-only", "name": "Platform users", "document_types": [USER_CREATION],
            "rules": [_otherwise(target_kind="ROLE", role_key="dr-bursar")]})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, resp.data)
        self.assertIn("never raised here", str(resp.data))
        self.assertFalse(WorkflowDynamicRole.all_objects.filter(
            tenant=self.tenant, code="dr-cx-only").exists())

    def test_a_school_cannot_try_rules_for_a_platform_only_type(self):
        resp = _call(PREVIEW, "post", self.viewer, self.tenant, {
            "requester": str(self.adebayo.pk), "document_types": [PAYOUT_BATCH],
            "rules": [_otherwise(target_kind="ROLE", role_key="dr-bursar")],
        }, path=BASE + "preview/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("never raised here", str(resp.data))

    def test_the_platform_saves_one_for_its_own_type(self):
        resp = _call(LIST, "post", self.cx_admin, self.platform, {
            "code": "dr-cx-users", "name": "Platform users", "document_types": [USER_CREATION],
            "rules": [_otherwise(target_kind="ROLE", role_key=self.cx_role.key)]})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(_body(resp)["document_types"], [USER_CREATION])

    def test_the_platform_cannot_save_one_for_a_school_only_type(self):
        with self.assertRaises(TemplateInvalidError) as caught:
            validate_rules(tenant=self.platform, document_types=[LEAVE],
                           rules=[_otherwise(target_kind="ROLE", role_key=self.cx_role.key)])
        self.assertIn("never raised here", caught.exception.message)

    def test_every_handler_says_who_raises_it(self):
        for document_type, handler in list_registered_handlers().items():
            if type(handler).__module__.startswith("vs_workflow.tests"):
                continue
            declared = any("audience" in vars(klass) for klass in type(handler).__mro__
                           if klass is not BaseWorkflowHandler)
            self.assertTrue(declared, f"{document_type}: its handler must declare its audience")
            self.assertIn(handler.audience, DocumentAudience.values, document_type)

    def test_audiences_are_spelled_as_tenant_kinds(self):
        # The registry compares a tenant's kind with an audience directly.
        self.assertEqual(DocumentAudience.PLATFORM, Tenant.Kind.PLATFORM)
        self.assertEqual(DocumentAudience.SCHOOL, Tenant.Kind.SCHOOL)
