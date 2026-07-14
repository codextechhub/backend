"""Tests for routing, approvers, and templates services."""
from unittest.mock import MagicMock, patch
from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from vs_workflow.constants import (
    StageAdvanceRule, StageKind, StageOnRejection,
    WorkflowInstanceStatus, WorkflowStageStatus,
)
from vs_workflow.exceptions import TemplateInvalidError
from vs_workflow.models import (
    WorkflowInstance, WorkflowStage, WorkflowStageApprover,
    WorkflowStageInstance, WorkflowTemplate,
)
from vs_workflow.services import routing as routing_svc
from vs_workflow.services import templates as templates_svc
from vs_workflow.services.approvers import EligibleApprover, resolve_approvers


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(email="u@test.com"):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        email=email, user_type="CX_STAFF",
        first_name="Test", last_name="User",
    )


def _make_template(doc_type="ROUTE_DOC", code="default"):
    return WorkflowTemplate.objects.create(
        document_type=doc_type, code=code, name="Route Test Template",
    )


def _make_stage(template, code="s1", order=1, kind="APPROVAL",
                advance_rule="ANY", skip_if_no_approvers=False, on_rejection="TERMINAL"):
    return WorkflowStage.objects.create(
        template=template, code=code, label=code,
        kind=kind, order=order,
        advance_rule=advance_rule,
        on_rejection=on_rejection,
        skip_if_no_approvers=skip_if_no_approvers,
    )


def _make_instance(template, user, stage=None,
                   status=WorkflowInstanceStatus.IN_PROGRESS):
    ct = ContentType.objects.get_for_model(WorkflowTemplate)
    return WorkflowInstance.objects.create(
        tenant=user.tenant,
        template=template,
        document_content_type=ct,
        document_object_id="fdoc",
        document_type=template.document_type,
        status=status,
        requested_by=user,
        current_stage=stage,
        submitted_at=timezone.now(),
    )


# ── Routing ───────────────────────────────────────────────────────────────────

