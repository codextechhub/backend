"""Workflow screens receive words for every condition, stage, role and document type.

A template page once showed a stage's condition as ``total >= 50000000`` in a
code box, a route as ``ENTRY -> sign-off``, an approving role as ``bursar`` and
a stuck approval's fix as "assign someone to the bursar role". The server holds
the labels, the catalogue and the names, so it sends the words: these tests
hold it to that.
"""
import itertools
import re

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from vs_rbac.tests.helpers import (
    codex_tenant, make_assignment, make_branch, make_permission, make_role,
    make_role_permission, make_school, make_school_admin,
)
from vs_workflow.conditions import evaluate_condition, register_condition
from vs_workflow.conditions.describe import (
    ALWAYS, UNKNOWN_FIELD, ConditionDescriber, ConditionNames, describe_condition,
)
from vs_workflow.conditions.evaluator import CHECK_FAILED
from vs_workflow.conditions.registry import UNDESCRIBED_FUNCTION
from vs_workflow.constants import PERM_TEMPLATE_VIEW
from vs_workflow.serializers import WorkflowTemplateReadSerializer
from vs_workflow.services.release import stage_requirement
from vs_workflow.services.templates import publish_template
from vs_workflow.views import WorkflowTemplateViewSet

_counter = itertools.count(1)
#: A field path, an operator key or a dotted document type shown as a label.
CODE_SHAPED = re.compile(r"[A-Za-z]_[A-Za-z]|[a-z]\.[a-z]|\b(?:gte|lte|not_in)\b")

DOCUMENT_TYPES = WorkflowTemplateViewSet.as_view({"get": "document_types"})
REQUISITION = "procurement.requisition"
PETTY_CASH_RETURN = "finance.petty_cash_return"


def assert_words(test, text):
    test.assertTrue(text)
    test.assertIsNone(CODE_SHAPED.search(text), text)


class ConditionSentenceTests(TestCase):
    """:func:`describe_condition` reads a stored condition the way a bursar says it."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug=f"cond-words-{next(_counter)}", name="Lagoon View")
        cls.ikeja = make_branch(cls.school, name="Ikeja Branch")
        cls.tenant = cls.school.tenant
        make_role(cls.tenant, name="Bursar", key="bursar")

    def test_a_stage_threshold_reads_the_documents_amount_in_naira(self):
        text = describe_condition(
            {"op": "gte", "field": "estimated_total", "value": 50_000_000},
            document_type=REQUISITION, reads_document=True,
        )
        self.assertEqual(text, "Amount is at least ₦500,000.00")

    def test_a_choice_and_money_from_the_handler_read_by_label(self):
        text = describe_condition(
            {"any": [{"op": "gt", "field": "shortage", "value": 1_000_000},
                     {"op": "eq", "field": "kind", "value": "CLOSE"}]},
            document_type=PETTY_CASH_RETURN, reads_document=True,
        )
        self.assertIn("Petty cash count shortage is more than ₦10,000.00", text)
        self.assertIn(" or Petty cash return kind is ", text)
        assert_words(self, text)

    def test_a_rule_names_the_branch_and_the_role_it_holds(self):
        names = ConditionNames(self.tenant)
        describer = ConditionDescriber(names=names)
        text = describer.describe({"all": [
            {"op": "eq", "field": "requester.branch", "value": str(self.ikeja.pk)},
            {"op": "contains", "field": "requester.role_keys", "value": "bursar"},
        ]})
        self.assertEqual(text, "Their branch is Ikeja Branch and Their role includes Bursar")

    def test_what_cannot_be_named_reads_neutrally_never_as_its_key(self):
        cases = (
            {"op": "eq", "field": "legacy_flag_code", "value": "FAST_TRACK"},
            {"op": "eq", "field": "requester.branch", "value": "999999"},
            {"op": "eq", "field": "kind", "value": "RETIRED_KIND"},
            {"fn": "tests.undescribed_label_check"},
        )
        for condition in cases:
            with self.subTest(condition=condition):
                text = ConditionDescriber(
                    document_type=PETTY_CASH_RETURN, tenant=self.tenant,
                ).describe(condition, reads_document=True)
                assert_words(self, text)
        self.assertTrue(describe_condition(cases[0]).startswith(UNKNOWN_FIELD))
        self.assertEqual(describe_condition(None), ALWAYS)

    def test_a_named_function_reads_as_its_registered_description(self):
        @register_condition("tests.label_raised_at", describe=lambda args: (
            f"Raised by someone at {args.get('branch_name', 'a branch')}"))
        def _raised_at(document, args=None):
            return True

        self.assertEqual(
            describe_condition({"fn": "tests.label_raised_at", "args": {"branch_name": "Ikeja Branch"}}),
            "Raised by someone at Ikeja Branch",
        )
        self.assertEqual(describe_condition({"fn": "tests.no_such_function"}), UNDESCRIBED_FUNCTION)


class TraceNamesNoExceptionTests(TestCase):
    def test_a_check_that_raises_is_recorded_in_words(self):
        @register_condition("tests.label_boom")
        def _boom(document, args=None):
            raise ZeroDivisionError("division by zero")

        result, trace = evaluate_condition({"fn": "tests.label_boom"}, {})
        self.assertFalse(result)
        self.assertEqual(trace["error"], CHECK_FAILED)
        self.assertNotIn("ZeroDivisionError", str(trace))

        result, trace = evaluate_condition({"op": "gt", "field": "amount", "value": 5}, {"amount": "x"})
        self.assertFalse(result)
        self.assertEqual(trace["error"], CHECK_FAILED)


class TemplateLabelsTests(TestCase):
    """A template as the template page reads it carries the words for each part."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug=f"tpl-words-{next(_counter)}", name="Lagoon View")
        make_branch(cls.school)
        cls.tenant = cls.school.tenant
        make_role(cls.tenant, name="Bursar", key="bursar")
        cls.template = publish_template(
            tenant=None, document_type=REQUISITION, code=f"labels-{next(_counter)}",
            name="Requisition approval",
            stages_payload=[
                {"code": "check", "label": "Budget check", "order": 1,
                 "approver_source": "ROLE", "approver_role_key": "bursar"},
                {"code": "senior", "label": "Senior approval", "order": 2,
                 "approver_source": "ROLE", "approver_role_key": "bursar",
                 "inclusion_condition": {"op": "gte", "field": "estimated_total",
                                         "value": 50_000_000}},
            ],
            routes_payload=[
                {"from_stage_code": None, "to_stage_code": "check", "order": 1},
                {"from_stage_code": "check", "to_stage_code": None, "order": 2,
                 "condition": {"op": "lt", "field": "estimated_total", "value": 50_000_000}},
            ],
        )

    def _data(self):
        return WorkflowTemplateReadSerializer(
            self.template, context={"tenant": self.tenant},
        ).data

    def test_a_shared_templates_role_is_named_in_the_readers_tenant(self):
        stages = {s["code"]: s for s in self._data()["stages"]}
        self.assertEqual(stages["check"]["approver_role_name"], "Bursar")

    def test_a_stage_condition_and_a_route_read_in_words(self):
        data = self._data()
        stages = {s["code"]: s for s in data["stages"]}
        self.assertIsNone(stages["check"]["inclusion_condition_description"])
        self.assertEqual(
            stages["senior"]["inclusion_condition_description"], "Amount is at least ₦500,000.00",
        )
        routes = sorted(data["routes"], key=lambda r: r["order"])
        self.assertEqual((routes[0]["from_stage_label"], routes[0]["to_stage_label"]),
                         (None, "Budget check"))
        self.assertEqual(routes[0]["condition_description"], ALWAYS)
        self.assertEqual(routes[1]["condition_description"], "Amount is less than ₦500,000.00")
        self.assertEqual(data["document_type_label"], "Purchase requisition")

    def test_a_stuck_approval_names_the_role_not_its_key(self):
        stage = self.template.stages.get(code="check")
        self.assertEqual(stage_requirement(stage, self.tenant), "assign someone to the Bursar role")
        other = make_school(slug=f"tpl-words-other-{next(_counter)}", name="Greenfield").tenant
        self.assertEqual(
            stage_requirement(stage, other), "assign someone to the role this step approves from",
        )


