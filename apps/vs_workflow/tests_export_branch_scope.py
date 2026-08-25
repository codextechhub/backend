"""The approvals export narrows the way the approvals screens narrow.

Inclusive, because a branch-pinned row here is an override of the tenant-wide
default rather than a replacement for it. The exclusive reading was tried on
these screens and is recorded in ``views._filter_by_branch`` as a defect: it
left branch users with an empty list whenever the tenant published at tenant
level, which is the normal case.
"""
from __future__ import annotations

from django.test import TestCase

from vs_exports.catalogue import ScopeContext, get_dataset
from vs_rbac.models import PermissionScope
from vs_rbac.tests.helpers import (
    make_assignment,
    make_branch,
    make_permission,
    make_role,
    make_role_permission,
    make_school,
    make_school_admin,
)


class WorkflowExportBranchScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.school = make_school(slug="brightfield", name="Brightfield Schools")
        cls.tenant = cls.school.tenant
        cls.lekki = make_branch(cls.school, name="Lekki Campus", is_main=True)
        cls.ikeja = make_branch(cls.school, name="Ikeja Campus", is_main=False)

        role = make_role(cls.school, name="Approver", key="branch_admin")
        make_role_permission(
            role,
            make_permission("workflow.instance.view", scope=PermissionScope.TENANT),
        )
        cls.lekki_approver = make_school_admin(
            None, email="approver@lekki.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.lekki_approver, role, branch=cls.lekki)
        cls.head_office = make_school_admin(
            None, email="adaeze@brightfield.test", tenant=cls.tenant,
        )
        make_assignment(cls.school, cls.head_office, role, branch=None)

    @classmethod
    def _template(cls):
        from vs_workflow.models import WorkflowTemplate

        template, _ = WorkflowTemplate.objects.get_or_create(
            tenant=cls.tenant, document_type="TEST_DOC", code="default",
            defaults={"name": "Test Template"},
        )
        return template

    _seq = 0

    def instance(self, reference, branch):
        """Built the way this module's own tests build one.

        Reusing their shape rather than inventing a lighter fixture: an
        instance needs a template and a document reference, and a fixture that
        skipped them would be testing a row this module never creates.
        """
        from django.contrib.contenttypes.models import ContentType
        from django.utils import timezone

        from vs_workflow.constants import WorkflowInstanceStatus
        from vs_workflow.models import WorkflowInstance, WorkflowTemplate

        type(self)._seq += 1
        template = self._template()
        return WorkflowInstance.objects.create(
            tenant=self.tenant,
            branch=branch,
            template=template,
            document_content_type=ContentType.objects.get_for_model(WorkflowTemplate),
            document_object_id=f"doc-{self._seq}-{reference}",
            document_type=template.document_type,
            status=WorkflowInstanceStatus.IN_PROGRESS,
            requested_by=self.head_office,
            submitted_at=timezone.now(),
            document_summary={},
        )

    def count_for(self, user):
        return len(list(get_dataset("workflow.approvals").base(
            ScopeContext(tenant=self.tenant, user=user),
        )))

    def test_a_branch_approver_sees_their_own_plus_the_tenant_wide_ones(self):
        self.instance("lekki-1", self.lekki)
        self.instance("shared-1", None)
        self.instance("ikeja-1", self.ikeja)
        self.assertEqual(self.count_for(self.lekki_approver), 2)

    def test_a_branch_approver_never_sees_another_branchs(self):
        self.instance("ikeja-1", self.ikeja)
        self.assertEqual(self.count_for(self.lekki_approver), 0)

    def test_head_office_sees_everything(self):
        self.instance("lekki-1", self.lekki)
        self.instance("shared-1", None)
        self.instance("ikeja-1", self.ikeja)
        self.assertEqual(self.count_for(self.head_office), 3)

    def test_the_tenant_wide_row_is_never_dropped(self):
        """The exact defect the exclusive reading caused on these screens."""
        self.instance("shared-1", None)
        self.assertEqual(self.count_for(self.lekki_approver), 1)

    def test_the_dataset_narrows_at_all(self):
        """So a refactor that drops the call is caught here."""
        import inspect

        from vs_workflow import export_datasets

        self.assertIn(
            "narrow_to_caller_branches",
            inspect.getsource(export_datasets._instances),
        )
