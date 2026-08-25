"""Tests for submit_for_approval - template cascade lookup and tenant scope."""
import itertools
from io import StringIO
from unittest.mock import patch, MagicMock

from django.test import TestCase

from vs_workflow.exceptions import (
    CrossTenantDocumentError, InvalidInstanceStateError, TemplateNotFoundError,
)
from vs_workflow.services.resolution import document_scope, resolve_template
from vs_workflow.services.submission import submit_for_approval


_counter = itertools.count(1)


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

class CrossTenantSubmissionTests(TestCase):
    """A submitter may only file into their own tenant.

    ``document_scope`` answers from the document, so a caller that hands over a
    document it never scoped to the requester would create the instance inside
    the *document's* tenant. That is what the removed generic
    ``POST /v1/workflow/instances/`` did: it loaded any content type by pk with
    the model's ordinary manager. The endpoint is gone, but the guard lives in
    the service because every module's submit endpoint passes through it.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import codex_tenant, make_branch, make_school

        self.bright_star = make_school(slug="bright-star-x", name="Bright Star").tenant
        self.greenfield = make_school(slug="greenfield-x", name="Greenfield").tenant
        make_branch(self.bright_star)
        make_branch(self.greenfield)
        self.platform = codex_tenant()

    def _doc(self, tenant):
        """A finance-shaped document: no tenant of its own, scoped via its entity."""
        class _EntityDoc:
            workflow_document_type = "TEST_DOC"
            pk = "docpk01"

        doc = _EntityDoc()
        doc.entity = MagicMock()
        doc.entity.tenant = tenant
        return doc

    def _user(self, tenant, **kwargs):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            email=f"u{next(_counter)}@test.com", password="pw", status="ACTIVE",
            first_name="Test", last_name="User", tenant=tenant, **kwargs)

    def test_another_tenant_s_document_is_refused(self):
        """Greenfield's bursar cannot submit Bright Star's document."""
        from vs_workflow.models import WorkflowInstance

        with patch("vs_workflow.services.submission.get_handler") as get_handler:
            get_handler.return_value.resolve_default_template_code.return_value = "standard"
            get_handler.return_value.validate_document.return_value = None
            with self.assertRaises(CrossTenantDocumentError):
                submit_for_approval(self._doc(self.bright_star),
                                    self._user(self.greenfield))
        self.assertEqual(WorkflowInstance.objects.count(), 0)

    def test_the_refusal_runs_before_the_document_handler_reacts(self):
        """Nothing may touch the other tenant's record, not even on_submitted.

        The payments handler stamps ``metadata["approval_status"]`` there, so a
        guard that ran after it would still leave another school's payout batch
        marked as awaiting approval.
        """
        with patch("vs_workflow.services.submission.get_handler") as get_handler:
            handler = get_handler.return_value
            handler.resolve_default_template_code.return_value = "standard"
            handler.validate_document.return_value = None
            with self.assertRaises(CrossTenantDocumentError):
                submit_for_approval(self._doc(self.bright_star),
                                    self._user(self.greenfield))
        handler.on_submitted.assert_not_called()

    def test_the_refusal_does_not_confirm_the_document_exists(self):
        """404, not 403: a 403 on somebody else's row is itself the disclosure."""
        self.assertEqual(CrossTenantDocumentError.http_status, 404)
        self.assertEqual(CrossTenantDocumentError.error_code, "DOCUMENT_NOT_FOUND")

    def test_the_owning_tenant_still_submits(self):
        """The guard refuses the foreign case only; it is not a blanket refusal.

        Proven by reaching TemplateNotFoundError, which is raised on the line
        immediately after the guard.
        """
        with patch("vs_workflow.services.submission.get_handler") as get_handler:
            get_handler.return_value.resolve_default_template_code.return_value = "standard"
            get_handler.return_value.validate_document.return_value = None
            with self.assertRaises(TemplateNotFoundError):
                submit_for_approval(self._doc(self.bright_star),
                                    self._user(self.bright_star))

    def test_a_platform_user_creation_still_submits(self):
        """The one flow whose document is not entity-scoped must keep working.

        A pending CX staff account carries a real ``tenant`` (the codex platform
        tenant), and the inviter is in that same tenant, so the guard passes.
        """
        with patch("vs_workflow.services.submission.get_handler") as get_handler:
            get_handler.return_value.resolve_default_template_code.return_value = "no-such"
            get_handler.return_value.validate_document.return_value = None
            with self.assertRaises(TemplateNotFoundError):
                submit_for_approval(self._doc(self.platform),
                                    self._user(self.platform))

    def test_a_document_whose_tenant_cannot_be_established_is_refused(self):
        """No registered document type resolves to a null tenant today.

        If one is ever added, the engine must refuse rather than resolve it
        platform-wide, where a tenant template could capture it.
        """
        with patch("vs_workflow.services.submission.get_handler") as get_handler:
            get_handler.return_value.resolve_default_template_code.return_value = "standard"
            get_handler.return_value.validate_document.return_value = None
            with self.assertRaises(CrossTenantDocumentError):
                submit_for_approval(_Doc(), self._user(self.greenfield))

    def test_platform_staff_may_still_submit_an_unscoped_document(self):
        """The escape hatch mirrors ``release.may_release``: platform staff only."""
        with patch("vs_workflow.services.submission.get_handler") as get_handler:
            get_handler.return_value.resolve_default_template_code.return_value = "no-such"
            get_handler.return_value.validate_document.return_value = None
            with self.assertRaises(TemplateNotFoundError):
                submit_for_approval(
                    _Doc(), self._user(self.platform, is_superuser=True))