class TemplateDocumentTypesEndpointTests(TestCase):
    """GET /workflow/templates/document-types/ offers the types by name."""

    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug=f"tpl-types-{next(_counter)}", name="Lagoon View")
        cls.branch = make_branch(cls.school)
        cls.tenant = cls.school.tenant
        cls.viewer = make_school_admin(cls.branch, email=f"types-{next(_counter)}@test.com")
        role = make_role(cls.tenant, name=f"Template viewer {next(_counter)}")
        make_role_permission(role, make_permission(PERM_TEMPLATE_VIEW))
        make_assignment(cls.tenant, cls.viewer, role)
        cls.outsider = make_school_admin(cls.branch, email=f"types-no-{next(_counter)}@test.com")

    def _get(self, user, tenant):
        request = APIRequestFactory().get("/v1/workflow/templates/document-types/")
        request.tenant = tenant
        request.rbac_tenant = tenant
        force_authenticate(request, user=user)
        return DOCUMENT_TYPES(request)

    def test_a_caller_without_template_view_is_refused(self):
        self.assertEqual(self._get(self.outsider, self.tenant).status_code, status.HTTP_403_FORBIDDEN)

    def test_a_school_is_offered_the_types_it_raises_by_name(self):
        response = self._get(self.viewer, self.tenant)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        values = {row["value"] for row in response.data}
        self.assertIn(REQUISITION, values)
        self.assertNotIn("PLATFORM_USER_CREATION", values)
        for row in response.data:
            assert_words(self, row["label"])
        labels = [row["label"] for row in response.data]
        self.assertEqual(labels, sorted(labels))

    def test_the_platform_is_offered_every_type(self):
        codex = codex_tenant()
        from vs_rbac.tests.helpers import make_vision_user

        operator = make_vision_user(email=f"types-op-{next(_counter)}@codex.com")
        role = make_role(codex, name=f"Template viewer {next(_counter)}")
        make_role_permission(role, make_permission(PERM_TEMPLATE_VIEW))
        make_assignment(codex, operator, role)
        values = {row["value"] for row in self._get(operator, codex).data}
        self.assertIn(REQUISITION, values)
        self.assertIn("PLATFORM_USER_CREATION", values)
