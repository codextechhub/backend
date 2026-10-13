"""Open approval requests get their snapshot retaken; closed ones are left alone."""
from io import StringIO

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from vs_rbac.tests.helpers import make_vision_user
from vs_workflow.constants import WorkflowInstanceStatus
from vs_workflow.handlers import BaseWorkflowHandler, register_handler
from vs_workflow.models import WorkflowInstance, WorkflowTemplate
from vs_workflow.presentation import summary_fields

DOCUMENT_TYPE = "test.rebuildSnapshot"
MARKED = [{"label": "Amount", "value": "N10.00", "access": "finance.banktransfer.amount"}]


@register_handler(DOCUMENT_TYPE)
class _Handler(BaseWorkflowHandler):
    noun = "Test document"
    approval_releases_nothing = True

    def resolve_default_template_code(self, document):
        return "default"

    def get_document_summary(self, document):
        return {
            "title": document.name,
            "fields": summary_fields([("Amount", "N10.00", "finance.banktransfer.amount")]),
        }


class RebuildApprovalSnapshotsTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = make_vision_user(email="rebuild-snapshots@codex.test")
        cls.template = WorkflowTemplate.objects.create(
            document_type=DOCUMENT_TYPE, code="default", name="Rebuild snapshot",
        )
        cls.open_request = cls.request(WorkflowInstanceStatus.IN_PROGRESS)
        cls.returned_request = cls.request(WorkflowInstanceStatus.RETURNED)
        cls.closed_request = cls.request(WorkflowInstanceStatus.APPROVED)
        cls.orphan_request = cls.request(WorkflowInstanceStatus.IN_PROGRESS, object_id="missing")

    @classmethod
    def request(cls, status, object_id=None):
        return WorkflowInstance.all_objects.create(
            tenant=cls.user.tenant, template=cls.template,
            document_content_type=ContentType.objects.get_for_model(WorkflowTemplate),
            document_object_id=object_id or str(cls.template.pk),
            document_type=DOCUMENT_TYPE, status=status, requested_by=cls.user,
            submitted_at=timezone.now(),
            document_summary={
                "title": "Old", "fields": [{"label": "Amount", "value": "N10.00"}],
            },
        )

    def run_command(self, *args):
        out = StringIO()
        call_command("rebuild_approval_snapshots", *args, stdout=out)
        return out.getvalue()

    def stored(self, request):
        return WorkflowInstance.all_objects.get(pk=request.pk).document_summary

    def test_a_dry_run_reports_and_writes_nothing(self):
        report = self.run_command("--dry-run")

        self.assertIn("Would rewrite 2 open approval request(s)", report)
        self.assertNotIn("access", str(self.stored(self.open_request)))

    def test_open_and_returned_requests_gain_their_markers(self):
        report = self.run_command()

        self.assertIn("Rewrote 2 open approval request(s)", report)
        for request in (self.open_request, self.returned_request):
            self.assertEqual(self.stored(request)["fields"], MARKED)
            self.assertEqual(self.stored(request)["title"], self.template.name)

    def test_a_closed_request_and_one_without_a_document_are_left_as_they_were(self):
        self.run_command()

        self.assertEqual(self.stored(self.closed_request)["title"], "Old")
        self.assertEqual(self.stored(self.orphan_request)["title"], "Old")

    def test_running_it_again_changes_nothing(self):
        self.run_command()
        report = self.run_command()

        self.assertIn("Rewrote 0 open approval request(s); 2 already current", report)

    def test_a_tenant_filter_that_matches_nothing_rewrites_nothing(self):
        report = self.run_command("--tenant", "no-such-school")

        self.assertIn("Rewrote 0 open approval request(s)", report)
        self.assertEqual(self.stored(self.open_request)["title"], "Old")