class RetiredSubmitPermissionTests(TestCase):
    """``workflow.instance.submit`` gated one endpoint and that endpoint is gone.

    A key that grants nothing is worse than no key: it reads on a role screen as
    though it confers the ability to submit, so an administrator who grants it
    believes they have given something. Submission is gated per module
    (``finance.creditnote.submit``, ``procurement.requisition.submit``), because
    only the owning module can say which documents a caller may address.
    """

    KEY = "workflow.instance.submit"

    def test_the_key_is_not_present_after_migrations(self):
        from vs_rbac.models import Permission

        self.assertFalse(Permission.objects.filter(key=self.KEY).exists())

    def test_reseeding_does_not_bring_it_back(self):
        """The seeder is idempotent and must not recreate a retired key."""
        from django.core.management import call_command
        from vs_rbac.models import Permission

        # The seeder skips any action row that does not exist yet, so the
        # counterweight below would pass vacuously without this.
        call_command("seed_actions", verbosity=0, stdout=StringIO())
        call_command("seed_workflow_permissions", verbosity=0, stdout=StringIO())
        self.assertFalse(Permission.objects.filter(key=self.KEY).exists())
        # The counterweight: the keys that do gate something are still seeded.
        self.assertTrue(Permission.objects.filter(key="workflow.instance.view").exists())


class AutoSkipDefaultTests(TestCase):
    """A stage published without ``skip_if_no_approvers`` parks, it does not skip.

    The default used to be True on both the model and the publish service, so the
    dangerous answer arrived by omission. Omission is the common case: a tenant
    publishes its own full version of a central ladder, and an editor changing one
    threshold does not resend the fields it is not changing. A payout ladder
    republished that way would auto-skip a stage nobody could approve and dispatch
    the money unapproved.
    """

    def setUp(self):
        from vs_rbac.tests.helpers import make_school

        self.tenant = make_school(slug="skip-default", name="Skip Default").tenant

    def _publish(self, stage):
        from vs_workflow.services.templates import publish_template

        return publish_template(
            tenant=self.tenant, branch=None, document_type="TEST_DOC",
            code="standard", name="Ladder", stages_payload=[stage],
        )

    def test_an_omitted_flag_parks(self):
        template = self._publish({
            "code": "checker", "label": "Checker", "kind": "APPROVAL", "order": 1,
            "approver_source": "RBAC_PERMISSION",
            "approver_permission_key": "finance.journal.approve",
        })
        self.assertFalse(template.stages.get(code="checker").skip_if_no_approvers)

    def test_the_model_default_agrees_with_the_publish_default(self):
        """Two defaults for one decision is how they drift apart."""
        from vs_workflow.models import WorkflowStage

        self.assertIs(WorkflowStage._meta.get_field("skip_if_no_approvers").default, False)

    def test_asking_for_auto_skip_still_gets_it(self):
        """The counterweight: this is a safer default, not a removed capability."""
        template = self._publish({
            "code": "optional", "label": "Optional", "kind": "APPROVAL", "order": 1,
            "approver_source": "RBAC_PERMISSION",
            "approver_permission_key": "finance.journal.approve",
            "skip_if_no_approvers": True,
        })
        self.assertTrue(template.stages.get(code="optional").skip_if_no_approvers)