class AdvanceInstanceTests(TestCase):

    def setUp(self):
        self.user = _make_user()
        self.template = _make_template()

    def test_terminal_instance_is_returned_unchanged(self):
        instance = _make_instance(
            self.template, self.user,
            status=WorkflowInstanceStatus.APPROVED,
        )
        result = routing_svc.advance_instance(instance, current_attempt=1)
        self.assertEqual(result.status, WorkflowInstanceStatus.APPROVED)

    def test_no_stages_raises_template_invalid(self):
        instance = _make_instance(self.template, self.user)
        with self.assertRaises(TemplateInvalidError):
            routing_svc.advance_instance(instance, current_attempt=1)

    def test_single_stage_activated_on_first_advance(self):
        stage = _make_stage(self.template)
        instance = _make_instance(self.template, self.user)
        with patch("vs_workflow.services.routing.approvers_service.resolve_approvers",
                   return_value=[]):
            routing_svc.advance_instance(instance, current_attempt=1)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(instance.current_stage, stage)
        self.assertTrue(
            WorkflowStageInstance.objects.filter(
                instance=instance, stage=stage, status=WorkflowStageStatus.ACTIVE,
            ).exists()
        )

    def test_stage_skipped_when_no_approvers_and_skip_flag_set(self):
        _make_stage(self.template, skip_if_no_approvers=True)
        instance = _make_instance(self.template, self.user)
        with patch("vs_workflow.services.routing.approvers_service.resolve_approvers",
                   return_value=[]):
            # No more stages after the skipped one — should terminate APPROVED.
            with patch("vs_workflow.services.routing.get_handler") as mock_handler:
                mock_handler.return_value.on_approved.return_value = None
                routing_svc.advance_instance(instance, current_attempt=1)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)

    def test_retired_stage_is_skipped(self):
        stage = _make_stage(self.template)
        stage.retired_at = timezone.now()
        stage.save(update_fields=["retired_at"])
        instance = _make_instance(self.template, self.user)
        with patch("vs_workflow.services.routing.get_handler") as mock_handler:
            mock_handler.return_value.on_approved.return_value = None
            routing_svc.advance_instance(instance, current_attempt=1)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertTrue(
            WorkflowStageInstance.objects.filter(
                instance=instance, stage=stage, status=WorkflowStageStatus.SKIPPED,
            ).exists()
        )

    def test_two_stages_advances_to_second(self):
        s1 = _make_stage(self.template, code="s1", order=1)
        s2 = _make_stage(self.template, code="s2", order=2)
        instance = _make_instance(self.template, self.user, stage=s1)
        with patch("vs_workflow.services.routing.approvers_service.resolve_approvers",
                   return_value=[]):
            routing_svc.advance_instance(instance, current_attempt=1)
        instance.refresh_from_db()
        self.assertEqual(instance.current_stage, s2)

    def test_all_stages_complete_terminates_approved(self):
        _make_stage(self.template)
        instance = _make_instance(self.template, self.user)
        # Simulate advancing past the only stage (current_stage already set).
        instance.current_stage = _make_stage(self.template, code="last", order=99)
        instance.save(update_fields=["current_stage"])
        with patch("vs_workflow.services.routing.get_handler") as mock_handler:
            mock_handler.return_value.on_approved.return_value = None
            routing_svc.advance_instance(instance, current_attempt=1)
        instance.refresh_from_db()
        self.assertEqual(instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertIsNone(instance.current_stage)
        self.assertIsNotNone(instance.completed_at)


# ── Approvers ─────────────────────────────────────────────────────────────────

class ResolveApproversTests(TestCase):

    def setUp(self):
        self.requester = _make_user("req@test.com")
        self.template  = _make_template()
        ct = ContentType.objects.get_for_model(WorkflowTemplate)
        self.instance = WorkflowInstance.objects.create(
            tenant=self.requester.tenant,
            template=self.template,
            document_content_type=ct,
            document_object_id="doc1",
            document_type="ROUTE_DOC",
            status=WorkflowInstanceStatus.IN_PROGRESS,
            requested_by=self.requester,
            submitted_at=timezone.now(),
        )

    def test_no_permission_key_returns_empty(self):
        stage = _make_stage(self.template)
        stage.approver_permission_key = ""
        stage.save(update_fields=["approver_permission_key"])
        result = resolve_approvers(stage, self.instance)
        self.assertEqual(result, [])

    def test_requester_excluded_from_approvers(self):
        stage = _make_stage(self.template)
        stage.approver_permission_key = "workflow.instance.submit"
        stage.approver_scope = "PLATFORM"
        stage.save(update_fields=["approver_permission_key", "approver_scope"])
        mock_qs = MagicMock()
        # .exclude() returns a mock whose .distinct() returns empty list
        mock_qs.exclude.return_value.distinct.return_value = []
        with patch("vs_workflow.services.approvers._users_with_permission",
                   return_value=mock_qs), \
             patch("vs_workflow.services.approvers.ApprovalDelegation.objects") as mock_del:
            mock_del.filter.return_value.filter.return_value.exclude.return_value\
                .select_related.return_value = []
            result = resolve_approvers(stage, self.instance)
        self.assertEqual(result, [])

    def test_eligible_approver_included(self):
        approver = _make_user("aprv@test.com")
        stage = _make_stage(self.template)
        stage.approver_permission_key = "workflow.instance.submit"
        stage.approver_scope = "PLATFORM"
        stage.save(update_fields=["approver_permission_key", "approver_scope"])

        mock_qs = MagicMock()
        mock_qs.exclude.return_value = mock_qs
        mock_qs.distinct.return_value = [approver]

        with patch("vs_workflow.services.approvers._users_with_permission",
                   return_value=mock_qs), \
             patch("vs_workflow.services.approvers.ApprovalDelegation.objects") as mock_del:
            mock_del.filter.return_value.filter.return_value.exclude.return_value\
                .select_related.return_value = []
            result = resolve_approvers(stage, self.instance)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].user, approver)
        self.assertIsNone(result[0].on_behalf_of)

    def test_exclusive_delegation_removes_delegator(self):
        """When a delegation is exclusive the delegator must not appear alongside the delegate."""
        delegator  = _make_user("delgtr@test.com")
        delegate   = _make_user("delgte@test.com")
        stage = _make_stage(self.template)
        stage.approver_permission_key = "workflow.instance.submit"
        stage.approver_scope = "PLATFORM"
        stage.save(update_fields=["approver_permission_key", "approver_scope"])

        mock_qs = MagicMock()
        mock_qs.exclude.return_value = mock_qs
        mock_qs.distinct.return_value = [delegator]

        mock_delegation = MagicMock()
        mock_delegation.delegator_id  = delegator.pk
        mock_delegation.delegate_id   = delegate.pk
        mock_delegation.delegator     = delegator
        mock_delegation.delegate      = delegate
        mock_delegation.exclusive     = True

        with patch("vs_workflow.services.approvers._users_with_permission",
                   return_value=mock_qs), \
             patch("vs_workflow.services.approvers.ApprovalDelegation.objects") as mock_del:
            mock_del.filter.return_value.filter.return_value.exclude.return_value\
                .select_related.return_value = [mock_delegation]
            result = resolve_approvers(stage, self.instance)

        user_ids = [r.user.pk for r in result]
        self.assertIn(delegate.pk, user_ids)
        self.assertNotIn(delegator.pk, user_ids)


