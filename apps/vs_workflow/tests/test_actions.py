"""Tests for services/actions.py - record_action, withdraw, cancel, resubmit, reverse_action."""
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
from vs_workflow.handlers.base import BaseWorkflowHandler
from vs_workflow.models import (
    WorkflowAuditLog, WorkflowInstance, WorkflowStage,
    WorkflowStageAction, WorkflowStageApprover, WorkflowStageInstance, WorkflowTemplate,
)
from vs_workflow.services import actions as svc
from vs_workflow.services import routing as routing_service
from vs_workflow.services.approvers import EligibleApprover


def _platform_tenant():
    """The one PLATFORM tenant, seeded by vs_tenants migration 0002.

    Being platform staff IS being on this tenant - there is no persona column
    standing in for it any more - so a fixture that wants a CX account names
    the tenant, exactly as production code does.
    """
    from vs_tenants.models import Tenant

    return Tenant.objects.get(slug="codex", kind=Tenant.Kind.PLATFORM)


def _make_user(email="u@test.com", tenant=None):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.create_user(
        email=email, tenant=tenant or _platform_tenant(),
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
        tenant=requester.tenant,
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
        # the stage - otherwise the stage moves to APPROVED and the second call
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

class _StubHandler(BaseWorkflowHandler):
    """A document handler that records what the engine asked it, and can refuse.

    The engine consults the owning module before a reversal, to ask whether the
    decision can still be undone, and tells it afterwards when the reversal
    changed the outcome. A stub is what lets a test see both halves without
    standing up a real document, and its ``refuse``/``fail_after`` switches are
    the two points at which a module can answer no.
    """

    document_type = "TEST_DOC"

    def __init__(self):
        self.validated = []
        self.reversed_contexts = []
        self.refuse = None  # Raised from validate_reversal when set.
        self.fail_after = None  # Raised from on_action_reversed when set.

    def validate_reversal(self, instance, context) -> None:
        self.validated.append(dict(context))
        if self.refuse is not None:
            raise self.refuse

    def on_action_reversed(self, instance, context) -> None:
        self.reversed_contexts.append(dict(context))
        if self.fail_after is not None:
            raise self.fail_after


class _HandlerStubbed:
    """Mixin that puts a :class:`_StubHandler` behind every engine handler lookup."""

    def _install_handler(self):
        handler = _StubHandler()
        for target in ("vs_workflow.services.actions.get_handler",
                       "vs_workflow.services.routing.get_handler"):
            patcher = patch(target, return_value=handler)
            self.addCleanup(patcher.stop)
            patcher.start()
        return handler


class ReverseActionTests(_HandlerStubbed, _Base):

    def setUp(self):
        super().setUp()
        self.handler = self._install_handler()

    def _cast_action(self, user=None):
        user = user or self.approver
        return WorkflowStageAction.objects.create(
            stage_instance=self.si, actor=user,
            action=ActionEnum.APPROVED, attempt=self.si.attempt,
        )

    def _resolve_stage(self, status=WorkflowStageStatus.APPROVED):
        self.si.status = status
        self.si.resolved_at = timezone.now()
        self.si.save(update_fields=["status", "resolved_at"])

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
        self._resolve_stage()
        original = self._cast_action()
        svc.reverse_action(original.id, self.requester, reason="wrong call")
        self.si.refresh_from_db()
        self.assertEqual(self.si.status, WorkflowStageStatus.ACTIVE)
        self.assertIsNone(self.si.resolved_at)

    def test_the_approver_can_vote_again_once_their_vote_is_reversed(self):
        """Reopening a stage is only worth anything if its approver can act again.

        The uniqueness rule on a stage attempt admits one *live* row per actor,
        so a reversed vote releases its author's slot. A rule that excluded only
        reversal rows would leave the reversed row holding the slot, and the
        second vote would fail on the database instead of being recorded.
        """
        with patch("vs_workflow.services.actions.routing_service"):
            svc.record_action(self.instance.id, self.approver, ActionEnum.APPROVED)
        original = WorkflowStageAction.objects.get(
            stage_instance=self.si, actor=self.approver, is_reversal_of__isnull=True)
        svc.reverse_action(original.id, self.requester, reason="voted in error")
        with patch("vs_workflow.services.actions.routing_service"):
            svc.record_action(self.instance.id, self.approver, ActionEnum.APPROVED)
        self.assertEqual(
            WorkflowStageAction.objects.filter(
                stage_instance=self.si, actor=self.approver,
                is_reversal_of__isnull=True, reversed_at__isnull=True).count(),
            1,
        )

    def test_the_owning_module_is_asked_before_anything_is_written(self):
        self._resolve_stage()
        original = self._cast_action()
        self.handler.refuse = ReversalNotAllowedError("Already sent to the provider.")
        with self.assertRaises(ReversalNotAllowedError):
            svc.reverse_action(original.id, self.requester, reason="too late")
        original.refresh_from_db()
        self.si.refresh_from_db()
        self.assertIsNone(original.reversed_at)
        self.assertEqual(self.si.status, WorkflowStageStatus.APPROVED)
        self.assertFalse(
            WorkflowStageAction.objects.filter(is_reversal_of=original).exists())

    def test_a_module_that_cannot_comply_rolls_the_whole_reversal_back(self):
        """on_action_reversed runs inside the reversal, not after it."""
        self._resolve_stage()
        original = self._cast_action()
        self.handler.fail_after = ReversalNotAllowedError("Document is locked.")
        with self.assertRaises(ReversalNotAllowedError):
            svc.reverse_action(original.id, self.requester, reason="mistake")
        original.refresh_from_db()
        self.si.refresh_from_db()
        self.assertIsNone(original.reversed_at)
        self.assertEqual(self.si.status, WorkflowStageStatus.APPROVED)

    def test_a_vote_the_stage_no_longer_needs_is_voided_without_reopening(self):
        """Two approvals on an ANY stage: reversing one leaves the stage decided."""
        self._resolve_stage()
        second = _make_user("apr2@test.com")
        _make_approver(self.si, second)
        original = self._cast_action()
        self._cast_action(second)
        svc.reverse_action(original.id, self.requester, reason="voted twice over")
        self.si.refresh_from_db()
        self.instance.refresh_from_db()
        self.assertEqual(self.si.status, WorkflowStageStatus.APPROVED)
        self.assertEqual(self.instance.current_stage_id, self.stage.pk)
        # Nothing moved, so the owning module is not asked to move anything.
        self.assertEqual(self.handler.reversed_contexts, [])
        self.assertEqual(len(self.handler.validated), 1)  # It was still consulted.

    def test_a_vote_from_a_superseded_attempt_cannot_be_reversed(self):
        self._resolve_stage(status=WorkflowStageStatus.RETURNED)
        original = self._cast_action()
        _make_stage_instance(self.instance, self.stage, attempt=2)
        with self.assertRaises(ReversalNotAllowedError):
            svc.reverse_action(original.id, self.requester, reason="stale")
        original.refresh_from_db()
        self.assertIsNone(original.reversed_at)

    def test_reversing_a_return_puts_the_instance_back_under_review(self):
        self.instance.status = WorkflowInstanceStatus.RETURNED
        self.instance.save(update_fields=["status"])
        self._resolve_stage(status=WorkflowStageStatus.RETURNED)
        original = WorkflowStageAction.objects.create(
            stage_instance=self.si, actor=self.approver,
            action=ActionEnum.RETURNED, comment="needs work", attempt=self.si.attempt,
        )
        svc.reverse_action(original.id, self.requester, reason="returned in error")
        self.si.refresh_from_db()
        self.instance.refresh_from_db()
        self.assertEqual(self.si.status, WorkflowStageStatus.ACTIVE)
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.IN_PROGRESS)


