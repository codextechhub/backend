"""Tests for services/actions.py — record_action, withdraw, cancel, resubmit, reverse_action."""
import logging
from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from vs_workflow.constants import (
    StageAdvanceRule, StageKind, StageOnRejection,
    WorkflowInstanceStatus, WorkflowStageStatus,
    WorkflowStageAction as ActionEnum,
)
from vs_workflow.exceptions import (
    CancellationNotAllowedError, DuplicateApproverActionError,
    InstanceTerminalError, InvalidInstanceStateError,
    NotAnEligibleApproverError, RequesterCannotApproveError,
    ReversalNotAllowedError,
)
from vs_workflow.models import (
    WorkflowAuditLog, WorkflowInstance, WorkflowStage,
    WorkflowStageAction, WorkflowStageApprover, WorkflowStageInstance, WorkflowTemplate,
)
from vs_workflow.services import actions as svc


def _make_user(email="u@test.com", user_type="CX_STAFF"):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.create_user(
        email=email, user_type=user_type,
        first_name="Test", last_name="User",
    )


def _make_template(doc_type="TEST_DOC", code="default"):
    return WorkflowTemplate.objects.create(
        document_type=doc_type, code=code, name="Test Template",
    )


def _make_stage(template, code="s1", advance_rule="ANY",
                on_rejection="TERMINAL", order=1):
    return WorkflowStage.objects.create(
        template=template, code=code, label=code.upper(),
        kind=StageKind.APPROVAL, order=order,
        advance_rule=advance_rule,
        on_rejection=on_rejection,
        skip_if_no_approvers=False,
    )


def _make_instance(template, requester, stage=None,
                   status=WorkflowInstanceStatus.IN_PROGRESS):
    ct = ContentType.objects.get_for_model(WorkflowTemplate)
    return WorkflowInstance.objects.create(
        template=template,
        document_content_type=ct,
        document_object_id="fake-doc-id",
        document_type=template.document_type,
        status=status,
        requested_by=requester,
        current_stage=stage,
        submitted_at=timezone.now(),
    )


def _make_stage_instance(instance, stage, status=WorkflowStageStatus.ACTIVE, attempt=1):
    return WorkflowStageInstance.objects.create(
        instance=instance, stage=stage,
        status=status, attempt=attempt,
        activated_at=timezone.now(),
    )


def _make_approver(stage_instance, user, attempt=1):
    return WorkflowStageApprover.objects.create(
        stage_instance=stage_instance, user=user, attempt=attempt,
    )


# ── Base fixture mixin ────────────────────────────────────────────────────────

class _Base(TestCase):
    def setUp(self):
        self.requester = _make_user("req@test.com")
        self.approver  = _make_user("apr@test.com")
        self.template  = _make_template()
        self.stage     = _make_stage(self.template)
        self.instance  = _make_instance(self.template, self.requester, self.stage)
        self.si        = _make_stage_instance(self.instance, self.stage)
        self.snap      = _make_approver(self.si, self.approver)


# ── record_action ──────────────────────────────────────────────────────────────

class RecordActionTests(_Base):

    def test_approved_creates_action_row(self):
        with patch("vs_workflow.services.actions.routing_service"):
            svc.record_action(self.instance.id, self.approver, ActionEnum.APPROVED)
        self.assertTrue(
            WorkflowStageAction.objects.filter(
                stage_instance=self.si, actor=self.approver,
                action=ActionEnum.APPROVED,
            ).exists()
        )

    def test_approved_writes_audit_log(self):
        with patch("vs_workflow.services.actions.routing_service"):
            svc.record_action(self.instance.id, self.approver, ActionEnum.APPROVED)
        self.assertTrue(
            WorkflowAuditLog.objects.filter(instance=self.instance).exists()
        )

    def test_rejected_requires_comment(self):
        with self.assertRaises(InvalidInstanceStateError):
            svc.record_action(self.instance.id, self.approver,
                              ActionEnum.REJECTED, comment="")

    def test_returned_requires_comment(self):
        with self.assertRaises(InvalidInstanceStateError):
            svc.record_action(self.instance.id, self.approver,
                              ActionEnum.RETURNED, comment="")

    def test_requester_cannot_approve_own_document(self):
        with self.assertRaises(RequesterCannotApproveError):
            svc.record_action(self.instance.id, self.requester, ActionEnum.APPROVED)

    def test_non_eligible_user_raises(self):
        stranger = _make_user("stranger@test.com")
        with self.assertRaises(NotAnEligibleApproverError):
            svc.record_action(self.instance.id, stranger, ActionEnum.APPROVED)

    def test_duplicate_vote_raises(self):
        # Use UNANIMOUS + a second approver so the first vote doesn't resolve
        # the stage — otherwise the stage moves to APPROVED and the second call
        # raises StageNotActiveError instead of DuplicateApproverActionError.
        self.stage.advance_rule = StageAdvanceRule.UNANIMOUS
        self.stage.save(update_fields=["advance_rule"])
        second = _make_user("second@test.com")
        _make_approver(self.si, second)

        svc.record_action(self.instance.id, self.approver, ActionEnum.APPROVED)
        with self.assertRaises(DuplicateApproverActionError):
            svc.record_action(self.instance.id, self.approver, ActionEnum.APPROVED)

    def test_terminal_instance_raises(self):
        self.instance.status = WorkflowInstanceStatus.APPROVED
        self.instance.save(update_fields=["status"])
        with self.assertRaises(InstanceTerminalError):
            svc.record_action(self.instance.id, self.approver, ActionEnum.APPROVED)

    def test_rejected_with_terminal_on_rejection_terminates(self):
        with patch("vs_workflow.services.actions.routing_service.advance_instance"), \
             patch("vs_workflow.services.actions.routing_service._terminate_rejected") as mock_term, \
             patch("vs_workflow.services.actions.routing_service._return_to_requester"):
            mock_term.return_value = self.instance
            svc.record_action(self.instance.id, self.approver,
                              ActionEnum.REJECTED, comment="bad")
        mock_term.assert_called_once()

    def test_returned_calls_return_to_requester(self):
        with patch("vs_workflow.services.actions.routing_service._return_to_requester") as mock_ret, \
             patch("vs_workflow.services.actions.routing_service._terminate_rejected"):
            mock_ret.return_value = self.instance
            svc.record_action(self.instance.id, self.approver,
                              ActionEnum.RETURNED, comment="needs revision")
        mock_ret.assert_called_once()