# ── EligibleApprover dataclass ────────────────────────────────────────────────

class EligibleApproverTests(SimpleTestCase):

    def test_on_behalf_of_defaults_to_none(self):
        user = MagicMock()
        ea = EligibleApprover(user=user)
        self.assertIsNone(ea.on_behalf_of)

    def test_fields_stored_correctly(self):
        user      = MagicMock()
        delegator = MagicMock()
        ea = EligibleApprover(user=user, on_behalf_of=delegator)
        self.assertEqual(ea.user, user)
        self.assertEqual(ea.on_behalf_of, delegator)


# ── publish_template ──────────────────────────────────────────────────────────

class PublishTemplateTests(TestCase):

    def test_create_new_template(self):
        t = templates_svc.publish_template(
            tenant=None, document_type="TPL_TEST", code="default",
            name="Test", stages_payload=[
                {"code": "s1", "label": "Step 1", "kind": "APPROVAL", "order": 1},
            ],
        )
        self.assertEqual(WorkflowTemplate.objects.filter(
            document_type="TPL_TEST", code="default").count(), 1)
        self.assertEqual(t.stages.count(), 1)

    def test_republish_updates_fields_in_place(self):
        templates_svc.publish_template(
            tenant=None, document_type="TPL_UPD", code="default",
            name="Original", stages_payload=[
                {"code": "s1", "label": "Step 1", "kind": "APPROVAL", "order": 1},
            ],
        )
        t = templates_svc.publish_template(
            tenant=None, document_type="TPL_UPD", code="default",
            name="Updated", stages_payload=[
                {"code": "s1", "label": "Step 1 updated", "kind": "APPROVAL", "order": 1},
            ],
        )
        self.assertEqual(t.name, "Updated")
        self.assertEqual(t.stages.count(), 1)
        self.assertEqual(WorkflowTemplate.objects.filter(
            document_type="TPL_UPD").count(), 1)

    def test_removed_stage_is_soft_retired(self):
        """A stage absent from a republish payload must be retired, not deleted."""
        templates_svc.publish_template(
            tenant=None, document_type="TPL_RET", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "Step 1", "kind": "APPROVAL", "order": 1},
                {"code": "s2", "label": "Step 2", "kind": "APPROVAL", "order": 2},
            ],
        )
        templates_svc.publish_template(
            tenant=None, document_type="TPL_RET", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "Step 1", "kind": "APPROVAL", "order": 1},
            ],
        )
        s2 = WorkflowStage.objects.get(
            template__document_type="TPL_RET", code="s2")
        self.assertIsNotNone(s2.retired_at)

    def test_republishing_retired_stage_reactivates_it(self):
        """Including a previously retired stage code in the payload un-retires it."""
        templates_svc.publish_template(
            tenant=None, document_type="TPL_UNRET", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "Step 1", "kind": "APPROVAL", "order": 1},
            ],
        )
        # Remove s1.
        templates_svc.publish_template(
            tenant=None, document_type="TPL_UNRET", code="default",
            name="T", stages_payload=[],
        )
        # Re-include s1.
        templates_svc.publish_template(
            tenant=None, document_type="TPL_UNRET", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "Step 1 back", "kind": "APPROVAL", "order": 1},
            ],
        )
        s1 = WorkflowStage.objects.get(
            template__document_type="TPL_UNRET", code="s1")
        self.assertIsNone(s1.retired_at)

    def test_routes_replaced_entirely_on_republish(self):
        """Routes have no instance-level references so they are fully replaced."""
        from vs_workflow.models import WorkflowRoutePath
        templates_svc.publish_template(
            tenant=None, document_type="TPL_RT", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "S1", "kind": "APPROVAL", "order": 1},
                {"code": "s2", "label": "S2", "kind": "APPROVAL", "order": 2},
            ],
            routes_payload=[
                {"from_stage_code": "s1", "to_stage_code": "s2", "order": 1},
            ],
        )
        templates_svc.publish_template(
            tenant=None, document_type="TPL_RT", code="default",
            name="T", stages_payload=[
                {"code": "s1", "label": "S1", "kind": "APPROVAL", "order": 1},
                {"code": "s2", "label": "S2", "kind": "APPROVAL", "order": 2},
            ],
            routes_payload=[],  # intentionally cleared
        )
        tpl = WorkflowTemplate.objects.get(document_type="TPL_RT")
        self.assertEqual(WorkflowRoutePath.objects.filter(template=tpl).count(), 0)
