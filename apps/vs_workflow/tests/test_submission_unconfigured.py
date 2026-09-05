"""Submitting a document against a template that has no steps.

The hole this closes is not "approval was skipped". It is quieter than that. A
template with no stages makes ``_pick_next_stage`` return None on the very first
hop, ``advance_instance`` reads that as "no remaining stage means the workflow
is fully approved", and the instance terminates APPROVED before anybody has
seen it.

That matters most wherever a terminal approved instance is treated as
authority. A payout batch presents exactly that to move money:
``_PreparedDispatch`` carries "the terminal workflow instance that authorised
the batch". So a school whose payout template had no steps could pay a bank
with no human decision anywhere, and the trail would show a properly approved
instance saying it was fine.

The refusal lives in ``submit_for_approval`` rather than in each caller because
all four submit paths funnel through it - finance's direct calls, procurement's
wrapper, payments and user creation - and the next one added will too.
"""
from unittest.mock import MagicMock

from django.test import TestCase

from vs_workflow.exceptions import ApprovalNotConfiguredError
from vs_workflow.models import WorkflowInstance
from vs_workflow.services.submission import submit_for_approval




class StagelessTemplateSubmissionTests(TestCase):
    def setUp(self):
        from vs_rbac.tests.helpers import make_school, make_school_admin, make_branch
        from vs_workflow.models import WorkflowTemplate

        self.school = make_school(slug="empty-ladder", name="Empty Ladder School")
        self.tenant = self.school.tenant
        self.branch = make_branch(self.school)
        self.user = make_school_admin(self.branch)
        self.template = WorkflowTemplate.objects.create(
            tenant=self.tenant, branch=None, document_type="TEST_DOC",
            code="default", name="No steps at all", is_active=True,
        )
        # The branch is a real, tenant-owned row, so ContentType resolves and
        # ``document_scope`` finds the tenant the same way a real document does.
        self.document = self.branch
        self.document.workflow_document_type = "TEST_DOC"

    def _submit(self, **kwargs):
        with self._handler():
            return submit_for_approval(
                self.document, self.user, template_code="default", **kwargs,
            )

    def _handler(self):
        """The registry's handler for TEST_DOC, stubbed to do nothing.

        The handler is not what is under test: it validates the document and
        reacts to submission, and both are the owning module's business. What
        is under test is what the engine does before either runs.
        """
        from unittest.mock import patch

        handler = MagicMock()
        handler.validate_document.return_value = None
        handler.resolve_default_template_code.return_value = "default"
        handler.get_document_summary.return_value = {}
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch("vs_workflow.services.submission.get_handler",
                                  return_value=handler))
        stack.enter_context(patch("vs_workflow.services.routing.get_handler",
                                  return_value=handler))
        return stack

    def test_an_empty_ladder_refuses_instead_of_approving_instantly(self):
        with self.assertRaises(ApprovalNotConfiguredError):
            self._submit()

    def test_nothing_is_written_when_it_refuses(self):
        """A refusal that left an instance behind would be worse than none.

        The document would carry a submitted-looking record that no stage will
        ever advance, and a replay guard elsewhere would find it and return it
        as though the submission had succeeded.
        """
        with self.assertRaises(ApprovalNotConfiguredError):
            self._submit()
        self.assertEqual(WorkflowInstance.all_objects.count(), 0)

    def test_confirming_lets_it_through_and_records_who_said_so(self):
        from vs_audit.models import AuditEvent

        instance = self._submit(
            confirm_without_approval=True,
            confirmation_reason="No ladder built yet; agreed with the head.",
        )

        self.assertIsNotNone(instance)
        event = AuditEvent.objects.filter(
            action_type="POSTED_WITHOUT_APPROVAL",
        ).latest("event_at")
        self.assertEqual(event.actor_user, self.user)
        self.assertIn("No ladder built yet", event.metadata["reason"])

    def test_the_refusal_carries_the_code_a_caller_can_recognise(self):
        """409 with a named code, so a UI can offer the confirmation.

        Reading prose out of a nested detail dict to decide whether to show a
        dialog is how that dialog stops appearing the day the wording changes.
        """
        self.assertEqual(
            ApprovalNotConfiguredError.error_code, "APPROVAL_NOT_CONFIGURED",
        )
        self.assertEqual(ApprovalNotConfiguredError.http_status, 409)