# ── _stage_fully_approved (advance rules) ─────────────────────────────────────

class StageFullyApprovedTests(_Base):

    def _approve(self, user, si=None):
        si = si or self.si
        WorkflowStageAction.objects.create(
            stage_instance=si, actor=user, action=ActionEnum.APPROVED,
            attempt=si.attempt,
        )

    def test_any_rule_one_approval_is_enough(self):
        self.stage.advance_rule = StageAdvanceRule.ANY
        self.stage.save(update_fields=["advance_rule"])
        self._approve(self.approver)
        self.assertTrue(svc._stage_fully_approved(self.si))

    def test_unanimous_rule_requires_all_approvers(self):
        self.stage.advance_rule = StageAdvanceRule.UNANIMOUS
        self.stage.save(update_fields=["advance_rule"])
        extra = _make_user("extra@test.com")
        _make_approver(self.si, extra)
        # Only one of two approved.
        self._approve(self.approver)
        self.assertFalse(svc._stage_fully_approved(self.si))
        # Both approved.
        self._approve(extra)
        self.assertTrue(svc._stage_fully_approved(self.si))

    def test_quorum_rule(self):
        self.stage.advance_rule = StageAdvanceRule.QUORUM
        self.stage.quorum_count = 2
        self.stage.save(update_fields=["advance_rule", "quorum_count"])
        extra = _make_user("extra2@test.com")
        _make_approver(self.si, extra)
        self._approve(self.approver)
        self.assertFalse(svc._stage_fully_approved(self.si))
        self._approve(extra)
        self.assertTrue(svc._stage_fully_approved(self.si))

    def test_reversed_vote_does_not_count(self):
        """A reversed APPROVED action must not contribute to the threshold."""
        self.stage.advance_rule = StageAdvanceRule.ANY
        self.stage.save(update_fields=["advance_rule"])
        action = WorkflowStageAction.objects.create(
            stage_instance=self.si, actor=self.approver,
            action=ActionEnum.APPROVED, attempt=self.si.attempt,
            reversed_at=timezone.now(),  # already reversed
        )
        self.assertFalse(svc._stage_fully_approved(self.si))


# ── withdraw ──────────────────────────────────────────────────────────────────

class WithdrawTests(_Base):

    def test_withdraw_sets_withdrawn(self):
        svc.withdraw(self.instance.id, self.requester)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.WITHDRAWN)
        self.assertIsNone(self.instance.current_stage)
        self.assertIsNotNone(self.instance.completed_at)

    def test_withdraw_by_non_requester_raises(self):
        with self.assertRaises(InvalidInstanceStateError):
            svc.withdraw(self.instance.id, self.approver)

    def test_withdraw_terminal_instance_raises(self):
        self.instance.status = WorkflowInstanceStatus.APPROVED
        self.instance.save(update_fields=["status"])
        with self.assertRaises(InstanceTerminalError):
            svc.withdraw(self.instance.id, self.requester)

    def test_withdraw_writes_audit_log(self):
        svc.withdraw(self.instance.id, self.requester)
        self.assertTrue(
            WorkflowAuditLog.objects.filter(
                instance=self.instance, event_type="INSTANCE_WITHDRAWN",
            ).exists()
        )

    def test_withdraw_missing_handler_does_not_raise(self):
        """_run_handler_callback must not block withdraw when handler is unregistered."""
        self.instance.document_type = "NO.HANDLER.REGISTERED"
        self.instance.save(update_fields=["document_type"])
        with self.assertLogs("vs_workflow.services.actions", level=logging.WARNING):
            svc.withdraw(self.instance.id, self.requester)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.WITHDRAWN)


