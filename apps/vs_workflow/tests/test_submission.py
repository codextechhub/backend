"""Tests for submit_for_approval — focusing on template cascade lookup."""
from unittest.mock import patch, MagicMock

from django.test import TestCase

from vs_workflow.exceptions import InvalidInstanceStateError, TemplateNotFoundError
from vs_workflow.services.submission import submit_for_approval


# ── Minimal fake document ─────────────────────────────────────────────────────

class _Doc:
    workflow_document_type = "TEST_DOC"
    tenant = None
    branch = None
    pk = "docpk01"


class _SchoolDoc(_Doc):
    """Document scoped to a tenant but no branch."""
    tenant = "tenant-1"


class _BranchDoc(_Doc):
    """Document scoped to both tenant and branch."""
    tenant = "tenant-1"
    branch = "branch-1"


# ── Cascade logic (unit tests — no DB needed) ─────────────────────────────────

class TemplateCascadeTests(TestCase):

    def _make_template(self, label="tpl"):
        t = MagicMock()
        t.code = "default"
        t.__str__ = lambda self: label
        return t

    def _run(self, doc, branch_tpl=None, school_tpl=None, platform_tpl=None):
        """
        Patch WorkflowTemplate.objects.get to simulate which scopes have a
        template. Pass None for a scope to simulate DoesNotExist.
        """
        from vs_workflow.models import WorkflowTemplate

        def fake_get(document_type, code, tenant, branch):
            if tenant == doc.tenant and branch == doc.branch and branch_tpl:
                return branch_tpl
            if tenant == doc.tenant and branch is None and school_tpl:
                return school_tpl
            if tenant is None and branch is None and platform_tpl:
                return platform_tpl
            raise WorkflowTemplate.DoesNotExist

        mock_handler = MagicMock()
        mock_handler.resolve_default_template_code.return_value = "default"
        mock_handler.validate_document.return_value = None
        mock_handler.get_document_summary.return_value = {}
        mock_handler.on_submitted.return_value = None

        with patch("vs_workflow.services.submission.WorkflowTemplate.objects") as mock_mgr, \
             patch("vs_workflow.services.submission.get_handler", return_value=mock_handler), \
             patch("vs_workflow.services.submission.ContentType.objects") as mock_ct, \
             patch("vs_workflow.services.submission.WorkflowInstance.objects") as mock_inst, \
             patch("vs_workflow.services.submission.audit_service"), \
             patch("vs_workflow.services.submission.routing_service"):

            mock_mgr.get.side_effect = lambda **kw: fake_get(
                kw["document_type"], kw["code"], kw["tenant"], kw["branch"]
            )
            mock_ct.get_for_model.return_value = MagicMock()
            created = MagicMock()
            mock_inst.create.return_value = created

            result = submit_for_approval(doc, requester := MagicMock())
            return result, mock_mgr.get.call_args_list

    def test_branch_specific_template_used_first(self):
        """When a branch-specific template exists it is preferred over school/platform."""
        doc = _BranchDoc()
        branch_tpl = self._make_template("branch")
        school_tpl = self._make_template("school")
        platform_tpl = self._make_template("platform")
        result, calls = self._run(doc, branch_tpl, school_tpl, platform_tpl)
        # Only one get() call — found on first try.
        self.assertEqual(len(calls), 1)

    def test_falls_back_to_school_when_no_branch_template(self):
        """Missing branch template → school-wide template used."""
        doc = _BranchDoc()
        school_tpl = self._make_template("school")
        platform_tpl = self._make_template("platform")
        _, calls = self._run(doc, None, school_tpl, platform_tpl)
        self.assertEqual(len(calls), 2)  # branch miss → school hit

    def test_falls_back_to_platform_when_no_branch_or_school_template(self):
        """Missing branch and school templates → platform template used."""
        doc = _BranchDoc()
        platform_tpl = self._make_template("platform")
        _, calls = self._run(doc, None, None, platform_tpl)
        self.assertEqual(len(calls), 3)  # branch miss → school miss → platform hit

    def test_school_doc_skips_branch_scope(self):
        """Document with no branch only tries school-wide and platform — no branch scope."""
        doc = _SchoolDoc()
        school_tpl = self._make_template("school")
        _, calls = self._run(doc, None, school_tpl, None)
        self.assertEqual(len(calls), 1)  # only school scope tried; found immediately

    def test_platform_doc_only_tries_platform_scope(self):
        """Document with school=None and branch=None only tries the platform scope."""
        doc = _Doc()  # school=None, branch=None
        platform_tpl = self._make_template("platform")
        _, calls = self._run(doc, None, None, platform_tpl)
        self.assertEqual(len(calls), 1)

    def test_platform_doc_no_duplicate_scope_tried(self):
        """Platform docs must not hit the same scope twice (regression guard)."""
        doc = _Doc()
        platform_tpl = self._make_template("platform")
        _, calls = self._run(doc, None, None, platform_tpl)
        # Should be exactly 1 call, not 2.
        self.assertEqual(len(calls), 1)

    def test_raises_template_not_found_when_all_scopes_miss(self):
        """TemplateNotFoundError raised when no scope has a matching template."""
        doc = _BranchDoc()
        with patch("vs_workflow.services.submission.WorkflowTemplate.objects") as mock_mgr, \
             patch("vs_workflow.services.submission.get_handler") as mock_get_handler:
            from vs_workflow.models import WorkflowTemplate
            mock_mgr.get.side_effect = WorkflowTemplate.DoesNotExist
            mock_get_handler.return_value.resolve_default_template_code.return_value = "x"
            mock_get_handler.return_value.validate_document.return_value = None
            with self.assertRaises(TemplateNotFoundError):
                submit_for_approval(doc, MagicMock())

    def test_missing_document_type_raises(self):
        """Document without workflow_document_type raises InvalidInstanceStateError."""
        class NoTypDoc:
            pk = "x"
        with self.assertRaises(InvalidInstanceStateError):
            submit_for_approval(NoTypDoc(), MagicMock())


class PlatformUserCreationTemplateTests(TestCase):
    def test_default_template_is_seeded_for_the_platform_tenant(self):
        from vs_workflow.models import WorkflowTemplate

        template = WorkflowTemplate.objects.get(
            tenant__slug="codex",
            document_type="PLATFORM_USER_CREATION",
            code="p-user-creation",
        )
        stage = template.stages.get(code="platform-admin-approval")

        self.assertEqual(stage.approver_permission_key, "platform.team.create")
        self.assertEqual(stage.approver_scope, "PLATFORM")
        self.assertTrue(stage.skip_if_no_approvers)
