"""Integration tests for workflow lifecycle notification emissions.

routing.py enqueues dispatch_notification via transaction.on_commit, so every
test drives the transition inside captureOnCommitCallbacks(execute=True);
Celery runs eagerly in tests, which makes the whole pipeline synchronous:
transition → on_commit → dispatch task → NotificationService → feed rows.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from vs_workflow.constants import (
    StageKind, WorkflowInstanceStatus, WorkflowStageStatus,
    WorkflowStageAction as ActionEnum,
)
from vs_workflow.models import (
    WorkflowInstance, WorkflowStage, WorkflowStageApprover,
    WorkflowStageInstance, WorkflowTemplate,
)
from vs_workflow.services import actions as actions_service
from vs_workflow.services import routing as routing_service


def _user(email):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        email=email, user_type="CX_STAFF", first_name="Test", last_name="User",
    )


class WorkflowNotificationTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        # Real event types + default templates so dispatch has something to render.
        from vs_notifications.services.seed import (
            seed_event_types, seed_notification_templates,
        )
        seed_event_types()
        seed_notification_templates()

        cls.requester = _user("requester@test.com")
        cls.approver = _user("approver@test.com")
        cls.template = WorkflowTemplate.objects.create(
            document_type="TEST_DOC", code="default", name="Test Template",
        )
        cls.stage = WorkflowStage.objects.create(
            template=cls.template, code="s1", label="S1",
            kind=StageKind.APPROVAL, order=1,
            advance_rule="ANY", on_rejection="TERMINAL",
            skip_if_no_approvers=False,
        )

    def _instance(self, stage=None, status=WorkflowInstanceStatus.IN_PROGRESS):
        ct = ContentType.objects.get_for_model(WorkflowTemplate)
        return WorkflowInstance.objects.create(
            template=self.template,
            document_content_type=ct,
            document_object_id="fake-doc-id",
            document_type=self.template.document_type,
            status=status,
            requested_by=self.requester,
            current_stage=stage,
            submitted_at=timezone.now(),
        )

    def _feed_rows(self, user, event_key):
        from vs_notifications.models import Notification
        return Notification.objects.filter(
            recipient=user, channel="in_app", event_type__key=event_key,
        )

    def test_stage_activation_notifies_approvers(self):
        """Activating a stage creates an in-app row for each eligible approver."""
        instance = self._instance()
        eligible = [SimpleNamespace(user=self.approver, on_behalf_of=None)]
        with patch.object(routing_service.approvers_service, "resolve_approvers",
                          return_value=eligible):
            with self.captureOnCommitCallbacks(execute=True):
                routing_service._activate_stage(instance, self.stage, attempt=1)

        rows = self._feed_rows(self.approver, "workflow.stage_activated")
        self.assertEqual(rows.count(), 1)
        self.assertIn("awaiting your decision", rows.first().body)

    def test_returned_notifies_requester(self):
        """A RETURNED vote notifies the requester with the comment."""
        instance = self._instance(stage=self.stage)
        si = WorkflowStageInstance.objects.create(
            instance=instance, stage=self.stage,
            status=WorkflowStageStatus.ACTIVE, attempt=1,
            activated_at=timezone.now(),
        )
        WorkflowStageApprover.objects.create(
            stage_instance=si, user=self.approver, attempt=1,
        )
        # _return_to_requester fires the document handler's on_returned; TEST_DOC
        # has no registered handler, so stub it like the other terminal paths.
        with patch.object(routing_service, "get_handler", return_value=MagicMock()):
            with self.captureOnCommitCallbacks(execute=True):
                actions_service.record_action(
                    instance.id, self.approver, ActionEnum.RETURNED, comment="fix totals",
                )

        self.assertEqual(
            self._feed_rows(self.requester, "workflow.returned").count(), 1,
        )

    def test_terminal_rejection_notifies_requester(self):
        """A terminal REJECTED vote notifies the requester."""
        instance = self._instance(stage=self.stage)
        si = WorkflowStageInstance.objects.create(
            instance=instance, stage=self.stage,
            status=WorkflowStageStatus.ACTIVE, attempt=1,
            activated_at=timezone.now(),
        )
        WorkflowStageApprover.objects.create(
            stage_instance=si, user=self.approver, attempt=1,
        )
        # on_rejection=TERMINAL routes through _terminate_rejected, whose
        # document handler isn't registered for TEST_DOC — stub it out.
        with patch.object(routing_service, "get_handler", return_value=MagicMock()):
            with self.captureOnCommitCallbacks(execute=True):
                actions_service.record_action(
                    instance.id, self.approver, ActionEnum.REJECTED, comment="no budget",
                )

        self.assertEqual(
            self._feed_rows(self.requester, "workflow.rejected").count(), 1,
        )

    def test_final_approval_notifies_requester(self):
        """Completing the last stage notifies the requester of full approval."""
        instance = self._instance()
        with patch.object(routing_service, "get_handler", return_value=MagicMock()):
            with self.captureOnCommitCallbacks(execute=True):
                routing_service._terminate_approved(instance)

        self.assertEqual(
            self._feed_rows(self.requester, "workflow.final_approved").count(), 1,
        )

    def test_template_opt_out_suppresses_notification(self):
        """A configured notification_events dict is exact intent — missing key = off."""
        self.template.notification_events = {"workflow.rejected": True}
        self.template.save(update_fields=["notification_events"])
        try:
            instance = self._instance()
            eligible = [SimpleNamespace(user=self.approver, on_behalf_of=None)]
            with patch.object(routing_service.approvers_service, "resolve_approvers",
                              return_value=eligible):
                with self.captureOnCommitCallbacks(execute=True):
                    routing_service._activate_stage(instance, self.stage, attempt=1)

            self.assertEqual(
                self._feed_rows(self.approver, "workflow.stage_activated").count(), 0,
            )
        finally:
            self.template.notification_events = {}
            self.template.save(update_fields=["notification_events"])
