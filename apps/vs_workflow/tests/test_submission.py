"""Tests for submit_for_approval - focusing on template cascade lookup."""
from unittest.mock import patch, MagicMock

from django.test import TestCase

from vs_workflow.exceptions import InvalidInstanceStateError, TemplateNotFoundError
from vs_workflow.services.resolution import document_scope, resolve_template
from vs_workflow.services.submission import submit_for_approval


# ── Minimal fake document ─────────────────────────────────────────────────────

class _Doc:
    workflow_document_type = "TEST_DOC"
    tenant = None
    branch = None
    pk = "docpk01"


# ── Cascade logic ─────────────────────────────────────────────────────────────

class TemplateCascadeTests(TestCase):
    """The branch → tenant → platform cascade, against real rows.

    Tested through :func:`resolve_template` rather than through a mocked manager
    inside ``submission``, because the cascade is no longer submission's: the
    finance direct-post gate resolves the same way through the same function, and
    a test that counts ORM calls in one caller proves nothing about the other.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import codex_tenant, make_branch, make_school

        self.school = make_school(slug="cascade-school", name="Cascade School")
        self.tenant = self.school.tenant
        self.branch = make_branch(self.school)
        self.platform = codex_tenant()

    def _template(self, *, tenant, branch, code="default", document_type="TEST_DOC",
                  is_active=True, name="T"):
        from vs_workflow.models import WorkflowTemplate

        return WorkflowTemplate.objects.create(
            tenant=tenant, branch=branch, document_type=document_type,
            code=code, name=name, is_active=is_active,
        )

    def _resolve(self, *, tenant, branch, code="default"):
        return resolve_template("TEST_DOC", tenant=tenant, branch=branch, code=code)

    def test_branch_specific_template_used_first(self):
        """A branch-specific template is preferred over tenant-wide and platform."""
        branch_tpl = self._template(tenant=self.tenant, branch=self.branch)
        self._template(tenant=self.tenant, branch=None)
        self._template(tenant=None, branch=None)
        self.assertEqual(self._resolve(tenant=self.tenant, branch=self.branch),
                         branch_tpl)

    def test_falls_back_to_tenant_when_no_branch_template(self):
        tenant_tpl = self._template(tenant=self.tenant, branch=None)
        self._template(tenant=None, branch=None)
        self.assertEqual(self._resolve(tenant=self.tenant, branch=self.branch),
                         tenant_tpl)

    def test_falls_back_to_platform_when_no_branch_or_tenant_template(self):
        platform_tpl = self._template(tenant=None, branch=None)
        self.assertEqual(self._resolve(tenant=self.tenant, branch=self.branch),
                         platform_tpl)

    def test_a_tenant_doc_never_picks_up_another_branch_s_template(self):
        """A branch template must not capture a document with no branch."""
        self._template(tenant=self.tenant, branch=self.branch)
        self.assertIsNone(self._resolve(tenant=self.tenant, branch=None))

    def test_platform_doc_only_matches_the_platform_scope(self):
        """school=None, branch=None must not be answered by a tenant template."""
        self._template(tenant=self.tenant, branch=None)
        self.assertIsNone(self._resolve(tenant=None, branch=None))

        platform_tpl = self._template(tenant=None, branch=None)
        self.assertEqual(self._resolve(tenant=None, branch=None), platform_tpl)

    def test_a_switched_off_tenant_template_falls_through_to_platform(self):
        """is_active is part of the lookup, not a check after it.

        A tenant that adjusted a shared template and then asked for the platform
        version back switches its own off. Filtering after the fact would find the
        inactive one and stop, which is the tenant stuck on nothing at all.
        """
        self._template(tenant=self.tenant, branch=None, is_active=False)
        platform_tpl = self._template(tenant=None, branch=None)
        self.assertEqual(self._resolve(tenant=self.tenant, branch=None), platform_tpl)

    def test_a_different_code_at_a_nearer_scope_does_not_shadow(self):
        """The engine resolves by code, so a tenant ladder under another code
        must not hide the platform one the engine would actually load."""
        self._template(tenant=self.tenant, branch=None, code="other")
        platform_tpl = self._template(tenant=None, branch=None, code="default")
        self.assertEqual(self._resolve(tenant=self.tenant, branch=None), platform_tpl)

    def test_no_template_anywhere_resolves_to_none(self):
        self.assertIsNone(self._resolve(tenant=self.tenant, branch=self.branch))


class DocumentScopeTests(TestCase):
    """Which (tenant, branch) a document approves under."""

    def setUp(self):
        from vs_rbac.tests.helpers import make_branch, make_school

        self.school = make_school(slug="scope-school", name="Scope School")
        self.tenant = self.school.tenant
        self.branch = make_branch(self.school)

    def test_a_direct_tenant_attribute_wins_even_when_it_is_none(self):
        """A platform document gates on the platform template only.

        Falling back for an explicit None would let a tenant template capture a
        document that belongs to nobody's tenant.
        """
        doc = _Doc()
        self.assertEqual(document_scope(doc, default_tenant=self.tenant),
                         (None, None))

    def test_a_finance_style_document_scopes_through_its_entity(self):
        class _EntityDoc:
            workflow_document_type = "TEST_DOC"

        doc = _EntityDoc()
        doc.entity = MagicMock()
        doc.entity.tenant = self.tenant
        self.assertEqual(document_scope(doc), (self.tenant, None))

    def test_a_document_with_neither_takes_the_default(self):
        class _Bare:
            workflow_document_type = "TEST_DOC"
            entity = None

        self.assertEqual(document_scope(_Bare(), default_tenant=self.tenant),
                         (self.tenant, None))
        # A read-side gate passes no default and resolves platform-wide.
        self.assertEqual(document_scope(_Bare()), (None, None))


class SubmissionGuardTests(TestCase):

    def test_raises_template_not_found_when_all_scopes_miss(self):
        """TemplateNotFoundError raised when no scope has a matching template."""
        with patch("vs_workflow.services.submission.get_handler") as mock_get_handler:
            mock_get_handler.return_value.resolve_default_template_code.return_value = "x"
            mock_get_handler.return_value.validate_document.return_value = None
            with self.assertRaises(TemplateNotFoundError):
                submit_for_approval(_Doc(), MagicMock())

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

        # The seed's permission key was converted to the role that granted it.
        self.assertEqual(stage.approver_source, "ROLE")
        self.assertEqual(stage.approver_role_key, "xvs_platform_admin")
        self.assertEqual(stage.approver_scope, "PLATFORM")
        self.assertTrue(stage.skip_if_no_approvers)