# ── cancel ────────────────────────────────────────────────────────────────────

class CancelTests(_Base):

    def test_cancel_sets_cancelled(self):
        svc.cancel(self.instance.id, self.approver, reason="Admin override")
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.CANCELLED)
        self.assertIsNone(self.instance.current_stage)
        self.assertIsNotNone(self.instance.completed_at)

    def test_cancel_no_reason_raises(self):
        with self.assertRaises(CancellationNotAllowedError):
            svc.cancel(self.instance.id, self.approver, reason="")

    def test_cancel_terminal_raises(self):
        self.instance.status = WorkflowInstanceStatus.CANCELLED
        self.instance.save(update_fields=["status"])
        with self.assertRaises(InstanceTerminalError):
            svc.cancel(self.instance.id, self.approver, reason="again")

    def test_cancel_missing_handler_does_not_raise(self):
        """_run_handler_callback must not block cancel when handler is unregistered."""
        self.instance.document_type = "NO.HANDLER.CANCEL"
        self.instance.save(update_fields=["document_type"])
        with self.assertLogs("vs_workflow.services.actions", level=logging.WARNING):
            svc.cancel(self.instance.id, self.approver, reason="cleanup")
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.CANCELLED)


# ── reverse_action ────────────────────────────────────────────────────────────

class ReverseActionTests(_Base):

    def _cast_action(self, user=None):
        user = user or self.approver
        return WorkflowStageAction.objects.create(
            stage_instance=self.si, actor=user,
            action=ActionEnum.APPROVED, attempt=self.si.attempt,
        )

    def test_reverse_marks_original_and_creates_reversal(self):
        original = self._cast_action()
        reversal = svc.reverse_action(original.id, self.requester, reason="mistake")
        original.refresh_from_db()
        self.assertIsNotNone(original.reversed_at)
        self.assertEqual(original.reversed_by, self.requester)
        self.assertEqual(reversal.is_reversal_of, original)

    def test_reverse_already_reversed_raises(self):
        original = self._cast_action()
        original.reversed_at = timezone.now()
        original.save(update_fields=["reversed_at"])
        with self.assertRaises(ReversalNotAllowedError):
            svc.reverse_action(original.id, self.requester, reason="again")

    def test_reverse_a_reversal_row_raises(self):
        original = self._cast_action()
        reversal = WorkflowStageAction.objects.create(
            stage_instance=self.si, actor=self.requester,
            action=ActionEnum.APPROVED, attempt=self.si.attempt,
            is_reversal_of=original,
        )
        with self.assertRaises(ReversalNotAllowedError):
            svc.reverse_action(reversal.id, self.requester, reason="nope")

    def test_reverse_without_reason_raises(self):
        original = self._cast_action()
        with self.assertRaises(ReversalNotAllowedError):
            svc.reverse_action(original.id, self.requester, reason="")

    def test_reverse_reactivates_approved_stage(self):
        """When the stage resolved because of this vote, it must be re-opened."""
        self.si.status = WorkflowStageStatus.APPROVED
        self.si.resolved_at = timezone.now()
        self.si.save(update_fields=["status", "resolved_at"])
        original = self._cast_action()
        svc.reverse_action(original.id, self.requester, reason="wrong call")
        self.si.refresh_from_db()
        self.assertEqual(self.si.status, WorkflowStageStatus.ACTIVE)
        self.assertIsNone(self.si.resolved_at)


# ── resubmit ──────────────────────────────────────────────────────────────────

class ResubmitTests(_Base):

    def setUp(self):
        super().setUp()
        self.instance.status = WorkflowInstanceStatus.RETURNED
        self.instance.save(update_fields=["status"])

    def test_resubmit_restores_in_progress(self):
        with patch("vs_workflow.services.actions.routing_service._activate_stage"), \
             patch("vs_workflow.services.actions.approvers_service.resolve_approvers",
                   return_value=[]):
            svc.resubmit(self.instance.id, self.requester)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.IN_PROGRESS)

    def test_resubmit_non_returned_raises(self):
        self.instance.status = WorkflowInstanceStatus.IN_PROGRESS
        self.instance.save(update_fields=["status"])
        with self.assertRaises(InvalidInstanceStateError):
            svc.resubmit(self.instance.id, self.requester)

    def test_resubmit_by_non_requester_raises(self):
        with self.assertRaises(InvalidInstanceStateError):
            svc.resubmit(self.instance.id, self.approver)

    def test_resubmit_increments_attempt(self):
        """Next attempt must be one higher than the latest stage instance attempt."""
        with patch("vs_workflow.services.actions.routing_service._activate_stage") as mock_act, \
             patch("vs_workflow.services.actions.approvers_service.resolve_approvers",
                   return_value=[]):
            svc.resubmit(self.instance.id, self.requester)
        mock_act.assert_called_once()
        _, _stage_arg, attempt_arg = mock_act.call_args[0]
        self.assertEqual(attempt_arg, self.si.attempt + 1)
