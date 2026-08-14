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


def _user(email, first_name="Test", last_name="User"):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.create_user(
        email=email, user_type="CX_STAFF", first_name=first_name, last_name=last_name,
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

        cls.requester = _user("requester@test.com", "Rita", "Requester")
        cls.approver = _user("approver@test.com", "Ada", "Approver")
        cls.template = WorkflowTemplate.objects.create(
            document_type="TEST_DOC", code="default", name="Test Template",
        )
        cls.stage = WorkflowStage.objects.create(
            template=cls.template, code="s1", label="S1",
            kind=StageKind.APPROVAL, order=1,
            advance_rule="ANY", on_rejection="TERMINAL",
            skip_if_no_approvers=False,
        )

    def _instance(self, stage=None, status=WorkflowInstanceStatus.IN_PROGRESS,
                  document_summary=None):
        ct = ContentType.objects.get_for_model(WorkflowTemplate)
        return WorkflowInstance.objects.create(
            tenant=self.requester.tenant,
            template=self.template,
            document_content_type=ct,
            document_object_id="fake-doc-id",
            document_type=self.template.document_type,
            status=status,
            requested_by=self.requester,
            current_stage=stage,
            submitted_at=timezone.now(),
            document_summary=document_summary or {},
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
        # document handler isn't registered for TEST_DOC - stub it out.
        with patch.object(routing_service, "get_handler", return_value=MagicMock()):
            with self.captureOnCommitCallbacks(execute=True):
                actions_service.record_action(
                    instance.id, self.approver, ActionEnum.REJECTED, comment="no budget",
                )

        self.assertEqual(
            self._feed_rows(self.requester, "workflow.rejected").count(), 1,
        )

    def test_final_approval_notifies_requester(self):
        """Automatic approval uses the document summary and names the system."""
        instance = self._instance(document_summary={
            "title": "Manuel Ola",
            "subtitle": "Platform user creation",
        })
        with patch.object(routing_service, "get_handler", return_value=MagicMock()):
            with self.captureOnCommitCallbacks(execute=True):
                routing_service._terminate_approved(instance)

        rows = self._feed_rows(self.requester, "workflow.final_approved")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().subject, "Platform User Creation Approved")
        self.assertEqual(
            rows.first().body,
            "Fully approved: 'Platform user creation: Manuel Ola' has been approved "
            "by the system and is now complete.",
        )

    def test_final_approval_notification_names_the_human_approver(self):
        instance = self._instance(
            stage=self.stage,
            document_summary={"title": "REQ-0042", "subtitle": "Purchase requisition"},
        )
        si = WorkflowStageInstance.objects.create(
            instance=instance, stage=self.stage,
            status=WorkflowStageStatus.ACTIVE, attempt=1,
            activated_at=timezone.now(),
        )
        WorkflowStageApprover.objects.create(
            stage_instance=si, user=self.approver, attempt=1,
        )

        with patch.object(routing_service, "get_handler", return_value=MagicMock()):
            with self.captureOnCommitCallbacks(execute=True):
                actions_service.record_action(
                    instance.id, self.approver, ActionEnum.APPROVED,
                )

        row = self._feed_rows(self.requester, "workflow.final_approved").get()
        self.assertEqual(row.subject, "Purchase Requisition Approved")
        self.assertIn("Purchase requisition: REQ-0042", row.body)
        self.assertIn("approved by Ada Approver", row.body)

    def test_template_opt_out_suppresses_notification(self):
        """A configured notification_events dict is exact intent - missing key = off."""
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


class ParkedRepairNotificationTests(TestCase):
    """A repaired stage must tell the people who just became eligible.

    A document parks when its stage activates with nobody able to approve it. The
    stage is already ACTIVE, so its activation notification fired to an empty
    recipient list and will never fire again. When somebody is finally appointed
    the repair fills the frozen approver snapshot - and until this was fixed it
    did so in silence, leaving the audit row as the only trace. The person who
    could now approve found the waiting document by opening the queue on spec,
    which can be days after it became approvable.
    """

    @classmethod
    def setUpTestData(cls):
        from vs_notifications.services.seed import (
            seed_event_types, seed_notification_templates,
        )
        seed_event_types()
        seed_notification_templates()

        cls.requester = _user("park-req@test.com", "Rita", "Requester")
        cls.approver = _user("park-apr@test.com", "Ada", "Approver")
        cls.template = WorkflowTemplate.objects.create(
            document_type="TEST_DOC", code="default", name="Parked Template",
        )
        cls.stage = WorkflowStage.objects.create(
            template=cls.template, code="s1", label="Checker",
            kind=StageKind.APPROVAL, order=1,
            advance_rule="ANY", on_rejection="TERMINAL",
            skip_if_no_approvers=False,
        )

    def _parked_instance(self):
        """An ACTIVE stage whose approver snapshot is empty - i.e. parked."""
        ct = ContentType.objects.get_for_model(WorkflowTemplate)
        instance = WorkflowInstance.objects.create(
            tenant=self.requester.tenant, template=self.template,
            document_content_type=ct, document_object_id="parked-doc",
            document_type=self.template.document_type,
            status=WorkflowInstanceStatus.IN_PROGRESS,
            requested_by=self.requester, current_stage=self.stage,
            submitted_at=timezone.now(),
            document_summary={"title": "REQ-0099", "subtitle": "Purchase requisition"},
        )
        WorkflowStageInstance.objects.create(
            instance=instance, stage=self.stage,
            status=WorkflowStageStatus.ACTIVE, attempt=1,
            activated_at=timezone.now(),
        )
        return instance

    def _repair(self, eligible):
        from vs_workflow.services import parking as parking_service

        with patch.object(parking_service.approvers_service, "resolve_approvers",
                          return_value=eligible), \
             patch.object(parking_service.ResolutionCache, "has_candidates",
                          return_value=True):
            with self.captureOnCommitCallbacks(execute=True):
                return parking_service.repair_workflows(tenant=self.requester.tenant)

    def _feed_rows(self, user):
        from vs_notifications.models import Notification
        return Notification.objects.filter(
            recipient=user, channel="in_app",
            event_type__key="workflow.stage_activated",
        )

    def test_repair_notifies_the_newly_eligible_approver(self):
        self._parked_instance()

        repaired = self._repair([SimpleNamespace(user=self.approver, on_behalf_of=None)])

        self.assertEqual(repaired, 1)
        rows = self._feed_rows(self.approver)
        self.assertEqual(rows.count(), 1)
        # The same copy a normally-activated stage produces, because that is what
        # it means to the recipient.
        self.assertIn("awaiting your decision", rows.first().body)
        self.assertIn("REQ-0099", rows.first().body)

    def test_a_repair_that_staffs_nobody_notifies_nobody(self):
        """Still parked means still silent - no empty or misleading message."""
        self._parked_instance()

        repaired = self._repair([])

        self.assertEqual(repaired, 0)
        self.assertEqual(self._feed_rows(self.approver).count(), 0)

    def test_a_second_repair_pass_neither_restaffs_nor_renotifies(self):
        """The freeze guarantee, seen from the notification side.

        The repair runs on every read of an approvals queue, so a parked document
        that has just been staffed is re-examined constantly. A populated snapshot
        must never be rewritten - and the person must not be told again on every
        page load.
        """
        from vs_workflow.models import WorkflowStageApprover

        self._parked_instance()
        eligible = [SimpleNamespace(user=self.approver, on_behalf_of=None)]

        self.assertEqual(self._repair(eligible), 1)
        self.assertEqual(self._repair(eligible), 0)

        self.assertEqual(WorkflowStageApprover.objects.count(), 1)
        self.assertEqual(self._feed_rows(self.approver).count(), 1)