class ReverseActionUnknownHandlerTests(_Base):
    """Reversal with nobody to ask is refused, unlike cancel and withdraw.

    Those two are the escape hatches that have to work on a stuck instance.
    Reversal declares a recorded outcome untrue, and only the module owning the
    document knows whether that outcome has already been acted on.
    """

    def test_reverse_without_a_registered_handler_raises(self):
        original = WorkflowStageAction.objects.create(
            stage_instance=self.si, actor=self.approver,
            action=ActionEnum.APPROVED, attempt=self.si.attempt,
        )
        with self.assertRaises(ReversalNotAllowedError):
            svc.reverse_action(original.id, self.requester, reason="mistake")
        original.refresh_from_db()
        self.assertIsNone(original.reversed_at)


class ReversalUnwindTests(_HandlerStubbed, TestCase):
    """A reversal on a ladder that has already run past the stage it undoes.

    Three approval stages, one approver each, driven through the real engine so
    the stage rows, the approver snapshots and the votes are the ones production
    writes. What is under test is whether the workflow can still be finished
    after an administrator withdraws the vote the rest of it was built on.
    """

    def setUp(self):
        self.requester = _make_user("req-unwind@test.com")
        self.a1 = _make_user("a1-unwind@test.com")
        self.a2 = _make_user("a2-unwind@test.com")
        self.a3 = _make_user("a3-unwind@test.com")
        self.admin = _make_user("admin-unwind@test.com")
        self.template = _make_template(doc_type="TEST_DOC")
        self.s1 = _make_stage(self.template, code="s1", order=1)
        self.s2 = _make_stage(self.template, code="s2", order=2)
        self.s3 = _make_stage(self.template, code="s3", order=3)
        self.instance = _make_instance(self.template, self.requester)
        self.handler = self._install_handler()

        by_stage = {"s1": self.a1, "s2": self.a2, "s3": self.a3}
        patcher = patch(
            "vs_workflow.services.routing.approvers_service.resolve_approvers",
            side_effect=lambda stage, instance: [
                EligibleApprover(user=by_stage[stage.code], on_behalf_of=None)
            ],
        )
        self.addCleanup(patcher.stop)
        patcher.start()
        routing_service._activate_stage(self.instance, self.s1, 1)
        self.instance.refresh_from_db()

    def _run_the_ladder(self):
        for approver in (self.a1, self.a2, self.a3):
            svc.record_action(self.instance.id, approver, ActionEnum.APPROVED)
        self.instance.refresh_from_db()

    def _live_vote_of(self, actor):
        return WorkflowStageAction.objects.get(
            stage_instance__instance=self.instance, actor=actor,
            is_reversal_of__isnull=True, reversed_at__isnull=True)

    def _stage_row(self, stage):
        return WorkflowStageInstance.objects.get(instance=self.instance, stage=stage)

    def test_reversing_the_first_vote_rolls_the_later_stages_back(self):
        self._run_the_ladder()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.APPROVED)

        svc.reverse_action(self._live_vote_of(self.a1).id, self.admin,
                           reason="the wrong person was asked")

        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.IN_PROGRESS)
        self.assertEqual(self.instance.current_stage_id, self.s1.pk)
        self.assertEqual(self._stage_row(self.s1).status, WorkflowStageStatus.ACTIVE)
        for stage in (self.s2, self.s3):
            self.assertEqual(self._stage_row(stage).status, WorkflowStageStatus.PENDING)
        # No vote anywhere on the instance still counts.
        self.assertFalse(
            WorkflowStageAction.objects.filter(
                stage_instance__instance=self.instance,
                is_reversal_of__isnull=True, reversed_at__isnull=True).exists())

    def test_the_ladder_can_be_run_to_the_end_again_after_a_reversal(self):
        """The deadlock this unwind exists to prevent.

        Leaving the later stages APPROVED with their votes live means the engine
        re-activates a stage whose approver is then refused as a duplicate, and
        the instance sits ACTIVE with nobody able to move it.
        """
        self._run_the_ladder()
        svc.reverse_action(self._live_vote_of(self.a1).id, self.admin,
                           reason="the wrong person was asked")
        self._run_the_ladder()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.APPROVED)

    def test_a_reopened_stage_does_not_double_its_approver_snapshot(self):
        self._run_the_ladder()
        svc.reverse_action(self._live_vote_of(self.a1).id, self.admin,
                           reason="the wrong person was asked")
        self._run_the_ladder()
        for stage in (self.s1, self.s2, self.s3):
            self.assertEqual(
                WorkflowStageApprover.objects.filter(
                    stage_instance=self._stage_row(stage), attempt=1).count(),
                1, f"{stage.code} snapshot was written twice",
            )

    def test_reversing_a_middle_vote_leaves_the_stage_before_it_alone(self):
        self._run_the_ladder()
        svc.reverse_action(self._live_vote_of(self.a2).id, self.admin,
                           reason="second checker was out of scope")
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.current_stage_id, self.s2.pk)
        self.assertEqual(self._stage_row(self.s1).status, WorkflowStageStatus.APPROVED)
        self.assertEqual(self._stage_row(self.s2).status, WorkflowStageStatus.ACTIVE)
        self.assertEqual(self._stage_row(self.s3).status, WorkflowStageStatus.PENDING)
        # The first stage's vote is untouched; only what followed it is undone.
        self.assertIsNone(self._live_vote_of(self.a1).reversed_at)

    def test_a_skipped_stage_is_rolled_back_with_the_ones_that_voted(self):
        """A stage the engine walked past is still a stage it walked past.

        Retiring the middle stage means the ladder runs s1 then s3, with s2
        recorded as skipped in between. Reversing s1 has to take both of them
        back, or s2 keeps a resolution that belongs to a run that no longer
        happened, and the second pass reads a decision nobody made this time.
        """
        self.s2.retired_at = timezone.now()
        self.s2.save(update_fields=["retired_at"])

        svc.record_action(self.instance.id, self.a1, ActionEnum.APPROVED)
        svc.record_action(self.instance.id, self.a3, ActionEnum.APPROVED)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertEqual(self._stage_row(self.s2).status, WorkflowStageStatus.SKIPPED)

        svc.reverse_action(self._live_vote_of(self.a1).id, self.admin,
                           reason="the wrong person was asked")

        self.assertEqual(self._stage_row(self.s2).status, WorkflowStageStatus.PENDING)
        self.assertEqual(self._stage_row(self.s3).status, WorkflowStageStatus.PENDING)
        self.assertEqual(self.handler.reversed_contexts[-1]["unwound_stages"],
                         ["s2", "s3"])

        # And the ladder still reaches the end, skipping s2 exactly as before.
        svc.record_action(self.instance.id, self.a1, ActionEnum.APPROVED)
        svc.record_action(self.instance.id, self.a3, ActionEnum.APPROVED)
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.status, WorkflowInstanceStatus.APPROVED)
        self.assertEqual(self._stage_row(self.s2).status, WorkflowStageStatus.SKIPPED)

    def test_the_owning_module_is_told_what_the_engine_undid(self):
        self._run_the_ladder()
        svc.reverse_action(self._live_vote_of(self.a1).id, self.admin,
                           reason="the wrong person was asked")
        told = self.handler.reversed_contexts[-1]
        self.assertEqual(told["reopened_stage_code"], "s1")
        self.assertEqual(told["unwound_stages"], ["s2", "s3"])
        self.assertTrue(told["was_final_approval"])

    def test_the_audit_log_names_the_stages_the_reversal_rolled_back(self):
        self._run_the_ladder()
        svc.reverse_action(self._live_vote_of(self.a1).id, self.admin,
                           reason="the wrong person was asked")
        entry = WorkflowAuditLog.objects.filter(
            instance=self.instance, event_type="ACTION_REVERSED").latest("occurred_at")
        self.assertEqual(entry.context["reopened_stage"], "s1")
        self.assertEqual(entry.context["unwound_stages"], ["s2", "s3"])


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
